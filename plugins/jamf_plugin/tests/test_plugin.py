"""
Stage 4: Jamf Pro connector -- the authoritative hardware inventory.

Scope distinction worth repeating to users: Okta's `-d` lists its *device
registry* (machines enrolled through Okta Verify / device trust). Jamf is
the actual managed-hardware inventory. The two legitimately disagree -- a
laptop can be in Jamf and unknown to Okta, or the reverse -- and that
disagreement is itself useful. Neither is "wrong".

**Auth is a token exchange, not a static key.** Jamf Pro's current method
is an API Client: POST client_id/client_secret to `/api/oauth/token` and
get a short-lived bearer token back (~20 minutes). The token is fetched
once per run and reused, because a lookup that shows both computers and
mobile devices would otherwise pay for two exchanges.

**`lastContactTime` and `lastReportDate` are different things** and this
module never conflates them. The first is when the device last checked in;
the second is when it last submitted a full inventory. A machine can check
in daily while its inventory is months stale, so reporting either as "last
seen" would answer a question nobody asked. Same class of trap as Okta's
device `lastUpdated`, which is why both are carried under their own names.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m jamf
"""

from __future__ import annotations

import httpx
import pytest
import respx
from jamf_plugin.plugin import MOBILE_FILTER_FIELDS, JamfPlugin, build_user_filter

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jamf

BASE = "https://example.jamfcloud.com"
TOKEN_URL = f"{BASE}/api/oauth/token"
COMPUTERS_URL = f"{BASE}/api/v1/computers-inventory"
MOBILE_URL = f"{BASE}/api/v2/mobile-devices/detail"

CONFIG = PluginConfig({
    "JAMF_BASE_URL": BASE,
    "JAMF_CLIENT_ID": "not-a-real-client-id",
    "JAMF_CLIENT_SECRET": "not-a-real-secret",
})


def _token(expires_in: int = 1200) -> dict:
    return {"access_token": "fake-bearer-token", "expires_in": expires_in, "token_type": "Bearer"}


def _computer(
    name="Jane's MacBook Pro",
    serial="C02XYZ123ABC",
    username="jdoe",
    managed=True,
    last_contact="2026-09-20T08:15:00.000Z",
    last_report="2026-09-19T03:00:00.000Z",
):
    return {
        "id": "101",
        "general": {
            "name": name,
            "lastContactTime": last_contact,
            "reportDate": last_report,
            "remoteManagement": {"managed": managed},
            "supervised": True,
        },
        "hardware": {
            "serialNumber": serial,
            "model": "MacBook Pro (16-inch, 2021)",
            "modelIdentifier": "MacBookPro18,3",
        },
        "operatingSystem": {"name": "macOS", "version": "15.6.0", "build": "24G84"},
        "userAndLocation": {
            "username": username,
            "realname": "Jane Doe",
            "email": "jdoe@example.com",
            "position": "Engineer",
        },
        "diskEncryption": {"individualRecoveryKeyValidityStatus": "VALID"},
    }


def _page(results, total=None):
    return {"totalCount": total if total is not None else len(results), "results": results}


def _plugin(config: PluginConfig = CONFIG) -> JamfPlugin:
    return JamfPlugin(config)


def _mock_token(**kw):
    return respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=_token(**kw)))


# --- Contract ----------------------------------------------------------------


def test_required_credentials_are_declared():
    assert set(JamfPlugin.required_credentials) == {
        "JAMF_BASE_URL", "JAMF_CLIENT_ID", "JAMF_CLIENT_SECRET"
    }


# --- The RSQL filter ----------------------------------------------------------
#
# An identifier can be any of three things and the CLI should not care which:
# a username, a device name, or a serial number. Reported 2026-09-23 --
# `jamf CW-EXAMPLE001-L` returned nothing because only the username field was
# searched, though the machine plainly existed under `general.name`.
#
# RSQL `or` means one request covers all three, so there is no shape-guessing
# and no fallback chain. Verified live: a device name, a bare serial and a
# real username each return exactly one match through the same filter.


def test_the_filter_searches_username_name_and_serial():
    f = build_user_filter("jdoe")

    assert 'userAndLocation.username=="jdoe"' in f
    assert 'general.name=="jdoe"' in f
    assert 'hardware.serialNumber=="jdoe"' in f


def test_the_three_clauses_are_ored_not_anded():
    """AND would require the string to be all three at once, matching nothing."""
    f = build_user_filter("jdoe")

    assert " or " in f
    assert " and " not in f


def test_the_mobile_field_set_is_different():
    """Mobile rejects `userAndLocation.username` with INVALID_FIELD and uses
    flat `username`; it also names the device `displayName`, not `name`."""
    f = build_user_filter("jdoe", MOBILE_FILTER_FIELDS)

    assert 'username=="jdoe"' in f
    assert "userAndLocation" not in f
    assert 'displayName=="jdoe"' in f
    assert 'serialNumber=="jdoe"' in f


def test_filter_escapes_embedded_quotes():
    """A quote would otherwise terminate the RSQL string -- and now there are
    three clauses, so an unescaped one breaks all of them."""
    f = build_user_filter('jd"oe')

    assert '\\"' in f
    # One escaped quote per clause, three clauses. Escaping only the first
    # would leave the other two able to terminate the RSQL string.
    assert f.count('\\"') == len(("userAndLocation.username", "general.name", "hardware.serialNumber"))


def test_filter_trims_surrounding_whitespace():
    assert build_user_filter("  jdoe  ") == build_user_filter("jdoe")


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_identifier_is_rejected(blank):
    with pytest.raises(ValueError):
        build_user_filter(blank)


# --- Bare usernames ---------------------------------------------------------
#
# Usernames in a Jamf inventory are commonly email addresses -- 197 of 197 on
# the live fleet, all one domain -- so `jamf dluo` matches nothing while
# `jamf dluo@example.com` works. JAMF_USER_DOMAIN adds the suffixed form as an
# extra clause so both spellings resolve.
#
# The domain is configuration, never hardcoded: it is org-specific, and this
# plugin ships to a public repo.


def test_a_bare_username_gains_a_suffixed_clause():
    f = build_user_filter("dluo", domain="example.com")

    assert 'userAndLocation.username=="dluo"' in f
    assert 'userAndLocation.username=="dluo@example.com"' in f


def test_only_the_username_field_is_suffixed():
    """A device name or serial is never an email address, so suffixing those
    would add two clauses that can never match."""
    f = build_user_filter("dluo", domain="example.com")

    assert 'general.name=="dluo@example.com"' not in f
    assert 'hardware.serialNumber=="dluo@example.com"' not in f


def test_an_identifier_that_already_has_an_at_is_not_suffixed():
    """`dluo@example.com@example.com` would match nothing and look absurd."""
    f = build_user_filter("dluo@example.com", domain="example.com")

    assert "@example.com@" not in f, "must not double-suffix"
    # The identifier already contains the domain, so it appears in all three
    # base clauses -- what matters is that no fourth clause was added.
    assert f == build_user_filter("dluo@example.com", domain=None)
    assert f.count(" or ") == 2


def test_no_domain_configured_means_no_extra_clause():
    """Unset is the default, and the plugin must behave exactly as before."""
    f = build_user_filter("dluo")

    assert f == build_user_filter("dluo", domain=None)
    assert "@" not in f


def test_a_domain_written_with_a_leading_at_is_accepted():
    """`@example.com` is the natural way to write it in a .env file."""
    assert build_user_filter("dluo", domain="@example.com") == \
           build_user_filter("dluo", domain="example.com")


def test_a_blank_domain_is_treated_as_unset():
    assert build_user_filter("dluo", domain="   ") == build_user_filter("dluo")


def test_the_suffixed_clause_is_escaped_too():
    f = build_user_filter('d"luo', domain="example.com")

    assert '\\"' in f
    # three base clauses plus the suffixed username clause
    assert f.count('\\"') == 4


def test_the_mobile_field_set_also_suffixes_only_its_username_field():
    f = build_user_filter("dluo", MOBILE_FILTER_FIELDS, domain="example.com")

    assert 'username=="dluo@example.com"' in f
    assert 'displayName=="dluo@example.com"' not in f
    assert 'serialNumber=="dluo@example.com"' not in f


@respx.mock
async def test_a_bare_username_reaches_the_api_with_both_spellings():
    config = PluginConfig({
        "JAMF_BASE_URL": BASE, "JAMF_CLIENT_ID": "id", "JAMF_CLIENT_SECRET": "s",
        "JAMF_USER_DOMAIN": "example.com"})
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await JamfPlugin(config).fetch("dluo")

    f = route.calls.last.request.url.params["filter"]
    assert 'userAndLocation.username=="dluo"' in f
    assert 'userAndLocation.username=="dluo@example.com"' in f


@respx.mock
async def test_without_the_setting_nothing_changes():
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("dluo")

    assert "@" not in route.calls.last.request.url.params["filter"]


@respx.mock
async def test_a_device_name_finds_the_machine():
    """The reported bug. `CW-EXAMPLE001-L` is a device name, not a user."""
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(
        return_value=httpx.Response(200, json=_page([_computer(name="CW-EXAMPLE001-L")])))

    result = await _plugin().fetch("CW-EXAMPLE001-L")

    assert result.data["count"] == 1
    assert 'general.name=="CW-EXAMPLE001-L"' in route.calls.last.request.url.params["filter"]


@respx.mock
async def test_a_bare_serial_finds_the_machine():
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(
        return_value=httpx.Response(200, json=_page([_computer(serial="EXAMPLE001")])))

    result = await _plugin().fetch("EXAMPLE001")

    assert result.data["count"] == 1
    assert 'hardware.serialNumber=="EXAMPLE001"' in route.calls.last.request.url.params["filter"]


@respx.mock
async def test_a_username_still_finds_their_machines():
    """The original behaviour must not regress."""
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([_computer()])))

    await _plugin().fetch("jdoe@example.com")

    assert 'userAndLocation.username=="jdoe@example.com"' in route.calls.last.request.url.params["filter"]


@respx.mock
async def test_one_request_covers_all_three_kinds():
    """No fallback chain: guessing wrong would cost a round trip per guess,
    and the order of guesses would decide ambiguous cases arbitrarily."""
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("CW-EXAMPLE001-L")

    assert route.call_count == 1


# --- Auth ---------------------------------------------------------------------


@respx.mock
async def test_the_client_credentials_are_exchanged_for_a_bearer_token():
    route = _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    body = route.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=not-a-real-client-id" in body


@respx.mock
async def test_the_bearer_token_is_used_on_the_inventory_call():
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    assert route.calls.last.request.headers["Authorization"] == "Bearer fake-bearer-token"


@respx.mock
async def test_the_token_is_fetched_once_and_reused_across_sections():
    """Showing computers and mobile devices must not pay for two exchanges."""
    token = _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([])))

    plugin = _plugin()
    await plugin.fetch("jdoe")
    await plugin.fetch_mobile_devices("jdoe")

    assert token.call_count == 1


@respx.mock
async def test_a_rejected_client_credential_is_an_actionable_error():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

    result = await _plugin().fetch("jdoe")

    assert not result.ok
    assert "client" in result.error.lower() or "credential" in result.error.lower()


@respx.mock
async def test_the_client_secret_never_appears_in_an_error():
    secret = "s3cr3t-jamf-client-secret"
    config = PluginConfig({
        "JAMF_BASE_URL": BASE, "JAMF_CLIENT_ID": "id", "JAMF_CLIENT_SECRET": secret})
    respx.post(TOKEN_URL).mock(side_effect=httpx.HTTPError(f"failed sending {secret}"))

    result = await _plugin(config).fetch("jdoe")

    assert secret not in result.error


# --- Field names verified against a live instance -----------------------------


@respx.mock
async def test_the_inventory_date_uses_the_field_jamf_actually_sends():
    """Live check 2026-09-21: the GENERAL section sends `reportDate`, not
    `lastReportDate`. The mock fixtures had the documented-looking name, so
    every test passed while the column would have been permanently "-"
    against a real instance."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([{
        "id": "1",
        "general": {"name": "Mac", "lastContactTime": "2026-09-21T17:34:00.000Z",
                    "reportDate": "2026-09-21T17:31:00.000Z",
                    "remoteManagement": {"managed": True}},
    }])))

    device = (await _plugin().fetch("jdoe")).data["devices"][0]

    assert device["last_inventory"] == "2026-09-21"
    assert device["last_check_in"] == "2026-09-21"


@respx.mock
async def test_a_record_without_an_inventory_date_renders_as_absent():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([{
        "id": "1", "general": {"name": "Mac", "remoteManagement": {"managed": True}}}])))

    device = (await _plugin().fetch("jdoe")).data["devices"][0]

    assert device["last_inventory"] is None


# --- Token lifetime -------------------------------------------------------------


@respx.mock
async def test_an_expired_token_is_re_exchanged(monkeypatch):
    """Live check 2026-09-21: this instance issues tokens with
    `expires_in: 59`, not the ~20 minutes the docs imply. Caching for the
    life of the process would hand a dead token to the second call."""
    import jamf_plugin.plugin as mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token(expires_in=59)))
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    plugin = _plugin()
    await plugin.fetch("jdoe")
    clock["t"] += 120                      # token has expired
    await plugin.fetch("jdoe")

    assert token.call_count == 2, "a stale token must be re-exchanged"


@respx.mock
async def test_a_live_token_is_reused(monkeypatch):
    import jamf_plugin.plugin as mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token(expires_in=1200)))
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    plugin = _plugin()
    await plugin.fetch("jdoe")
    clock["t"] += 5
    await plugin.fetch("jdoe")

    assert token.call_count == 1


@respx.mock
async def test_a_token_close_to_expiry_is_refreshed_early(monkeypatch):
    """A 59-second token can die mid-request. Refreshing only once it is
    already dead would surface as a random 401 on the second section."""
    import jamf_plugin.plugin as mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    token = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json=_token(expires_in=59)))
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    plugin = _plugin()
    await plugin.fetch("jdoe")
    clock["t"] += 55                       # inside the safety margin, not yet expired
    await plugin.fetch("jdoe")

    assert token.call_count == 2


# --- Computers ------------------------------------------------------------------


@respx.mock
async def test_computers_are_returned_normalised():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([_computer()])))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["count"] == 1
    device = result.data["devices"][0]
    assert device["name"] == "Jane's MacBook Pro"
    assert device["serial_number"] == "C02XYZ123ABC"
    assert device["model"] == "MacBook Pro (16-inch, 2021)"
    assert device["os"] == "macOS 15.6.0"
    assert device["managed"] is True


@respx.mock
async def test_check_in_and_inventory_dates_are_kept_separate():
    """A machine can check in daily while its inventory is months stale.
    Reporting either as "last seen" would answer a question nobody asked."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([
        _computer(last_contact="2026-09-20T08:15:00.000Z", last_report="2026-06-01T03:00:00.000Z")
    ])))

    device = (await _plugin().fetch("jdoe")).data["devices"][0]

    assert device["last_check_in"] == "2026-09-20"
    assert device["last_inventory"] == "2026-06-01"


@respx.mock
async def test_an_unmanaged_device_is_still_listed():
    """Unmanaged-but-present is a real state, and arguably the interesting
    one -- Jamf knows about the machine but cannot act on it."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(
        return_value=httpx.Response(200, json=_page([_computer(managed=False)])))

    device = (await _plugin().fetch("jdoe")).data["devices"][0]

    assert device["managed"] is False


@respx.mock
async def test_a_user_with_no_devices_is_a_success():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["devices"] == []
    assert "no-devices" in result.tags


@respx.mock
async def test_missing_sections_do_not_crash():
    """Jamf omits a section entirely when it has nothing for it."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(
        return_value=httpx.Response(200, json=_page([{"id": "1", "general": {"name": "Bare"}}])))

    result = await _plugin().fetch("jdoe")

    assert result.ok
    assert result.data["devices"][0]["serial_number"] is None


@respx.mock
async def test_several_devices_are_all_returned():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([
        _computer(name="MacBook", serial="AAA111"),
        _computer(name="iMac", serial="BBB222"),
    ])))

    result = await _plugin().fetch("jdoe")

    assert {d["serial_number"] for d in result.data["devices"]} == {"AAA111", "BBB222"}


# --- Request shape ----------------------------------------------------------------


@respx.mock
async def test_the_query_is_filtered_to_the_user_server_side():
    """Jamf inventories run to thousands of machines; filtering client-side
    would pull the whole fleet to show one person's laptop."""
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    assert 'userAndLocation.username=="jdoe"' in route.calls.last.request.url.params["filter"]


@respx.mock
async def test_only_the_sections_we_render_are_requested():
    """A full inventory record carries applications, fonts, plugins, local
    accounts and certificates. Everything in `data` reaches the plaintext
    cache, so unrendered sections are not asked for."""
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    sections = route.calls.last.request.url.params.multi_items()
    requested = {v for k, v in sections if k == "section"}
    assert "HARDWARE" in requested
    assert "APPLICATIONS" not in requested
    assert "FONTS" not in requested


# --- Failure modes -------------------------------------------------------------------


@respx.mock
async def test_forbidden_on_inventory_is_an_actionable_error():
    """An API Role can authenticate yet lack Read Computers."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch("jdoe")

    assert not result.ok
    assert "read" in result.error.lower() or "privilege" in result.error.lower()


@respx.mock
async def test_timeout_is_an_error_result():
    _mock_token()
    respx.get(COMPUTERS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch("jdoe")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await JamfPlugin(PluginConfig({})).fetch("jdoe")

    assert not result.ok
    assert "JAMF_BASE_URL" in result.error


@respx.mock
async def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    config = PluginConfig({
        "JAMF_BASE_URL": f"{BASE}/", "JAMF_CLIENT_ID": "i", "JAMF_CLIENT_SECRET": "s"})
    _mock_token()
    route = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin(config).fetch("jdoe")

    assert route.called


# --- Mobile devices ---------------------------------------------------------------


@respx.mock
async def test_mobile_devices_are_a_separate_call():
    """Most lookups want laptops. Phones cost another request, so they are
    opt-in rather than folded into the default view."""
    _mock_token()
    computers = respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([])))
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("jdoe")

    assert computers.called


def _mobile_record():
    """The shape the live endpoint actually returns.

    Verified 2026-09-21. Section-nested exactly like a computer record, NOT
    the flat object the first implementation assumed -- every field except
    the id was being read from the wrong place, and the mocks agreed with
    the mistake.
    """
    return {
        "mobileDeviceId": "55",
        "general": {
            "displayName": "Jane's iPhone",
            "osVersion": "18.2",
            "managed": True,
            "lastContactDate": "2026-09-19T12:00:00.000Z",
            "lastInventoryUpdateDate": "2026-09-18T10:00:00.000Z",
        },
        "hardware": {"serialNumber": "F2LXYZ", "model": "iPhone 15 Pro"},
        "userAndLocation": {"username": "jdoe", "emailAddress": "jdoe@example.com"},
    }


@respx.mock
async def test_mobile_devices_are_returned_normalised():
    _mock_token()
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([_mobile_record()])))

    result = await _plugin().fetch_mobile_devices("jdoe")

    assert result.ok
    device = result.data["devices"][0]
    assert device["name"] == "Jane's iPhone"
    assert device["serial_number"] == "F2LXYZ"
    assert device["model"] == "iPhone 15 Pro"
    assert device["os"] == "18.2"
    assert device["managed"] is True
    assert device["assigned_to"] == "jdoe"


@respx.mock
async def test_mobile_dates_come_from_their_own_fields():
    """`lastContactDate` here, not `lastContactTime` as on computers."""
    _mock_token()
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([_mobile_record()])))

    device = (await _plugin().fetch_mobile_devices("jdoe")).data["devices"][0]

    assert device["last_check_in"] == "2026-09-19"
    assert device["last_inventory"] == "2026-09-18"


@respx.mock
async def test_mobile_sections_must_be_requested_or_the_record_is_hollow():
    """Live: `hardware` and `userAndLocation` come back null unless asked
    for, so serial, model and the assigned user were all silently absent."""
    _mock_token()
    route = respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch_mobile_devices("jdoe")

    requested = {v for k, v in route.calls.last.request.url.params.multi_items() if k == "section"}
    assert {"GENERAL", "HARDWARE", "USER_AND_LOCATION"} <= requested


@respx.mock
async def test_mobile_filters_on_the_flat_username_field():
    """The mobile endpoint rejects `userAndLocation.username` outright with
    INVALID_FIELD -- it only accepts flat `username`. Computers want the
    dotted path. The two endpoints genuinely differ."""
    _mock_token()
    route = respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch_mobile_devices("jdoe")

    f = route.calls.last.request.url.params["filter"]
    assert 'username=="jdoe"' in f
    assert "userAndLocation" not in f, "mobile rejects the dotted path with INVALID_FIELD"


@respx.mock
async def test_a_rejected_filter_field_is_an_actionable_error():
    """Jamf answers 400 INVALID_FIELD. The raw httpx message leaks the whole
    URL and says nothing useful."""
    _mock_token()
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(400, json={
        "httpStatus": 400,
        "errors": [{"code": "INVALID_FIELD", "description": "Cannot filter by field [x]"}]}))

    result = await _plugin().fetch_mobile_devices("jdoe")

    assert not result.ok
    assert "filter" in result.error.lower()
    assert "https://" not in result.error, "must not echo the raw URL back"


# --- Personal data ------------------------------------------------------------------


@respx.mock
async def test_the_assigned_username_is_carried_but_not_their_contact_details():
    """`userAndLocation` also carries realname, email and position. The
    username is needed to confirm the device is attributed to the right
    person; the rest is not needed to answer "what hardware do they have",
    and `data` reaches the plaintext local cache."""
    _mock_token()
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json=_page([_computer()])))

    result = await _plugin().fetch("jdoe")

    assert result.data["devices"][0]["assigned_to"] == "jdoe"
    blob = repr(result.data)
    assert "jdoe@example.com" not in blob
    assert "Engineer" not in blob


# --- Mock mode -------------------------------------------------------------------------


async def test_mock_mode_works_with_no_credentials():
    plugin = JamfPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"}))

    result = await plugin.fetch("jdoe")

    assert result.ok
    assert result.data["devices"]
    assert result.data["devices"][0]["serial_number"]


async def test_mock_mode_populates_the_same_fields_the_real_api_does():
    """The mock fixture drifting from the real payload is how the
    `lastReportDate` bug survived: every test passed against a fixture that
    used a field name the API does not send. A fixture that cannot produce
    a populated inventory date is not standing in for anything."""
    plugin = JamfPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"}))

    device = (await plugin.fetch("jdoe")).data["devices"][0]

    assert device["last_check_in"], "mock must populate check-in"
    assert device["last_inventory"], "mock must populate inventory date"
    assert device["serial_number"]
    assert device["os"]


async def test_mock_mode_covers_mobile_devices_too():
    plugin = JamfPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"}))

    result = await plugin.fetch_mobile_devices("jdoe")

    assert result.ok
    assert result.data["devices"]
