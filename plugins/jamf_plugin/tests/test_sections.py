"""
Jamf OS, software and local-account sections.

Three things learned from the live fleet that shape this module:

**Applications duplicate heavily.** One machine reported 192 entries but
only 91 distinct name+version pairs -- including 102 copies of a single app
at paths like `/Applications/SomeVendor-57.localized/SomeVendor.app`,
`-69`, `-33`. A flat list would be 102 near-identical rows burying
everything else. Grouping by name+version and showing a count surfaces that
as the anomaly it is rather than hiding it in the noise.

**`uid >= 500` is the real test for a human account, not a `_` prefix.**
`root` (0), `daemon` (1) and `nobody` (-2) carry no underscore but are not
people. Sampled 402 uids across machines; all numeric, so the comparison is
safe.

**`-os` cannot be declared.** With `-o` and `-s` both taken it is already a
valid bundle meaning os+software, so declaring it as an OS alias would make
the same two letters mean different things depending on position -- the
trap documented in the CLI shape note. OS is `-o` / `--os` only.

Run just this stage:  pytest -m jamf
"""

from __future__ import annotations

import httpx
import pytest
import respx
from jamf_plugin.plugin import JamfPlugin, group_applications, is_human_account

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jamf

BASE = "https://example.jamfcloud.com"
TOKEN_URL = f"{BASE}/api/oauth/token"
COMPUTERS_URL = f"{BASE}/api/v1/computers-inventory"
CONFIG = PluginConfig({
    "JAMF_BASE_URL": BASE, "JAMF_CLIENT_ID": "id", "JAMF_CLIENT_SECRET": "secret"})


def _record(**sections):
    base = {"id": "1", "general": {"name": "CW-EXAMPLE001-L"},
            "operatingSystem": {"name": "macOS", "version": "26.6.2", "build": "25G83",
                                "activeDirectoryStatus": "Not Bound",
                                "fileVault2Status": "BOOT_ENCRYPTED"},
            "applications": [], "localUserAccounts": []}
    base.update(sections)
    return base


def _mock(records=None):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "t", "expires_in": 1200}))
    return respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(
        200, json={"totalCount": 1, "results": records if records is not None else [_record()]}))


def _app(name, version="1.0", path=None, size=10):
    return {"name": name, "version": version, "path": path or f"/Applications/{name}",
            "sizeMegabytes": size, "bundleId": f"com.example.{name}", "macAppStore": False,
            "updateAvailable": False}


def _account(username, uid, admin=False, fv2=False, home=100):
    return {"username": username, "uid": str(uid), "admin": admin,
            "fileVault2Enabled": fv2, "homeDirectorySizeMb": home,
            "fullName": username.title(), "userAccountType": "LOCAL"}


# --- Human vs system accounts ---------------------------------------------------


@pytest.mark.parametrize("uid", [501, 502, 1000])
def test_a_real_uid_is_a_human_account(uid):
    assert is_human_account(_account("someone", uid)) is True


@pytest.mark.parametrize(("username", "uid"), [("root", 0), ("daemon", 1), ("nobody", -2)])
def test_low_uids_are_system_even_without_an_underscore(username, uid):
    """`root`, `daemon` and `nobody` carry no underscore but are not people.
    Filtering on the prefix alone would list all three."""
    assert is_human_account(_account(username, uid)) is False


def test_underscore_service_accounts_are_system():
    assert is_human_account(_account("_spotlight", 89)) is False


def test_a_non_numeric_uid_is_treated_as_system():
    """Never seen live across 402 sampled uids, but a crash here would take
    out the whole section."""
    assert is_human_account({"username": "odd", "uid": "not-a-number"}) is False


def test_a_missing_uid_is_treated_as_system():
    assert is_human_account({"username": "odd"}) is False


# --- Grouping applications --------------------------------------------------------


def test_identical_apps_at_different_paths_collapse_to_one_row():
    """The live case: 102 copies of one app under numbered directories."""
    apps = [_app("SomeVendor.app", "26.31.1", f"/Applications/SomeVendor-{i}.localized/x.app")
            for i in range(102)]

    grouped = group_applications(apps)

    assert len(grouped) == 1
    assert grouped[0]["copies"] == 102
    assert grouped[0]["name"] == "SomeVendor.app"


def test_different_versions_stay_separate():
    """Two versions installed is a real finding, not duplication."""
    grouped = group_applications([_app("Zoom.app", "6.0"), _app("Zoom.app", "6.1")])

    assert len(grouped) == 2


def test_a_single_copy_reports_one():
    grouped = group_applications([_app("Safari.app", "26.6.2")])

    assert grouped[0]["copies"] == 1


def test_the_most_duplicated_app_sorts_first():
    """An app installed 102 times is the thing worth seeing, so it should not
    be buried alphabetically."""
    apps = [_app("Zeta.app")] + [_app("Alpha.app", path=f"/Applications/a{i}") for i in range(5)]

    grouped = group_applications(apps)

    assert grouped[0]["name"] == "Alpha.app"
    assert grouped[0]["copies"] == 5


def test_equally_common_apps_sort_by_name():
    grouped = group_applications([_app("Zeta.app"), _app("Alpha.app")])

    assert [g["name"] for g in grouped] == ["Alpha.app", "Zeta.app"]


def test_grouping_an_empty_list_is_empty():
    assert group_applications([]) == []


# --- OS ----------------------------------------------------------------------------


@respx.mock
async def test_os_details_are_returned():
    _mock()

    result = await JamfPlugin(CONFIG).fetch_os("jdoe")

    os_info = result.data["devices"][0]
    # `name` is the device name, as in every other shaper here; the OS's own
    # name is `os_name`.
    assert os_info["name"] == "CW-EXAMPLE001-L"
    assert os_info["os_name"] == "macOS"
    assert os_info["version"] == "26.6.2"
    assert os_info["build"] == "25G83"
    assert os_info["active_directory"] == "Not Bound"
    assert os_info["filevault_status"] == "BOOT_ENCRYPTED"


@respx.mock
async def test_the_os_section_is_requested():
    route = _mock()

    await JamfPlugin(CONFIG).fetch_os("jdoe")

    sections = {v for k, v in route.calls.last.request.url.params.multi_items() if k == "section"}
    assert "OPERATING_SYSTEM" in sections


# --- Software -----------------------------------------------------------------------


@respx.mock
async def test_software_is_grouped_and_counted():
    apps = [_app("Dup.app", path=f"/Applications/d{i}") for i in range(3)] + [_app("Solo.app")]
    _mock([_record(applications=apps)])

    result = await JamfPlugin(CONFIG).fetch_software("jdoe")

    grouped = result.data["devices"][0]["applications"]
    assert {g["name"]: g["copies"] for g in grouped} == {"Dup.app": 3, "Solo.app": 1}


@respx.mock
async def test_software_reports_both_totals():
    """Raw entry count and distinct count are different facts, and their gap
    is the duplication signal."""
    apps = [_app("Dup.app", path=f"/Applications/d{i}") for i in range(3)]
    _mock([_record(applications=apps)])

    data = (await JamfPlugin(CONFIG).fetch_software("jdoe")).data["devices"][0]

    assert data["total_entries"] == 3
    assert data["distinct_count"] == 1


@respx.mock
async def test_the_applications_section_is_requested():
    route = _mock()

    await JamfPlugin(CONFIG).fetch_software("jdoe")

    sections = {v for k, v in route.calls.last.request.url.params.multi_items() if k == "section"}
    assert "APPLICATIONS" in sections


# --- Local users -----------------------------------------------------------------------


@respx.mock
async def test_only_human_accounts_are_returned():
    accounts = [_account("root", 0, admin=True), _account("_spotlight", 89),
                _account("daemon", 1), _account("exampleuser", 502, fv2=True)]
    _mock([_record(localUserAccounts=accounts)])

    data = (await JamfPlugin(CONFIG).fetch_local_users("jdoe")).data["devices"][0]

    assert [a["username"] for a in data["accounts"]] == ["exampleuser"]


@respx.mock
async def test_the_hidden_system_account_count_is_reported():
    """Silently dropping 129 rows would be the kind of quiet omission this
    tool avoids; the count says what was filtered."""
    accounts = [_account("root", 0), _account("_x", 89), _account("exampleuser", 502)]
    _mock([_record(localUserAccounts=accounts)])

    data = (await JamfPlugin(CONFIG).fetch_local_users("jdoe")).data["devices"][0]

    assert data["system_accounts_hidden"] == 2


@respx.mock
async def test_admin_and_filevault_flags_are_carried():
    accounts = [_account("admin1", 501, admin=True), _account("user1", 502, fv2=True)]
    _mock([_record(localUserAccounts=accounts)])

    data = (await JamfPlugin(CONFIG).fetch_local_users("jdoe")).data["devices"][0]
    by_name = {a["username"]: a for a in data["accounts"]}

    assert by_name["admin1"]["admin"] is True
    assert by_name["user1"]["filevault_enabled"] is True


@respx.mock
async def test_a_machine_with_no_human_accounts_is_not_an_error():
    _mock([_record(localUserAccounts=[_account("root", 0)])])

    result = await JamfPlugin(CONFIG).fetch_local_users("jdoe")

    assert result.ok
    assert result.data["devices"][0]["accounts"] == []


# --- Mock mode -------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["fetch_os", "fetch_software", "fetch_local_users"])
async def test_mock_mode_covers_every_new_section(method):
    plugin = JamfPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"}))

    result = await getattr(plugin, method)("jdoe")

    assert result.ok
    assert result.data["devices"]
