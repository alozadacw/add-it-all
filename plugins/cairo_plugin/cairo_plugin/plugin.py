"""
CAIRO connector: the internal TPRM vendor and application register.

`add-it-all cairo <name>` answers "is this vendor/application allowed in
our space, and what do we know about how it is used".

**Not person-scoped.** Every other connector here takes a person. CAIRO
takes a vendor or application name. It is deliberately excluded from the
Stage 7 person aggregate -- see `docs/STAGES.md`. That exclusion lives in
the aggregate command's explicit plugin list rather than as a flag here,
so adding this connector required no change to `src/add_it_all/`.

Two API facts drive the shape of this module.

**There is no server-side filtering.** `?search=`, `?name=`, `?status=`,
`?limit=`, `?page=` are all accepted and all ignored -- `/api/vendors`
returns the entire register every time (713 records / ~730KB when this was
written). So matching is ours to define and happens client-side, and the
shared cache does real work rather than shaving milliseconds.

**The application detail is nested inside the vendor detail.** There is no
`/api/applications` endpoint. `GET /api/vendors/{id}` embeds
`assessments[]`, and each assessment's `engagement_context` describes one
application and how it is used. A vendor can therefore have several
applications; 22 of them do.

**Three fields are called some variant of "status" and none of them mean
the same thing.** On live data they disagree on every single record:

    vendor.status              approval -- "allowed in our space"
                               approved / denied / pending_approval /
                               review_required / under_review
    assessment.status          assessment workflow state
                               exempt / pending / pre_complete / post_complete
    assessment.workflow_status review state
                               draft / review_complete / tprm_review / ...

They are surfaced under distinct labels and never merged. Collapsing them
would produce a confident, wrong answer to the one question this connector
exists to answer.

**Personal data is collected only where it answers the question.**
Everything in `data` is written to the plaintext local cache, so unused
personal data is not carried. Three people-fields exist and they are three
different people:

    owner                     owns the vendor relationship   67 distinct
    engagement.business_owner owns this application         223 distinct
    assigned_to_name          ran the TPRM review             5 distinct

The first two are carried and shown under distinct labels -- they agree on
256 of 568 live records and differ on 312, so collapsing them would
misattribute ownership. **The reviewer is never carried**: showing the
analyst who ran the review where an owner is expected points at the wrong
person entirely. `primary_contact`, `assigned_to`, `third_party_contact`,
`third_party_email` and `coupa_requester_email` are also dropped -- they
are vendor-side or process contacts, not owners.

Required env vars (see `.env.example`):
    CAIRO_BASE_URL      e.g. https://tprm.example.internal
    CAIRO_API_KEY       read-only API key, sent as `Authorization: Bearer`
Optional:
    CAIRO_TIMEOUT_SECONDS       per-request timeout (default 10)
    ADD_IT_ALL_MOCK_CAIRO=1     serve a fixture instead of calling out
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import httpx
import typer
from rich.console import Console
from rich.table import Table

from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult
from add_it_all.redaction import safe_error

DEFAULT_TIMEOUT_SECONDS = 10.0

#: The approval value that means "allowed in our space". Confirmed with the
#: CAIRO owner 2026-09-04; the other four values all mean "not (yet) allowed"
#: but for different reasons, so they are shown verbatim rather than folded
#: into a boolean.
APPROVED_STATUS = "approved"

#: Vendor approval statuses, and how to colour them. Anything unrecognised
#: stays yellow: an unknown status is not evidence that something is allowed.
_STATUS_COLOURS: dict[str, str] = {
    "approved": "green",
    "denied": "red",
    "review_required": "yellow",
    "under_review": "yellow",
    "pending_approval": "yellow",
}

_RISK_COLOURS: dict[str, str] = {
    "critical": "red",
    "high": "red",
    "medium": "yellow",
    "low": "green",
}

#: Width budget for the `use case` cell. Live descriptions are prose
#: paragraphs -- several hundred characters -- and rendering one raw turns a
#: one-row table into a fifteen-line block that pushes every other column off
#: the screen. Verified against the real register before choosing a number.
DESCRIPTION_WIDTH = 90

#: Cap on how many candidates the chooser prints. The register is 713 rows;
#: a one-letter query would otherwise fill the terminal.
MAX_MATCHES_SHOWN = 15


def summarise(text: str | None, limit: int = DESCRIPTION_WIDTH) -> str | None:
    """Shorten a prose description to something that fits a table cell.

    Prefers cutting at the end of the first sentence: on live data that
    sentence is almost always the "what is this" summary, which is exactly
    what a scan of the table wants. Falls back to a word-boundary character
    cut so a truncated value never ends mid-word.
    """
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed

    head, sep, _ = collapsed.partition(". ")
    if sep and len(head) + 1 <= limit:
        return f"{head}."

    cut = collapsed[:limit]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return f"{cut.rstrip()}\u2026"


def normalise(text: str | None) -> str:
    """Casefold and collapse whitespace, for comparing names."""
    return " ".join((text or "").split()).casefold()


def match_vendors(vendors: list[dict], query: str) -> list[dict]:
    """Vendors whose name or domain contains `query`, case-insensitively.

    Substring rather than exact: the API offers no search of its own, and
    nobody types "AcmeSec Corporation, Inc." from memory. Domain is included
    because people often know the URL but not the registered company name.
    """
    needle = normalise(query)
    if not needle:
        return []
    hits = [
        v
        for v in vendors
        if needle in normalise(v.get("name")) or needle in normalise(v.get("domain"))
    ]
    return sorted(hits, key=lambda v: normalise(v.get("name")))


def pick_exact(matches: list[dict], query: str) -> dict | None:
    """The one vendor whose name matches `query` exactly, if there is one.

    Without this, searching "Databright" for a register that also holds
    "Databright Plugin" would be ambiguous forever, and the exactly-named
    vendor could never be reached.
    """
    needle = normalise(query)
    exact = [v for v in matches if normalise(v.get("name")) == needle]
    return exact[0] if len(exact) == 1 else None


class CairoPlugin(ConnectorPlugin):
    name = "cairo"
    required_credentials = ("CAIRO_BASE_URL", "CAIRO_API_KEY")

    async def fetch(self, identifier: str) -> ConnectorResult:
        """Resolve `identifier` to one vendor, with its applications.

        Never raises for ordinary failures -- returns `ConnectorResult(error=)`.
        """
        try:
            vendors = await self._call_vendors_backend()
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return self._error(identifier, exc)

        matches = match_vendors(vendors, identifier)
        if not matches:
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                data={"found": False, "ambiguous": False, "matches": [],
                      "vendor": None, "applications": [], "assessed": False,
                      "approved": False, "detail_error": None},
                tags=["not-found"],
            )

        chosen = matches[0] if len(matches) == 1 else pick_exact(matches, identifier)
        if chosen is None:
            # Several candidates and no exact name. Picking the "best" one
            # would be a guess, and an approval answer for the wrong vendor
            # is worse than making someone choose.
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                data={"found": False, "ambiguous": True,
                      "matches": [self._to_summary(v) for v in matches],
                      "vendor": None, "applications": [], "assessed": False,
                      "approved": False, "detail_error": None},
                tags=["ambiguous"],
            )

        # The list call already answered "does this exist and is it approved".
        # A detail failure degrades the applications section rather than
        # discarding an answer we already have.
        detail: dict = {}
        detail_error: str | None = None
        try:
            detail = await self._call_vendor_detail_backend(chosen["id"])
        except Exception as exc:  # noqa: BLE001
            detail_error = safe_error(exc, secrets=[self.config.get("CAIRO_API_KEY")])

        assessments = detail.get("assessments") or []
        applications = [self._to_application(a) for a in assessments]
        vendor = self._to_vendor(detail or chosen)

        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={
                "found": True,
                "ambiguous": False,
                "matches": [],
                "vendor": vendor,
                "applications": applications,
                # Distinct from "has no applications": 174 of 713 vendors have
                # never been assessed, and the register simply does not say.
                "assessed": bool(assessments) if detail_error is None else False,
                "approved": vendor["status"] == APPROVED_STATUS,
                "detail_error": detail_error,
            },
            properties={"vendor_id": chosen.get("id")},
            tags=["approved" if vendor["status"] == APPROVED_STATUS else "not-approved"],
        )

    def _error(self, identifier: str, exc: Exception) -> ConnectorResult:
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            error=safe_error(exc, secrets=[self.config.get("CAIRO_API_KEY")]),
        )

    # -- backend seam ---------------------------------------------------------

    async def _call_vendors_backend(self) -> list[dict]:
        if self.mock_mode:
            return self._mock_vendors_fixture()

        async with self._client() as client:
            response = await client.get(self._url("/api/vendors"), headers=self._headers())
        self._raise_for_auth(response)
        response.raise_for_status()
        return response.json()

    async def _call_vendor_detail_backend(self, vendor_id: str) -> dict:
        if self.mock_mode:
            return self._mock_detail_fixture()

        url = self._url(f"/api/vendors/{quote(str(vendor_id), safe='')}")
        async with self._client() as client:
            response = await client.get(url, headers=self._headers())
        self._raise_for_auth(response)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _raise_for_auth(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise RuntimeError(
                "CAIRO rejected the request. Check CAIRO_API_KEY is set and still "
                "valid, and that CAIRO_BASE_URL points at the right environment."
            )

    def _url(self, path: str) -> str:
        return f"{self.config.require('CAIRO_BASE_URL').rstrip('/')}{path}"

    def _client(self) -> httpx.AsyncClient:
        timeout = float(self.config.get("CAIRO_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
        return httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.require('CAIRO_API_KEY')}",
            "Accept": "application/json",
        }

    def _mock_vendors_fixture(self) -> list[dict]:
        return [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "name": "Mock AcmeSec Corporation",
                "domain": "acmesec.example",
                "status": "approved",
                "risk_level": "low",
                "category": "Cloud SaaS",
                "data_classification": "Confidential",
                "last_approved_at": "2025-11-02 12:00:00+00",
                "next_review_at": "2026-11-02 12:00:00+00",
            }
        ]

    def _mock_detail_fixture(self) -> dict:
        detail = dict(self._mock_vendors_fixture()[0])
        detail["assessments"] = [
            {
                "id": "00000000-0000-0000-0000-0000000000a1",
                "status": "post_complete",
                "workflow_status": "review_complete",
                "engagement_context": {
                    "description": "Mock threat-intel enrichment for the security team.",
                    "engagement_type": "Cloud SaaS",
                    "data_classification": "Confidential",
                    "production_access": "yes",
                    "inherent_risk_level": "medium",
                },
            }
        ]
        return detail

    # -- shaping ----------------------------------------------------------------
    #
    # Everything below drops personal data on purpose. `primary_contact`,
    # `owner`, `assigned_to`, `assigned_to_name` and `coupa_requester_email`
    # exist on every record and none are needed to answer "is this allowed" --
    # and `data` is written to the plaintext cache.

    @staticmethod
    def _to_summary(vendor: dict) -> dict:
        """The few fields the chooser needs."""
        return {
            "id": vendor.get("id"),
            "name": vendor.get("name"),
            "status": vendor.get("status"),
            "risk_level": vendor.get("risk_level"),
            "category": vendor.get("category"),
        }

    @staticmethod
    def _to_vendor(vendor: dict) -> dict:
        return {
            "name": vendor.get("name"),
            # Owns the *vendor relationship* (67 distinct people on live data).
            # Distinct from an application's `business_owner`, and distinct
            # again from `assigned_to_name`, which is the TPRM analyst who ran
            # the review -- only 5 people org-wide and deliberately excluded.
            "owner": vendor.get("owner"),
            "status": vendor.get("status"),
            "risk_level": vendor.get("risk_level"),
            "category": vendor.get("category"),
            "domain": vendor.get("domain"),
            "data_classification": vendor.get("data_classification"),
            "last_approved_at": (vendor.get("last_approved_at") or "")[:10] or None,
            "next_review_at": (vendor.get("next_review_at") or "")[:10] or None,
        }

    @staticmethod
    def _to_application(assessment: dict) -> dict:
        context = assessment.get("engagement_context") or {}
        return {
            "description": context.get("description"),
            # Owns *this application* (223 distinct people on live data).
            # Agrees with the vendor-level `owner` on 256 of 568 records and
            # differs on 312, so the two are reported separately rather than
            # collapsed into one "owner".
            "business_owner": context.get("business_owner"),
            "engagement_type": context.get("engagement_type"),
            "data_classification": context.get("data_classification"),
            "production_access": context.get("production_access"),
            "inherent_risk_level": context.get("inherent_risk_level"),
            # Kept under their own names. See the module docstring: these are
            # NOT the vendor's approval status and must never read as if.
            "assessment_status": assessment.get("status"),
            "workflow_status": assessment.get("workflow_status"),
        }

    # -- CLI ------------------------------------------------------------------

    def cli(self) -> typer.Typer:
        """`add-it-all cairo <vendor-or-application-name>`.

        Same shape as every other connector: identifier as a direct argument,
        no noun subcommands. Lives here, not in core `cli.py`.
        """
        sub_app = typer.Typer(
            help="CAIRO (TPRM) vendor and application register lookups.",
            context_settings={"allow_interspersed_args": True},
        )
        console = Console()

        @sub_app.callback(invoke_without_command=True)
        def cairo(
            identifier: str = typer.Argument(
                ..., help="Vendor or application name (or domain). Partial names work."
            ),
        ) -> None:
            """Look one vendor or application up in CAIRO."""
            result = asyncio.run(self.fetch(identifier))

            if not result.ok:
                console.print(f"[red]CAIRO lookup failed:[/red] {result.error}")
                raise typer.Exit(code=1)

            data = result.data

            if data["ambiguous"]:
                _print_matches(identifier, data["matches"])
                return

            if not data["found"]:
                console.print(
                    f"[yellow]No vendor or application in CAIRO matches[/yellow] {identifier}"
                )
                return

            _print_vendor(data["vendor"])

            if data["detail_error"]:
                console.print(f"[yellow]Application detail unavailable:[/yellow] {data['detail_error']}")
                return

            if not data["assessed"]:
                # Deliberately not "no applications" -- the register has no
                # assessment on file, which is a different claim.
                console.print(
                    "[yellow]No assessment on file[/yellow] - no application detail "
                    "recorded for this vendor."
                )
                return

            _print_applications(data["applications"])

        def _print_vendor(vendor: dict) -> None:
            status = vendor["status"] or "unknown"
            colour = _STATUS_COLOURS.get(status, "yellow")
            risk = vendor["risk_level"] or "unknown"
            risk_colour = _RISK_COLOURS.get(risk, "yellow")
            console.print(
                f"[bold]{vendor['name']}[/bold] - [{colour}]{status.upper()}[/{colour}]"
                f"  (risk: [{risk_colour}]{risk}[/{risk_colour}])"
            )

            table = Table(title=f"CAIRO - {vendor['name']}")
            table.add_column("field")
            table.add_column("value")
            for label, key in (
                ("status", "status"),
                ("risk level", "risk_level"),
                ("category", "category"),
                ("vendor owner", "owner"),
                ("domain", "domain"),
                ("data classification", "data_classification"),
                ("last approved", "last_approved_at"),
                ("next review", "next_review_at"),
            ):
                table.add_row(label, str(vendor[key]) if vendor[key] is not None else "-")
            console.print(table)

        def _print_applications(applications: list[dict]) -> None:
            table = Table(title=f"Applications / engagements ({len(applications)})")
            table.add_column("use case")
            # The application's own owner, not the vendor's and not the
            # reviewer's. no_wrap because a truncated name is worse than
            # useless -- it points at a person who may not exist.
            table.add_column("owner", no_wrap=True)
            table.add_column("type", no_wrap=True)
            table.add_column("prod\naccess", no_wrap=True)
            table.add_column("inherent\nrisk", no_wrap=True)
            for app in applications:
                risk = app["inherent_risk_level"] or "-"
                colour = _RISK_COLOURS.get(risk, "yellow")
                table.add_row(
                    summarise(app["description"]) or "-",
                    app["business_owner"] or "-",
                    app["engagement_type"] or "-",
                    app["production_access"] or "-",
                    f"[{colour}]{risk}[/{colour}]",
                )
            console.print(table)

        def _print_matches(identifier: str, matches: list[dict]) -> None:
            shown = matches[:MAX_MATCHES_SHOWN]
            console.print(
                f"[yellow]{len(matches)} vendors match[/yellow] '{identifier}'"
                f"[yellow]. Re-run with a more specific name:[/yellow]"
            )
            table = Table()
            table.add_column("name")
            table.add_column("status", no_wrap=True)
            table.add_column("risk", no_wrap=True)
            table.add_column("category")
            for match in shown:
                status = match["status"] or "unknown"
                colour = _STATUS_COLOURS.get(status, "yellow")
                table.add_row(
                    match["name"] or "-",
                    f"[{colour}]{status}[/{colour}]",
                    match["risk_level"] or "-",
                    match["category"] or "-",
                )
            console.print(table)
            if len(matches) > len(shown):
                # Never truncate silently -- a short list reads as "that's all".
                console.print(
                    f"[yellow]{len(matches) - len(shown)} more not shown[/yellow] - "
                    "narrow the search."
                )

        return sub_app
