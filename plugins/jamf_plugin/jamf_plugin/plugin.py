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

#: Numeric hardware fields Jamf leaves at 0 on Apple Silicon. They are not
#: "zero cores" -- they are Intel-era fields it never populates. Verified on a
#: live M3 Mac 2026-09-23. Rendering them as numbers would be confidently
#: wrong, so a 0 here is treated as absent.
_ZERO_MEANS_ABSENT = (
    "coreCount", "processorCount", "processorSpeedMhz", "busSpeedMhz",
    "cacheSizeKilobytes", "openRamSlots",
)


#: macOS reserves uids below 500 for system accounts. This is the real test
#: for "is this a person": `root` (0), `daemon` (1) and `nobody` (-2) carry no
#: underscore, so filtering on the prefix alone lists all three. Sampled 402
#: uids across the live fleet; every one was numeric.
MIN_HUMAN_UID = 500

#: Applications rows printed before the CLI says "N more". Grouping already
#: collapses duplicates, so this is about screen height rather than noise.
MAX_APPS_SHOWN = 30


def is_human_account(account: dict) -> bool:
    """True for a real user account rather than a system one."""
    raw = account.get("uid")
    try:
        return int(raw) >= MIN_HUMAN_UID
    except (TypeError, ValueError):
        # Never seen live, but a crash here would take out the whole section.
        return False


def group_applications(applications: list[dict]) -> list[dict]:
    """Collapse identical name+version pairs, counting the copies.

    One live machine reported 192 entries but only 91 distinct name+version
    pairs -- including 102 copies of a single app under numbered directories
    (`/Applications/SomeVendor-57.localized/...`, `-69`, `-33`). A flat list
    would be a hundred near-identical rows burying everything else, so the
    copies are counted instead. Most-duplicated first, because an app
    installed 102 times is the finding, not a footnote; ties break by name.

    Different versions stay separate -- two versions installed is a real
    observation, not duplication.
    """
    buckets: dict[tuple, dict] = {}
    for app in applications:
        key = (app.get("name"), app.get("version"))
        bucket = buckets.setdefault(key, {
            "name": app.get("name"), "version": app.get("version"),
            "size_megabytes": app.get("sizeMegabytes"),
            "mac_app_store": bool(app.get("macAppStore")),
            "update_available": bool(app.get("updateAvailable")),
            "copies": 0, "paths": [],
        })
        bucket["copies"] += 1
        if len(bucket["paths"]) < 3:      # enough to show where, not a dump
            bucket["paths"].append(app.get("path"))
    return sorted(buckets.values(), key=lambda b: (-b["copies"], (b["name"] or "").lower()))


def optional_number(value) -> int | None:
    """A count Jamf may simply not have populated.

    Zero is not a real answer for any of these -- no Mac has zero cores --
    so it is reported as absent rather than as a number someone might act on.
    """
    if value in (None, "", 0):
        return None
    return value


def format_ram(megabytes) -> str | None:
    """RAM in GB. 16384 MB is a number nobody thinks in."""
    if not megabytes:
        return None
    return f"{int(megabytes) // 1024} GB"


#: Re-exchange this many seconds before a token's stated expiry. Tokens here
#: live 59 seconds, so the margin is a meaningful fraction of the lifetime
#: rather than a rounding error.
TOKEN_REFRESH_MARGIN_SECONDS = 10


def build_user_filter(
    identifier: str,
    fields: tuple[str, ...] = COMPUTER_FILTER_FIELDS,
    domain: str | None = None,
) -> str:
    """RSQL filter matching `identifier` against every field it could be.

    An identifier is a username, a device name or a serial number, and the
    caller should not have to declare which. All are OR-ed into a single
    query rather than tried in sequence: a fallback chain costs a round trip
    per wrong guess, and its ordering would silently decide which record
    wins when a string matches two fields.

    **Bare usernames.** Jamf usernames are commonly email addresses -- 197
    of 197 on the live fleet, all one domain -- so `jamf dluo` matches
    nothing while `jamf dluo@example.com` works. When `domain` is configured
    and the identifier carries no `@`, the suffixed form is added as one
    more clause so both spellings resolve. Only the *username* field gets
    it: a device name or serial is never an email address, so suffixing
    those would add clauses that can never match.

    The domain is configuration rather than a constant -- it is org-specific
    and this plugin ships in a public repo.

    Filtering server-side matters regardless: this instance holds 3,461
    computers, and pulling the fleet to find one laptop would be slow and
    rude.

    `fields` differs by endpoint. That is not a guess: the mobile endpoint
    answers INVALID_FIELD for `userAndLocation.username` and enumerates what
    it will accept.
    """
    cleaned = (identifier or "").strip()
    if not cleaned:
        raise ValueError("jamf needs a username, device name or serial to look up.")

    def rsql(value: str) -> str:
        # Escape backslashes then quotes so a value cannot terminate the RSQL
        # string or inject a clause. Applied per-clause: one unescaped quote
        # would break all of them.
        return value.replace("\\", "\\\\").replace('"', '\\"')

    clauses = [f'{field}=="{rsql(cleaned)}"' for field in fields]

    suffix = (domain or "").strip().lstrip("@")
    if suffix and "@" not in cleaned:
        # fields[0] is the username field on both endpoints.
        clauses.append(f'{fields[0]}=="{rsql(f"{cleaned}@{suffix}")}"')

    return " or ".join(clauses)


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

    async def fetch_hardware(self, identifier: str) -> ConnectorResult:
        """Hardware detail for the machines matching `identifier`."""
        return await self._fetch_devices(
            identifier, self._call_computers_backend, self._to_hardware
        )

    async def fetch_os(self, identifier: str) -> ConnectorResult:
        """Operating system detail for the matching machines."""
        return await self._fetch_devices(
            identifier, self._call_os_backend, self._to_os
        )

    async def fetch_software(self, identifier: str) -> ConnectorResult:
        """Installed applications, grouped by name+version."""
        return await self._fetch_devices(
            identifier, self._call_software_backend, self._to_software
        )

    async def fetch_local_users(self, identifier: str) -> ConnectorResult:
        """Local accounts on the matching machines, system accounts filtered."""
        return await self._fetch_devices(
            identifier, self._call_accounts_backend, self._to_local_users
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
            raw = await backend(
                build_user_filter(identifier, fields, self.config.get("JAMF_USER_DOMAIN"))
            )
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

    async def _call_os_backend(self, user_filter: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_computers_fixture()
        return await self._sectioned_query(user_filter, ("GENERAL", "OPERATING_SYSTEM"))

    async def _call_software_backend(self, user_filter: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_computers_fixture()
        # APPLICATIONS is the heaviest section here -- ~42KB for one machine.
        # Requested only when asked for, never folded into the default view.
        return await self._sectioned_query(user_filter, ("GENERAL", "APPLICATIONS"))

    async def _call_accounts_backend(self, user_filter: str) -> list[dict]:
        if self.mock_mode:
            return self._mock_computers_fixture()
        return await self._sectioned_query(user_filter, ("GENERAL", "LOCAL_USER_ACCOUNTS"))

    async def _sectioned_query(self, user_filter: str, sections: tuple[str, ...]) -> list[dict]:
        params = [("filter", user_filter), ("page-size", str(PAGE_SIZE))]
        params += [("section", section) for section in sections]
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
            "hardware": {
                "serialNumber": "C02MOCK00001", "make": "Apple",
                "model": "MacBook Pro (16-inch, 2021)", "modelIdentifier": "MacBookPro18,3",
                "processorType": "Apple M1 Pro", "processorArchitecture": "arm64",
                "appleSilicon": True, "totalRamMegabytes": 16384,
                "batteryHealth": "NORMAL", "batteryCapacityPercent": 94,
                "macAddress": "00:00:5E:00:53:00", "bootRom": "10151.0.0",
                # The zeros a real Apple Silicon Mac reports -- the fixture
                # mirrors reality so the helper is actually exercised.
                "coreCount": 0, "processorCount": 0, "processorSpeedMhz": 0,
                "openRamSlots": 0,
            },
            "operatingSystem": {"name": "macOS", "version": "15.6.0"},
            "userAndLocation": {"username": "jdoe"},
            "operatingSystem": {"name": "macOS", "version": "15.6.0", "build": "24G84",
                                "activeDirectoryStatus": "Not Bound",
                                "fileVault2Status": "BOOT_ENCRYPTED"},
            # Duplicated on purpose: the live fleet does this, and a fixture
            # without it would never exercise the grouping.
            "applications": [
                {"name": "Mock Duplicated.app", "version": "1.0", "sizeMegabytes": 40,
                 "path": f"/Applications/Mock-{i}.localized/Mock Duplicated.app"}
                for i in range(3)
            ] + [{"name": "Safari.app", "version": "26.6.2", "sizeMegabytes": 20,
                  "path": "/Applications/Safari.app"}],
            "localUserAccounts": [
                {"username": "root", "uid": "0", "admin": True},
                {"username": "_spotlight", "uid": "89"},
                {"username": "mockuser", "uid": "502", "admin": False,
                 "fileVault2Enabled": True, "homeDirectorySizeMb": 29696,
                 "fullName": "Mock User", "userAccountType": "LOCAL"},
            ],
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
    def _to_os(entry: dict) -> dict:
        os_info = entry.get("operatingSystem") or {}
        general = entry.get("general") or {}
        return {
            "name": general.get("name"),
            "os_name": os_info.get("name"),
            "version": os_info.get("version"),
            "build": os_info.get("build"),
            "supplemental_build": os_info.get("supplementalBuildVersion"),
            "rapid_security_response": os_info.get("rapidSecurityResponse"),
            "active_directory": os_info.get("activeDirectoryStatus"),
            # FileVault appears here as well as in DISK_ENCRYPTION; this is
            # the boot-volume state specifically.
            "filevault_status": os_info.get("fileVault2Status"),
        }

    @staticmethod
    def _to_software(entry: dict) -> dict:
        applications = entry.get("applications") or []
        grouped = group_applications(applications)
        return {
            "name": (entry.get("general") or {}).get("name"),
            "applications": grouped,
            # Two different facts. The gap between them IS the duplication
            # signal, so neither is derived from the other at render time.
            "total_entries": len(applications),
            "distinct_count": len(grouped),
        }

    @staticmethod
    def _to_local_users(entry: dict) -> dict:
        accounts = entry.get("localUserAccounts") or []
        humans = [a for a in accounts if is_human_account(a)]
        return {
            "name": (entry.get("general") or {}).get("name"),
            "accounts": [{
                "username": a.get("username"),
                "full_name": a.get("fullName"),
                "uid": a.get("uid"),
                "admin": bool(a.get("admin")),
                "filevault_enabled": bool(a.get("fileVault2Enabled")),
                "home_size_mb": a.get("homeDirectorySizeMb"),
                "account_type": a.get("userAccountType"),
            } for a in humans],
            # Said out loud rather than silently dropped: one live machine
            # had 134 accounts of which 129 were system.
            "system_accounts_hidden": len(accounts) - len(humans),
        }

    @classmethod
    def _to_hardware(cls, entry: dict) -> dict:
        hardware = entry.get("hardware") or {}
        general = entry.get("general") or {}
        return {
            "name": general.get("name"),
            "make": hardware.get("make"),
            "model": hardware.get("model"),
            "model_identifier": hardware.get("modelIdentifier"),
            "serial_number": hardware.get("serialNumber"),
            "chip": hardware.get("processorType"),
            "architecture": hardware.get("processorArchitecture"),
            "apple_silicon": hardware.get("appleSilicon"),
            "memory": format_ram(hardware.get("totalRamMegabytes")),
            # See _ZERO_MEANS_ABSENT: 0 here means "not populated", not "none".
            "core_count": optional_number(hardware.get("coreCount")),
            "processor_speed_mhz": optional_number(hardware.get("processorSpeedMhz")),
            "open_ram_slots": optional_number(hardware.get("openRamSlots")),
            "battery_health": hardware.get("batteryHealth"),
            "battery_capacity_percent": optional_number(hardware.get("batteryCapacityPercent")),
            "mac_address": hardware.get("macAddress"),
            "boot_rom": hardware.get("bootRom"),
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
            hardware: bool = typer.Option(
                False, "--hardware", "-hardware", "-w",
                help="Hardware detail: model, chip, memory, battery. Short form "
                "is -w (from hardWare); -h is deliberately left free for help.",
            ),
            os_detail: bool = typer.Option(
                False, "--os", "-o",
                help="Operating system detail: version, build, FileVault boot "
                "state, AD binding. No -os spelling: that is already the "
                "bundle -o + -s (os + software).",
            ),
            software: bool = typer.Option(
                False, "--software", "-software", "-s",
                help="Installed applications, grouped by name and version with "
                "a copy count. The heaviest section -- opt-in only.",
            ),
            users: bool = typer.Option(
                False, "--users", "-users", "-u",
                help="Local user accounts (uid >= 500). System accounts are "
                "filtered out and counted rather than listed.",
            ),
            mobile: bool = typer.Option(
                False, "--mobile", "-mobile", "-m",
                help="Mobile devices (phones, tablets) assigned to this user.",
            ),
        ) -> None:
            """Look one person's Jamf-managed hardware up."""
            extras = (mobile, hardware, os_detail, software, users)
            show_computers = devices or not any(extras)
            sole_section = sum((show_computers, *extras)) == 1

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
            if hardware:
                _print_hardware(
                    asyncio.run(self.fetch_hardware(identifier)),
                    identifier=identifier, primary=sole_section,
                )
            if os_detail:
                _print_os(asyncio.run(self.fetch_os(identifier)),
                          identifier=identifier, primary=sole_section)
            if software:
                _print_software(asyncio.run(self.fetch_software(identifier)),
                                identifier=identifier, primary=sole_section)
            if users:
                _print_local_users(asyncio.run(self.fetch_local_users(identifier)),
                                   identifier=identifier, primary=sole_section)

        def _section_guard(result, label: str, identifier: str, primary: bool):
            """Shared failure/empty handling. Returns the devices, or None if
            the caller should stop."""
            if not result.ok:
                console.print(f"[red]{label} unavailable:[/red] {result.error}")
                if primary:
                    raise typer.Exit(code=1)
                return None
            found = result.data["devices"]
            if not found:
                console.print(f"[yellow]No Jamf record for[/yellow] {identifier}")
                return None
            return found

        def _print_os(result, identifier: str, primary: bool) -> None:
            found = _section_guard(result, "Operating system", identifier, primary)
            if found is None:
                return
            for device in found:
                table = Table(
                    title=f"Operating system - {device['name'] or identifier}",
                    show_header=False,
                )
                table.add_column("field", no_wrap=True)
                table.add_column("value")
                for label, value in (
                    ("os", " ".join(p for p in (device["os_name"], device["version"]) if p)),
                    ("build", device["build"]),
                    ("supplemental build", device["supplemental_build"]),
                    ("rapid security response", device["rapid_security_response"]),
                    ("FileVault (boot volume)", device["filevault_status"]),
                    ("Active Directory", device["active_directory"]),
                ):
                    table.add_row(label, str(value) if value else "-")
                console.print(table)

        def _print_software(result, identifier: str, primary: bool) -> None:
            found = _section_guard(result, "Applications", identifier, primary)
            if found is None:
                return
            for device in found:
                apps = device["applications"]
                if not apps:
                    console.print(f"[yellow]No applications recorded for[/yellow] {identifier}")
                    continue
                shown = apps[:MAX_APPS_SHOWN]
                # Both totals in the title: their gap is the duplication
                # signal, and stating only one would hide it.
                title = (f"Applications - {device['name'] or identifier} "
                         f"({device['distinct_count']} distinct, "
                         f"{device['total_entries']} installed)")
                table = Table(title=title)
                table.add_column("application")
                table.add_column("version", no_wrap=True)
                table.add_column("copies", no_wrap=True, justify="right")
                table.add_column("size", no_wrap=True, justify="right")
                for app in shown:
                    copies = app["copies"]
                    # Anything installed more than once is worth a second
                    # look -- one machine had the same app 102 times.
                    rendered = f"[yellow]{copies}[/yellow]" if copies > 1 else str(copies)
                    size = f"{app['size_megabytes']} MB" if app["size_megabytes"] else "-"
                    table.add_row(app["name"] or "-", app["version"] or "-", rendered, size)
                console.print(table)
                if len(apps) > len(shown):
                    console.print(
                        f"[yellow]{len(apps) - len(shown)} more not shown[/yellow]"
                    )

        def _print_local_users(result, identifier: str, primary: bool) -> None:
            found = _section_guard(result, "Local accounts", identifier, primary)
            if found is None:
                return
            for device in found:
                accounts = device["accounts"]
                hidden = device["system_accounts_hidden"]
                if not accounts:
                    console.print(
                        f"[yellow]No local user accounts on[/yellow] "
                        f"{device['name'] or identifier} "
                        f"[dim]({hidden} system accounts hidden)[/dim]"
                    )
                    continue
                table = Table(title=f"Local accounts - {device['name'] or identifier}")
                table.add_column("username", no_wrap=True)
                table.add_column("full name")
                table.add_column("uid", no_wrap=True, justify="right")
                table.add_column("admin", no_wrap=True)
                table.add_column("FileVault", no_wrap=True)
                table.add_column("home", no_wrap=True, justify="right")
                for account in accounts:
                    home = (f"{account['home_size_mb'] // 1024} GB"
                            if account["home_size_mb"] and account["home_size_mb"] >= 1024
                            else (f"{account['home_size_mb']} MB"
                                  if account["home_size_mb"] else "-"))
                    table.add_row(
                        account["username"] or "-",
                        account["full_name"] or "-",
                        str(account["uid"] or "-"),
                        "[yellow]admin[/yellow]" if account["admin"] else "no",
                        "[green]yes[/green]" if account["filevault_enabled"] else "no",
                        home,
                    )
                console.print(table)
                # Said out loud: one live machine hid 129 of 134 accounts.
                console.print(f"[dim]{hidden} system accounts hidden (uid < {MIN_HUMAN_UID})[/dim]")

        def _print_hardware(result, identifier: str, primary: bool) -> None:
            if not result.ok:
                console.print(f"[red]Hardware unavailable:[/red] {result.error}")
                if primary:
                    raise typer.Exit(code=1)
                return

            found = result.data["devices"]
            if not found:
                console.print(f"[yellow]No Jamf hardware record for[/yellow] {identifier}")
                return

            for device in found:
                # Vertical rather than a wide row: there are a dozen fields
                # and model alone runs to 40 characters.
                table = Table(
                    title=f"Hardware - {device['name'] or device['serial_number'] or identifier}",
                    show_header=False,
                )
                table.add_column("field", no_wrap=True)
                table.add_column("value")
                chip = " ".join(
                    part for part in (device["chip"], f"({device['architecture']})"
                                      if device["architecture"] else None) if part
                )
                rows = [
                    ("model", device["model"]),
                    ("model id", device["model_identifier"]),
                    ("serial", device["serial_number"]),
                    ("chip", chip or None),
                    # None here means Jamf did not populate it -- see
                    # optional_number. On Apple Silicon that is every one of
                    # the Intel-era counters.
                    ("cores", device["core_count"]),
                    ("cpu MHz", device["processor_speed_mhz"]),
                    ("memory", device["memory"]),
                    ("free RAM slots", device["open_ram_slots"]),
                    ("battery health", device["battery_health"]),
                    ("battery capacity", f"{device['battery_capacity_percent']}%"
                     if device["battery_capacity_percent"] else None),
                    ("MAC address", device["mac_address"]),
                    ("boot ROM", device["boot_rom"]),
                ]
                for label, value in rows:
                    table.add_row(label, str(value) if value is not None else "-")
                console.print(table)

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

            # `model` was requested back into this view (2026-09-23) and
            # `name` gives way to make room. Measured on the live fleet:
            # model runs 31-40 characters while names are uniformly 15 and
            # shaped `CW-<serial>-L` -- the serial is already its own column,
            # so `name` was carrying no information model does not. Name
            # stays in `data` for JSON consumers.
            table = Table(title=f"{title} ({result.data['count']}) - {identifier}")
            # Serial is what an operator acts on, so it never wraps -- the
            # same call made for the Okta device table.
            table.add_column("serial", no_wrap=True)
            table.add_column("model")
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
                    device["serial_number"] or "-",
                    device.get("model") or "-",
                    device["os"] or "-",
                    managed,
                    device["last_check_in"] or "-",
                    device["last_inventory"] or "-",
                )
            console.print(table)

        return sub_app
