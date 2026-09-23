"""
Jamf Pro connector: the authoritative managed-hardware inventory.

`add-it-all jamf <user>` lists the computers Jamf has assigned to someone;
`-m` adds mobile devices.

**Scope, versus Okta.** Okta's `-d` lists its *device registry* -- machines
enrolled through Okta Verify / device trust. Jamf is the actual managed
hardware inventory. The two legitimately disagree: a laptop can be in Jamf
and unknown to Okta, or enrolled in Okta and never managed by Jamf. Neither
is wrong, and the disagreement is often the useful part. The CLI help says
so, because users conflate the two constantly.

**Auth is a token exchange, not a static key.** Jamf Pro's current method
is an API Client (Settings > API Roles and Clients): POST client_id and
client_secret to `/api/oauth/token`, receive a bearer token good for ~20
minutes. The token is fetched once per plugin instance and reused, so a
lookup showing both computers and mobile devices pays for one exchange
rather than two.

**`lastContactTime` and `reportDate` are different things** and are
never conflated here. The first is when the device last checked in; the
second is when it last submitted a full inventory. A machine can check in
daily while its inventory is months stale, so collapsing them into "last
seen" would answer a question nobody asked -- the same trap as Okta's
device `lastUpdated`, which is why both are carried under their own names
and rendered as separate columns.

Note the inventory field is `reportDate`, **not** `lastReportDate`. The
latter is the name you would guess, the mocks originally used it, and every
test passed while the column would have been permanently empty against a
real instance. Verified live 2026-09-21.

Required env vars (see `.env.example`):
    JAMF_BASE_URL       e.g. https://your-org.jamfcloud.com
    JAMF_CLIENT_ID      API Client id
    JAMF_CLIENT_SECRET  API Client secret
Optional:
    JAMF_TIMEOUT_SECONDS        per-request timeout (default 10)
    ADD_IT_ALL_MOCK_JAMF=1      serve a fixture instead of calling out
"""

from __future__ import annotations

import asyncio
import time

import httpx
import typer
from rich.console import Console
from rich.table import Table

from add_it_all.plugins.base import ConnectorPlugin, ConnectorResult
from add_it_all.redaction import safe_error

DEFAULT_TIMEOUT_SECONDS = 10.0

#: Inventory sections to request. A full Jamf record also carries
#: applications, fonts, plugins, local accounts, certificates and printers --
#: megabytes per machine. Everything in `data` reaches the plaintext local
#: cache, so only what is rendered is asked for.
_COMPUTER_SECTIONS = ("GENERAL", "HARDWARE", "OPERATING_SYSTEM", "USER_AND_LOCATION")

#: Fields an identifier is matched against. A person may type a username, a
#: device name or a serial and should not have to say which -- reported
#: 2026-09-23, when `jamf CW-EXAMPLE001-L` found nothing because only the
#: username was searched though the machine existed under `general.name`.
#: RSQL `or` covers all three in ONE request, so there is no shape-guessing
#: and no fallback chain whose ordering would decide ambiguous cases.
COMPUTER_FILTER_FIELDS = ("userAndLocation.username", "general.name", "hardware.serialNumber")

#: The mobile endpoint names the same concepts differently: it rejects
#: `userAndLocation.username` with INVALID_FIELD and wants flat `username`,
#: and the device is `displayName` rather than `name`.
MOBILE_FILTER_FIELDS = ("username", "displayName", "serialNumber")

#: Mobile records are section-nested exactly like computers, and `hardware`
#: and `userAndLocation` come back null unless requested -- so serial, model
#: and the assigned user are all silently absent without this.
_MOBILE_SECTIONS = ("GENERAL", "HARDWARE", "USER_AND_LOCATION")

#: Jamf caps page size; the fleet is filtered to one user server-side so this
#: is generous rather than tuned.
PAGE_SIZE = 200

#: Re-exchange this many seconds before a token's stated expiry. Tokens here
#: live 59 seconds, so the margin is a meaningful fraction of the lifetime
#: rather than a rounding error.
TOKEN_REFRESH_MARGIN_SECONDS = 10


def build_user_filter(
    identifier: str, fields: tuple[str, ...] = COMPUTER_FILTER_FIELDS
) -> str:
    """RSQL filter matching `identifier` against every field it could be.

    An identifier is a username, a device name or a serial number, and the
    caller should not have to declare which. All three are OR-ed into a
    single query rather than tried in sequence: a fallback chain costs a
    round trip per wrong guess, and its ordering would silently decide which
    record wins when a string matches two fields.

    Filtering server-side matters regardless -- this instance holds 3,461
    computers, and pulling the fleet to find one laptop would be slow and
    rude.

    `fields` differs by endpoint. That is not a guess: the mobile endpoint
    answers INVALID_FIELD for `userAndLocation.username` and enumerates what
    it will accept.
    """
    cleaned = (identifier or "").strip()
    if not cleaned:
        raise ValueError("jamf needs a username, device name or serial to look up.")
    # Escape backslashes then quotes so a value cannot terminate the RSQL
    # string or inject a clause. Applied per-clause: one unescaped quote
    # would now break all three.
    safe = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return " or ".join(f'{field}=="{safe}"' for field in fields)


class JamfPlugin(ConnectorPlugin):
    name = "jamf"
    required_credentials = ("JAMF_BASE_URL", "JAMF_CLIENT_ID", "JAMF_CLIENT_SECRET")

    def __init__(self, config=None):
        super().__init__(config)
        #: Cached with its expiry. Do NOT assume a long life: this instance
        #: issues tokens with `expires_in: 59` (verified 2026-09-21), not the
        #: ~20 minutes the docs imply. Caching for the life of the process
        #: would hand a dead token to the second section of a `-dm` lookup.
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def fetch(self, identifier: str) -> ConnectorResult:
        """Computers assigned to `identifier`. Never raises for ordinary failures."""
        return await self._fetch_devices(
            identifier, self._call_computers_backend, self._to_computer
        )

    async def fetch_mobile_devices(self, identifier: str) -> ConnectorResult:
        """Phones and tablets assigned to `identifier`."""
        return await self._fetch_devices(
            identifier, self._call_mobile_backend, self._to_mobile_device,
            fields=MOBILE_FILTER_FIELDS,
        )

    async def _fetch_devices(
        self, identifier, backend, shaper, fields=COMPUTER_FILTER_FIELDS
    ) -> ConnectorResult:
        try:
            raw = await backend(build_user_filter(identifier, fields))
        except ValueError as exc:
            return ConnectorResult(plugin_name=self.name, identifier=identifier, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - contract: never crash aggregation
            return ConnectorResult(
                plugin_name=self.name,
                identifier=identifier,
                error=safe_error(exc, secrets=[self.config.get("JAMF_CLIENT_SECRET")]),
            )

        devices = [shaper(entry) for entry in raw]
        return ConnectorResult(
            plugin_name=self.name,
            identifier=identifier,
            data={"found": True, "devices": devices, "count": len(devices)},
            tags=["no-devices"] if not devices else ["has-devices"],
        )

    # -- backend seam ---------------------------------------------------------

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        # Refresh a little before the deadline rather than after it: a
        # 59-second token can otherwise die between the check and the
        # request it authorises, surfacing as a random 401 on the second
        # section rather than anything diagnosable.
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token

        response = await client.post(
            f"{self._base_url()}/api/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.config.require("JAMF_CLIENT_ID"),
                "client_secret": self.config.require("JAMF_CLIENT_SECRET"),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code in (400, 401, 403):
            raise RuntimeError(
                "Jamf rejected the API client credentials. Check JAMF_CLIENT_ID and "
                "JAMF_CLIENT_SECRET, and that the API Client is enabled in "
                "Settings > API Roles and Clients."
            )
        response.raise_for_status()
        body = response.json()
        self._token = body["access_token"]
        lifetime = float(body.get("expires_in") or 0)
        self._token_expires_at = time.monotonic() + max(lifetime - TOKEN_REFRESH_MARGIN_SECONDS, 0)
        return self._token

    async def _call_computers_backend(self, user_filter: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_computers_fixture()

        params = [("filter", user_filter), ("page-size", str(PAGE_SIZE))]
        params += [("section", section) for section in _COMPUTER_SECTIONS]
        return await self._get_inventory("/api/v1/computers-inventory", params, "computers")

    async def _call_mobile_backend(self, user_filter: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_mobile_fixture()

        params = [("filter", user_filter), ("page-size", str(PAGE_SIZE))]
        params += [("section", section) for section in _MOBILE_SECTIONS]
        return await self._get_inventory("/api/v2/mobile-devices/detail", params, "mobile devices")

    async def _get_inventory(self, path: str, params: list, what: str) -> list[dict]:
        async with self._client() as client:
            token = await self._access_token(client)
            response = await client.get(
                f"{self._base_url()}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
        if response.status_code in (401, 403):
            # The client authenticated (we hold a token) but its API Role
            # lacks the object privilege. Naming the privilege saves a hunt.
            raise RuntimeError(
                f"Jamf refused the {what} query. The API Role attached to this client "
                f"likely lacks Read privileges for {what}."
            )
        if response.status_code == 400:
            # Typically INVALID_FIELD on the filter. The raw httpx message
            # echoes the entire URL and explains nothing; Jamf's own
            # description is the useful part.
            detail = ""
            try:
                errors = response.json().get("errors") or []
                detail = (errors[0] or {}).get("description", "")
            except Exception:  # noqa: BLE001 - a malformed body must not mask the 400
                pass
            raise RuntimeError(
                f"Jamf rejected the {what} filter. {detail} "
                "This is a bug in how the query is built, not something a different "
                "username will fix."
            )
        response.raise_for_status()
        return response.json().get("results") or []

    def _base_url(self) -> str:
        return self.config.require("JAMF_BASE_URL").rstrip("/")

    def _client(self) -> httpx.AsyncClient:
        timeout = float(self.config.get("JAMF_TIMEOUT_SECONDS") or DEFAULT_TIMEOUT_SECONDS)
        return httpx.AsyncClient(timeout=timeout)

    def _mock_computers_fixture(self) -> list[dict]:
        return [{
            "id": "101",
            "general": {"name": "Mock MacBook Pro", "lastContactTime": "2026-09-20T08:15:00.000Z",
                        "reportDate": "2026-09-19T03:00:00.000Z",
                        "remoteManagement": {"managed": True}},
            "hardware": {"serialNumber": "C02MOCK00001",
                         "model": "MacBook Pro (16-inch, 2021)"},
            "operatingSystem": {"name": "macOS", "version": "15.6.0"},
            "userAndLocation": {"username": "jdoe"},
        }]

    def _mock_mobile_fixture(self) -> list[dict]:
        # Section-nested, matching what the live endpoint returns. A flat
        # fixture is what let the original mobile shaper pass every test
        # while reading every field from the wrong place.
        return [{
            "mobileDeviceId": "55",
            "general": {"displayName": "Mock iPhone", "osVersion": "18.2", "managed": True,
                        "lastContactDate": "2026-09-19T12:00:00.000Z",
                        "lastInventoryUpdateDate": "2026-09-18T10:00:00.000Z"},
            "hardware": {"serialNumber": "F2LMOCK0001", "model": "iPhone 15 Pro"},
            "userAndLocation": {"username": "jdoe"},
        }]

    # -- shaping --------------------------------------------------------------
    #
    # `userAndLocation` also carries realname, email, position and department.
    # Only the username is kept -- it confirms the device is attributed to the
    # right person, which is the question being asked. The rest is personal
    # data that would land in the plaintext cache for no benefit.

    @staticmethod
    def _to_computer(entry: dict) -> dict:
        general = entry.get("general") or {}
        hardware = entry.get("hardware") or {}
        os_info = entry.get("operatingSystem") or {}
        user = entry.get("userAndLocation") or {}
        os_name = " ".join(
            part for part in (os_info.get("name"), os_info.get("version")) if part
        )
        return {
            "device_id": entry.get("id"),
            "name": general.get("name"),
            "serial_number": hardware.get("serialNumber"),
            "model": hardware.get("model"),
            "os": os_name or None,
            "managed": bool((general.get("remoteManagement") or {}).get("managed")),
            # Two different questions; see the module docstring.
            "last_check_in": (general.get("lastContactTime") or "")[:10] or None,
            # `reportDate`, NOT `lastReportDate`. Verified against a live
            # instance 2026-09-21: the documented-looking name is not what
            # the GENERAL section sends, and the mocks had agreed with the
            # wrong one.
            "last_inventory": (general.get("reportDate") or "")[:10] or None,
            "assigned_to": user.get("username"),
        }

    @staticmethod
    def _to_mobile_device(entry: dict) -> dict:
        """Mobile records are section-nested like computers, and use their
        own field names: `general.displayName` not `name`,
        `general.lastContactDate` not `lastContactTime`. Verified live
        2026-09-21 -- the first implementation read a flat object and got
        nothing but the id."""
        general = entry.get("general") or {}
        hardware = entry.get("hardware") or {}
        user = entry.get("userAndLocation") or {}
        return {
            "device_id": entry.get("mobileDeviceId"),
            "name": general.get("displayName"),
            "serial_number": hardware.get("serialNumber"),
            "model": hardware.get("model"),
            "os": general.get("osVersion"),
            "managed": bool(general.get("managed")),
            "last_check_in": (general.get("lastContactDate") or "")[:10] or None,
            "last_inventory": (general.get("lastInventoryUpdateDate") or "")[:10] or None,
            "assigned_to": user.get("username"),
        }

    # -- CLI ------------------------------------------------------------------

    def cli(self) -> typer.Typer:
        """`add-it-all jamf <identifier> [-d] [-m]`."""
        sub_app = typer.Typer(
            help="Jamf Pro managed-hardware lookups. This is the authoritative "
            "inventory -- unlike `okta -d`, which lists Okta's own device "
            "registry. The two legitimately disagree.",
            context_settings={"allow_interspersed_args": True},
        )
        console = Console()

        @sub_app.callback(invoke_without_command=True)
        def jamf(
            identifier: str = typer.Argument(..., help="Jamf username the device is assigned to."),
            devices: bool = typer.Option(
                False, "--devices", "-devices", "-d",
                help="Computers assigned to this user. The default when no other "
                "flag is given.",
            ),
            mobile: bool = typer.Option(
                False, "--mobile", "-mobile", "-m",
                help="Mobile devices (phones, tablets) assigned to this user.",
            ),
        ) -> None:
            """Look one person's Jamf-managed hardware up."""
            show_computers = devices or not mobile
            sole_section = sum((show_computers, mobile)) == 1

            if show_computers:
                _print_devices(
                    asyncio.run(self.fetch(identifier)),
                    title="Computers", identifier=identifier, primary=sole_section,
                )
            if mobile:
                _print_devices(
                    asyncio.run(self.fetch_mobile_devices(identifier)),
                    title="Mobile devices", identifier=identifier, primary=sole_section,
                )

        def _print_devices(result, title: str, identifier: str, primary: bool) -> None:
            if not result.ok:
                console.print(f"[red]{title} unavailable:[/red] {result.error}")
                if primary:
                    raise typer.Exit(code=1)
                return

            found = result.data["devices"]
            if not found:
                console.print(
                    f"[yellow]No {title.lower()} in Jamf assigned to[/yellow] {identifier}"
                )
                return

            # Six columns, not seven. Seven squeezed `model` to "Ma…" and
            # wrapped the device name over four lines at a stock 80-column
            # terminal -- the same squeeze that took the Okta device table
            # from seven columns to five, twice. `model` gives way because
            # it is the least actionable: the serial identifies the machine
            # uniquely and the OS says what it is. It stays in `data` for
            # JSON consumers.
            table = Table(title=f"{title} ({result.data['count']}) - {identifier}")
            table.add_column("name")
            # Serial is what an operator acts on, so it never wraps -- the
            # same call made for the Okta device table.
            table.add_column("serial", no_wrap=True)
            table.add_column("os", no_wrap=True)
            table.add_column("managed", no_wrap=True)
            # Two columns, deliberately, and neither gives way. A machine can
            # check in daily while its inventory is months stale; one "last
            # seen" column would hide exactly that.
            table.add_column("check-in", no_wrap=True)
            table.add_column("inventory", no_wrap=True)
            for device in found:
                managed = (
                    "[green]yes[/green]" if device["managed"] else "[red]unmanaged[/red]"
                )
                table.add_row(
                    device["name"] or "-",
                    device["serial_number"] or "-",
                    device["os"] or "-",
                    managed,
                    device["last_check_in"] or "-",
                    device["last_inventory"] or "-",
                )
            console.print(table)

        return sub_app
