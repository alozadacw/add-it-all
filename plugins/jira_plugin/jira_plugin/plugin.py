"""
Jira connector: issues assigned to and reported by a person.

`add-it-all jira <identifier>` shows the issues currently assigned to
someone; `-r` shows what they reported.

Three facts about Jira Cloud drive this module's shape. All were
established by probing the real instance on 2026-09-07, not read from
documentation -- the docs and most tutorials still describe the old API.

**`/rest/api/3/search` is gone.** It answers `410 Gone` with a pointer to
`/rest/api/3/search/jql`. Anything built against the old endpoint fails for
every user, so there is a test pinning that we never call it.

**The new endpoint refuses unbounded JQL** ("Please add a search
restriction to your query") and pages with an opaque cursor
(`nextPageToken` + `isLast`) rather than `startAt` offsets. Most
importantly it returns **no `total`**: there is no cheap way to learn how
many issues someone has. Reporting the page size as a count would
understate a workload, and quietly-wrong answers are the failure mode this
whole tool exists to avoid -- so an incomplete page is reported as "at
least N", never as N.

**JQL cannot take a username.** GDPR-era changes removed usernames and
emails from JQL; a query must use an `accountId`. So a lookup is two steps:
resolve the identifier via `/user/search`, then query with the id. Passing
a username instead matches nothing *without* erroring, which is exactly the
kind of silent empty answer worth guarding against.

**Scope (decided 2026-09-07).** The default view is *assigned* issues, with
reported available via `-r`. Assigned is the actionable set: open work that
needs reassigning when someone leaves, or that says what they are stuck on.
Reported is historical. On the real instance the two differ substantially
for the same person, so they are shown under separate headings and never
merged into one count.

**Looking up one issue.** `add-it-all jira ENG-1` shows a single ticket.
The identifier is normally a person, but an issue key is distinctive enough
(`ABC-123`) to detect without guessing -- an email always carries an `@`,
and a display name never has the letters-hyphen-digits shape. Detection is
case-insensitive because Jira itself is: the API answers 200 for
`eng-42` as readily as the uppercase spelling.

There is deliberately no fallback from a failed key lookup to a person
search. A string shaped like an issue key that Jira does not know is a
typo, not a colleague.

Required env vars (see `.env.example`):
    JIRA_BASE_URL       e.g. https://your-org.atlassian.net
    JIRA_EMAIL          the account the API token belongs to
    JIRA_API_TOKEN      an API token (Basic auth, email:token)
Optional:
    JIRA_TIMEOUT_SECONDS        per-request timeout (default 10)
    ADD_IT_ALL_MOCK_JIRA=1      serve a fixture instead of calling out
"""

from __future__ import annotations

import asyncio
import base64
import re

import httpx
import typer
from rich.console import Console
from rich.table import Table

from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult
from add_it_all.redaction import safe_error

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Relationships this connector will query. Whitelisted rather than
#: interpolated freely: the value lands in a JQL string, and accepting
#: arbitrary text there would let a caller smuggle in their own query.
RELATIONSHIPS = ("assignee", "reporter")

#: Issues fetched per request. The API caps this; asking for more is
#: silently reduced rather than erroring.
PAGE_SIZE = 100

#: Rows printed before the CLI starts saying "more not shown".
MAX_ISSUES_SHOWN = 15

#: Hard bound on cursor following, so a looping `nextPageToken` cannot hang
#: the CLI.
_MAX_PAGES = 20

#: Only the fields actually rendered. Jira issues carry description bodies,
#: comments and custom fields; `data` is written to the plaintext local
#: cache, so anything not displayed is not requested.
_ISSUE_FIELDS = "key,summary,status,project,priority,updated"

_STATUS_COLOURS = {"Done": "green", "In Progress": "yellow", "To Do": "cyan"}

#: Jira project keys are letters then letters/digits/underscore; the issue
#: number is decimal. Matched case-insensitively because Jira resolves
#: lowercase keys too -- a case-sensitive pattern would send `eng-1` down the
#: person-search path and fail confusingly.
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")

#: Fields fetched for a single issue. The untrimmed response is ~46KB on the
#: live instance -- roughly 90 custom fields plus comments, worklog and
#: attachments -- and everything in `data` reaches the plaintext local cache.
_SINGLE_ISSUE_FIELDS = (
    "summary,status,project,issuetype,priority,assignee,reporter,creator,"
    "created,updated,resolution,labels,description"
)

#: Description text is truncated in the table; the full body can run to
#: thousands of characters.
DESCRIPTION_WIDTH = 400


def looks_like_issue_key(value: str | None) -> bool:
    """True if `value` is shaped like an issue key rather than a person."""
    return bool(_ISSUE_KEY_RE.match((value or "").strip()))


#: ADF nodes that end a line of prose. Their text must not run into whatever
#: follows -- joining blocks with nothing produced "instance:1. Add/set up"
#: from a real ticket, welding a sentence end onto the next paragraph.
_ADF_BLOCK_TYPES = frozenset({
    "paragraph", "heading", "listItem", "blockquote", "codeBlock",
    "panel", "rule", "tableRow", "tableCell", "tableHeader", "mediaSingle",
})


def flatten_adf(node: object) -> str | None:
    """Plain text from an Atlassian Document Format body.

    API v3 returns descriptions as a nested ADF document rather than a
    string, so the raw value is unreadable JSON. This walks the tree keeping
    text nodes; unknown node types contribute nothing rather than raising,
    since Atlassian keeps adding them.

    Block-level nodes emit a trailing space so adjacent paragraphs and list
    items stay separate words, while inline runs inside one paragraph (a
    bold span mid-sentence, say) are concatenated -- a space there would
    break words apart.

    Returns None rather than "" for an empty document, so the CLI can show
    "no description" distinctly from an empty one.
    """

    def walk(item: object) -> str:
        if isinstance(item, list):
            return "".join(walk(child) for child in item)
        if not isinstance(item, dict):
            return ""
        node_type = item.get("type")
        if node_type == "text":
            return str(item.get("text") or "")
        if node_type == "hardBreak":
            return " "
        inner = "".join(walk(child) for child in item.get("content") or [])
        return f"{inner} " if node_type in _ADF_BLOCK_TYPES else inner

    text = " ".join(walk(node).split())
    return text or None


def build_jql(relationship: str, account_id: str) -> str:
    """JQL selecting one person's issues, newest first.

    `relationship` is whitelisted because it is interpolated into a query
    string. The account id is quoted: ids contain a colon, which JQL would
    otherwise read as an operator.

    The ORDER BY is not cosmetic -- without it the first page is arbitrary,
    and the first page is all most people read.
    """
    if relationship not in RELATIONSHIPS:
        raise ValueError(
            f"unknown relationship {relationship!r}; expected one of {', '.join(RELATIONSHIPS)}"
        )
    return f'{relationship} = "{account_id}" ORDER BY updated DESC'


class JiraPlugin(ConnectorPlugin):
    name = "jira"
    required_credentials = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")

    async def fetch(
        self,
        identifier: str,
        *,
        relationship: str = "assignee",
        account_id: str | None = None,
        fetch_all: bool = False,
    ) -> ConnectorResult:
        """Issues for one person. Never raises for ordinary failures."""
        try:
            account = None
            if account_id is None:
                accounts = await self._call_user_search_backend(identifier)
                if not accounts:
                    return self._not_found(identifier)
                if len(accounts) > 1:
                    # Picking the first would attribute someone else's
                    # tickets to the person you asked about.
                    return ConnectorResult(
                        plugin_name=self.name,
                        identifier=identifier,
                        data={"found": False, "ambiguous": True,
                              "candidates": [self._to_account(a) for a in accounts],
                              "account": None, "issues": [], "count": 0,
                              "complete": True, "relationship": relationship},
                        tags=["ambiguous"],
                    )
                account = self._to_account(accounts[0])
                account_id = account["account_id"]

            issues, complete = await self._call_search_backend(
                build_jql(relationship, account_id), fetch_all
            )
        except ValueError as exc:
            return ConnectorResult(plugin_name=self.name, identifier=identifier, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                error=safe_error(exc, secrets=[self.config.get("JIRA_API_TOKEN")]),
            )

        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={
                "found": True,
                "ambiguous": False,
                "candidates": [],
                "account": account,
                "issues": issues,
                "count": len(issues),
                # False means "there are more we did not fetch". The API
                # gives no total, so `count` is only a real count when this
                # is True -- see the module docstring.
                "complete": complete,
                "relationship": relationship,
            },
            properties={"account_id": account_id},
            tags=["no-issues"] if not issues else ["has-issues"],
        )

    async def fetch_issue(self, key: str) -> ConnectorResult:
        """One issue by key. Never raises for ordinary failures."""
        try:
            raw = await self._call_issue_backend(key.strip())
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=key,
                error=safe_error(exc, secrets=[self.config.get("JIRA_API_TOKEN")]),
            )

        if raw is None:
            return ConnectorResult(
                plugin_name=self.name,
                identifier=key,
                data={
                    "found": False,
                    "issue": None,
                    # Jira answers 404 both for issues that do not exist and
                    # for issues the caller cannot see -- deliberately, so
                    # existence is not leaked. Saying only "no such issue"
                    # would send someone hunting for a typo that isn't there.
                    "not_found_reason": (
                        "no such issue, or this account lacks permission to view it"
                    ),
                },
                tags=["not-found"],
            )

        return ConnectorResult(
            plugin_name=self.name,
            identifier=key,
            data={"found": True, "issue": self._to_full_issue(raw), "not_found_reason": None},
            properties={"issue_id": raw.get("id")},
            tags=["issue"],
        )

    def _not_found(self, identifier: str) -> ConnectorResult:
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={"found": False, "ambiguous": False, "candidates": [], "account": None,
                  "issues": [], "count": 0, "complete": True, "relationship": "assignee"},
            tags=["not-found"],
        )

    # -- backend seam ---------------------------------------------------------

    async def _call_user_search_backend(self, identifier: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_accounts_fixture()

        url = f"{self._base_url()}/rest/api/3/user/search"
        async with self._client() as client:
            response = await client.get(
                url, headers=self._headers(), params={"query": identifier, "maxResults": "10"}
            )
        self._raise_for_auth(response)
        response.raise_for_status()
        return response.json()

    async def _call_issue_backend(self, key: str) -> dict | None:
        if self.mock_mode:
            return self._mock_single_issue_fixture()

        url = f"{self._base_url()}/rest/api/3/issue/{key}"
        async with self._client() as client:
            response = await client.get(
                url, headers=self._headers(), params={"fields": _SINGLE_ISSUE_FIELDS}
            )
        self._raise_for_auth(response)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def _call_search_backend(
        self, jql: str, fetch_all: bool = False
    ) -> tuple[list[dict], bool]:
        """Return (issues, complete).

        One page by default. `complete` is False when Jira said `isLast:
        false` and we stopped -- the caller must not treat len(issues) as a
        total in that case.
        """
        if self.mock_mode:
            # Shaped, not raw: the mock path must produce the same structure
            # as the real one or mock mode exercises a different code path
            # than the thing it is standing in for.
            return [self._to_issue(i) for i in self._mock_issues_fixture()], True

        url = f"{self._base_url()}/rest/api/3/search/jql"
        params = {"jql": jql, "maxResults": str(PAGE_SIZE), "fields": _ISSUE_FIELDS}

        issues: list[dict] = []
        complete = True
        seen_tokens: set[str] = set()

        async with self._client() as client:
            for _ in range(_MAX_PAGES if fetch_all else 1):
                response = await client.get(url, headers=self._headers(), params=params)
                self._raise_for_auth(response)
                if response.status_code == 400:
                    # The endpoint rejects unbounded JQL, among other things.
                    # Its raw body is not actionable and the query is ours.
                    raise RuntimeError(
                        "Jira rejected the JQL query. This is a bug in how the query "
                        "is built, not something a different name will fix."
                    )
                response.raise_for_status()

                body = response.json()
                issues.extend(body.get("issues") or [])

                if body.get("isLast", True):
                    break
                token = body.get("nextPageToken")
                if not token or token in seen_tokens:
                    # No cursor, or a repeating one. Either way we cannot
                    # safely continue, and there is more we have not seen.
                    complete = False
                    break
                seen_tokens.add(token)
                params = dict(params, nextPageToken=token)
            else:
                complete = False

        return [self._to_issue(i) for i in issues], complete

    def _base_url(self) -> str:
        return self.config.require("JIRA_BASE_URL").rstrip("/")

    def _client(self) -> httpx.AsyncClient:
        timeout = float(self.config.get("JIRA_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
        return httpx.AsyncClient(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        # Jira Cloud uses Basic email:token. Built by hand rather than via
        # httpx's auth= so the value flows through safe_error's scrubbing
        # like every other credential in this project.
        raw = f"{self.config.require('JIRA_EMAIL')}:{self.config.require('JIRA_API_TOKEN')}"
        encoded = base64.b64encode(raw.encode()).decode()
        return {"Authorization": f"Basic {encoded}", "Accept": "application/json"}

    @staticmethod
    def _raise_for_auth(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise RuntimeError(
                "Jira rejected the request. Check JIRA_EMAIL and JIRA_API_TOKEN, and "
                "that the account has Browse Projects on the projects you expect."
            )

    def _mock_accounts_fixture(self) -> list[dict]:
        return [{"accountId": "mock-account-1", "displayName": "Mock Dana Example",
                 "emailAddress": "dana@example.com", "accountType": "atlassian", "active": True}]

    def _mock_issues_fixture(self) -> list[dict]:
        return [
            {"key": "MOCK-1", "fields": {
                "summary": "Mock: laptop will not boot",
                "status": {"name": "In Progress", "statusCategory": {"name": "In Progress"}},
                "project": {"key": "MOCK", "name": "Mock Project"},
                "priority": {"name": "High"}, "updated": "2026-09-01T10:00:00.000+0000"}},
            {"key": "MOCK-2", "fields": {
                "summary": "Mock: request VPN access",
                "status": {"name": "To Do", "statusCategory": {"name": "To Do"}},
                "project": {"key": "MOCK", "name": "Mock Project"},
                "priority": {"name": "Medium"}, "updated": "2026-08-20T09:00:00.000+0000"}},
        ]

    def _mock_single_issue_fixture(self) -> dict:
        return {"id": "10001", "key": "MOCK-1", "fields": {
            "summary": "Mock: cloud console admin access",
            "status": {"name": "Blocked", "statusCategory": {"name": "In Progress"}},
            "project": {"key": "MOCK", "name": "Mock Project"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "High"},
            "assignee": {"displayName": "Mock Dana Example"},
            "reporter": {"displayName": "Mock Ravi Patel"},
            "creator": {"displayName": "Mock Ravi Patel"},
            "created": "2026-08-01T09:00:00.000+0000",
            "updated": "2026-09-08T14:00:00.000+0000",
            "resolution": None,
            "labels": ["access", "cloud"],
            "description": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Mock: please provision staging access."}]}]},
        }}

    # -- shaping --------------------------------------------------------------

    @staticmethod
    def _to_account(account: dict) -> dict:
        return {
            "account_id": account.get("accountId"),
            "display_name": account.get("displayName"),
            "email": account.get("emailAddress"),
            "account_type": account.get("accountType"),
            # A deactivated Jira user is exactly who an offboarding check is
            # asking about, so this is carried rather than filtered on.
            "active": account.get("active"),
        }

    @classmethod
    def _to_full_issue(cls, issue: dict) -> dict:
        """One issue in detail. Reporter and creator are kept separate: a
        ticket raised on a colleague's behalf has different people in each,
        and for a tool about people that difference is the interesting part."""
        fields = issue.get("fields") or {}

        def person(key: str) -> str | None:
            value = fields.get(key) or {}
            return value.get("displayName")

        base = cls._to_issue(issue)
        base.update({
            "issue_type": (fields.get("issuetype") or {}).get("name"),
            "project_name": (fields.get("project") or {}).get("name"),
            "assignee": person("assignee"),
            "reporter": person("reporter"),
            "creator": person("creator"),
            "created": (fields.get("created") or "")[:10] or None,
            "resolution": (fields.get("resolution") or {}).get("name"),
            "labels": fields.get("labels") or [],
            "description": flatten_adf(fields.get("description")),
        })
        return base

    @staticmethod
    def _to_issue(issue: dict) -> dict:
        fields = issue.get("fields") or {}
        status = fields.get("status") or {}
        return {
            "key": issue.get("key"),
            "summary": fields.get("summary"),
            "status": status.get("name"),
            "status_category": (status.get("statusCategory") or {}).get("name"),
            "project": (fields.get("project") or {}).get("key"),
            "priority": (fields.get("priority") or {}).get("name"),
            "updated": (fields.get("updated") or "")[:10] or None,
        }

    # -- CLI ------------------------------------------------------------------

    def cli(self) -> typer.Typer:
        """`add-it-all jira <identifier> [-t] [-r]`."""
        sub_app = typer.Typer(
            help="Jira issue lookups.",
            context_settings={"allow_interspersed_args": True},
        )
        console = Console()

        @sub_app.callback(invoke_without_command=True)
        def jira(
            identifier: str = typer.Argument(
                ...,
                help="Email address or display name of a person, or an issue "
                "key like ENG-123.",
            ),
            tickets: bool = typer.Option(
                False, "--tickets", "-tickets", "-t",
                help="Issues assigned to this person. The default when no other "
                "flag is given.",
            ),
            reported: bool = typer.Option(
                False, "--reported", "-reported", "-r",
                help="Issues this person reported, rather than ones assigned to them.",
            ),
            show_all: bool = typer.Option(
                False, "--all",
                help="Follow pagination and show every issue. Jira's search API "
                "returns no total, so without this a long list is reported as "
                "'at least N' rather than a count.",
            ),
        ) -> None:
            """Look up a person's Jira issues, or one issue by key."""
            # An issue key is a different question, not a different view of
            # the same one, so it dispatches before any section logic. The
            # section flags describe a person's workload and mean nothing
            # for a single ticket -- rejected rather than silently dropped.
            if looks_like_issue_key(identifier):
                unusable = [
                    name for name, on in
                    (("--tickets", tickets), ("--reported", reported), ("--all", show_all))
                    if on
                ]
                if unusable:
                    console.print(
                        f"[red]{', '.join(unusable)} cannot be used with an issue key.[/red]\n"
                        f"{identifier} is one ticket; those flags select a person's issues.\n"
                        f"Did you mean: [bold]add-it-all jira {identifier}[/bold]?"
                    )
                    raise typer.Exit(code=2)
                _print_issue(identifier)
                return

            # Flags select sections. With none given, assigned is what people
            # want; `-r` alone means reported only.
            show_assigned = tickets or not reported
            sole_section = sum((show_assigned, reported)) == 1

            # Resolve once even when both sections are shown.
            first = asyncio.run(
                self.fetch(
                    identifier,
                    relationship="assignee" if show_assigned else "reporter",
                    fetch_all=show_all,
                )
            )
            if not first.ok:
                console.print(f"[red]Jira lookup failed:[/red] {first.error}")
                raise typer.Exit(code=1)

            if first.data["ambiguous"]:
                _print_candidates(identifier, first.data["candidates"])
                return
            if not first.data["found"]:
                console.print(f"[yellow]No Jira account found for[/yellow] {identifier}")
                return

            _print_account(first.data["account"])
            account_id = first.properties.get("account_id")

            if show_assigned:
                _print_issues(first.data, primary=sole_section, fetch_all=show_all)
            if reported:
                second = (
                    first
                    if not show_assigned
                    else asyncio.run(
                        self.fetch(
                            identifier, relationship="reporter",
                            account_id=account_id, fetch_all=show_all,
                        )
                    )
                )
                if not second.ok:
                    console.print(f"[yellow]Reported issues unavailable:[/yellow] {second.error}")
                    if sole_section:
                        raise typer.Exit(code=1)
                    return
                _print_issues(second.data, primary=sole_section, fetch_all=show_all)

        def _print_issue(key: str) -> None:
            result = asyncio.run(self.fetch_issue(key))

            if not result.ok:
                console.print(f"[red]Jira lookup failed:[/red] {result.error}")
                raise typer.Exit(code=1)

            if not result.data["found"]:
                console.print(
                    f"[yellow]No Jira issue[/yellow] {key} [yellow]-[/yellow] "
                    f"{result.data['not_found_reason']}."
                )
                return

            issue = result.data["issue"]
            colour = _STATUS_COLOURS.get(issue["status_category"], "yellow")
            console.print(
                f"[bold]{issue['key']}[/bold]  [{colour}]{issue['status'] or '-'}[/{colour}]\n"
                f"{issue['summary'] or '-'}"
            )

            table = Table(show_header=False)
            table.add_column("field", no_wrap=True)
            table.add_column("value")
            rows = [
                ("type", issue["issue_type"]),
                ("project", f"{issue['project']} ({issue['project_name']})"
                            if issue["project_name"] else issue["project"]),
                ("priority", issue["priority"]),
                ("assignee", issue["assignee"] or "[yellow]unassigned[/yellow]"),
                ("reporter", issue["reporter"]),
            ]
            # Only when it differs: a ticket raised on someone's behalf has a
            # different creator, and that is worth seeing. Showing it always
            # would just be a duplicate row on most tickets.
            if issue["creator"] and issue["creator"] != issue["reporter"]:
                rows.append(("creator", issue["creator"]))
            rows += [
                ("created", issue["created"]),
                ("updated", issue["updated"]),
                ("resolution", issue["resolution"]),
                ("labels", ", ".join(issue["labels"]) if issue["labels"] else None),
            ]
            for label, value in rows:
                table.add_row(label, str(value) if value else "-")
            console.print(table)

            if issue["description"]:
                body = issue["description"]
                if len(body) > DESCRIPTION_WIDTH:
                    cut = body[:DESCRIPTION_WIDTH]
                    body = cut[: cut.rindex(" ")] + "\u2026" if " " in cut else cut + "\u2026"
                console.print(f"\n[dim]description[/dim]\n{body}")

        def _print_account(account: dict) -> None:
            state = "" if account.get("active") else " [red](inactive)[/red]"
            console.print(
                f"[bold]{account.get('display_name') or '-'}[/bold]"
                f"{state} [dim]{account.get('email') or ''}[/dim]"
            )

        def _print_issues(data: dict, primary: bool, fetch_all: bool = False) -> None:
            label = "Assigned to" if data["relationship"] == "assignee" else "Reported by"
            issues = data["issues"]

            if not issues:
                console.print(f"[yellow]No issues {label.lower()} this person.[/yellow]")
                return

            # The API gives no total, so an incomplete page must never be
            # presented as a count -- "5" and "at least 5" are different
            # claims about someone's workload.
            count = f"{data['count']}" if data["complete"] else f"at least {data['count']}"
            # Display policy follows what the user asked for, not whether the
            # fetch happened to be complete. Conflating the two produced a
            # live bug: --all fetched 236 issues, rendered 15, and advised
            # "use --all" -- the flag already in use.
            shown = issues if fetch_all else issues[:MAX_ISSUES_SHOWN]

            table = Table(title=f"{label} ({count})")
            table.add_column("key", no_wrap=True)
            table.add_column("summary")
            table.add_column("status", no_wrap=True)
            table.add_column("project", no_wrap=True)
            table.add_column("updated", no_wrap=True)
            for issue in shown:
                colour = _STATUS_COLOURS.get(issue["status_category"], "yellow")
                table.add_row(
                    issue["key"] or "-",
                    issue["summary"] or "-",
                    f"[{colour}]{issue['status'] or '-'}[/{colour}]",
                    issue["project"] or "-",
                    issue["updated"] or "-",
                )
            console.print(table)

            # Both hints are suppressed under --all: advising a flag the
            # user has already passed is a dead end, not guidance.
            if not fetch_all:
                if len(issues) > len(shown):
                    console.print(
                        f"[yellow]{len(issues) - len(shown)} more not shown[/yellow] - "
                        "use [bold]--all[/bold]"
                    )
                if not data["complete"]:
                    console.print(
                        "[yellow]Jira returned no total and there are more pages[/yellow] - "
                        "use [bold]--all[/bold] for a real count."
                    )

        def _print_candidates(identifier: str, candidates: list[dict]) -> None:
            console.print(
                f"[yellow]{len(candidates)} Jira accounts match[/yellow] '{identifier}'"
                f"[yellow]. Re-run with an exact email:[/yellow]"
            )
            table = Table()
            table.add_column("name")
            table.add_column("email")
            table.add_column("active", no_wrap=True)
            for candidate in candidates:
                table.add_row(
                    candidate["display_name"] or "-",
                    candidate["email"] or "-",
                    "yes" if candidate["active"] else "[red]no[/red]",
                )
            console.print(table)

        return sub_app
