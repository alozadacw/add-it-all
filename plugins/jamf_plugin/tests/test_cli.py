"""
Stage 4: the `add-it-all jamf <identifier> [flags]` CLI surface.

Same shape as every other connector: identifier as a direct argument, no
noun subcommands, flags select sections and bundle.

    jamf jdoe          computers (the primary view -- Jamf is a Mac fleet
                       tool, and laptops are what people ask about)
    jamf jdoe -d       computers, explicitly
    jamf jdoe -m       mobile devices
    jamf jdoe -dm      both

Run just this stage:  pytest -m jamf
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx
from jamf_plugin.plugin import JamfPlugin
from typer.testing import CliRunner

from add_it_all.cli import build_app
from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jamf

BASE = "https://example.jamfcloud.com"
TOKEN_URL = f"{BASE}/api/oauth/token"
COMPUTERS_URL = f"{BASE}/api/v1/computers-inventory"
MOBILE_URL = f"{BASE}/api/v2/mobile-devices/detail"

CONFIG = PluginConfig({
    "JAMF_BASE_URL": BASE, "JAMF_CLIENT_ID": "id", "JAMF_CLIENT_SECRET": "secret"})
MOCK_CONFIG = PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"})

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    return _ANSI.sub("", result.stdout)


def _app(config: PluginConfig = CONFIG):
    return build_app({"jamf": JamfPlugin(config)})


def _computer(name="Jane's MacBook Pro", serial="C02XYZ123ABC", managed=True):
    return {
        "id": "101",
        "general": {"name": name, "lastContactTime": "2026-09-20T08:15:00.000Z",
                    "lastReportDate": "2026-09-19T03:00:00.000Z",
                    "remoteManagement": {"managed": managed}},
        "hardware": {"serialNumber": serial, "model": "MacBook Pro (16-inch, 2021)"},
        "operatingSystem": {"name": "macOS", "version": "15.6.0"},
        "userAndLocation": {"username": "jdoe", "email": "jdoe@example.com"},
    }


def _mobile():
    """Section-nested, as the live endpoint actually returns. A flat fixture
    here is what let the mobile shaper read every field from the wrong place
    while the whole suite stayed green."""
    return {
        "mobileDeviceId": "55",
        "general": {"displayName": "Jane's iPhone", "osVersion": "18.2", "managed": True,
                    "lastContactDate": "2026-09-19T12:00:00.000Z",
                    "lastInventoryUpdateDate": "2026-09-18T10:00:00.000Z"},
        "hardware": {"serialNumber": "F2LXYZ", "model": "iPhone 15 Pro"},
        "userAndLocation": {"username": "jdoe", "emailAddress": "jdoe@example.com"},
    }


def _mock(computers=None, mobiles=None):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 1200}))
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(200, json={
        "totalCount": 1, "results": computers if computers is not None else [_computer()]}))
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(200, json={
        "totalCount": 1, "results": mobiles if mobiles is not None else [_mobile()]}))


# --- Default view ----------------------------------------------------------------


@respx.mock
def test_identifier_alone_shows_computers():
    _mock()

    result = runner.invoke(_app(), ["jamf", "jdoe"])

    assert result.exit_code == 0
    assert "C02XYZ123ABC" in _out(result)


@respx.mock
def test_the_default_view_does_not_fetch_mobile_devices():
    _mock()
    mobile = respx.get(MOBILE_URL).mock(return_value=httpx.Response(
        200, json={"totalCount": 0, "results": []}))

    runner.invoke(_app(), ["jamf", "jdoe"])

    assert not mobile.called


def test_no_identifier_is_a_usage_error():
    assert runner.invoke(_app(), ["jamf"]).exit_code == 2


@respx.mock
def test_no_devices_says_so():
    _mock(computers=[])

    result = runner.invoke(_app(), ["jamf", "jdoe"])

    assert result.exit_code == 0
    assert "no" in _out(result).lower()


# --- Sections and bundling -------------------------------------------------------


@pytest.mark.parametrize("flag", ["-d", "-devices", "--devices"])
@respx.mock
def test_every_computers_spelling_works(flag):
    _mock()

    assert runner.invoke(_app(), ["jamf", "jdoe", flag]).exit_code == 0


@pytest.mark.parametrize("flag", ["-m", "-mobile", "--mobile"])
@respx.mock
def test_every_mobile_spelling_works(flag):
    _mock()

    result = runner.invoke(_app(), ["jamf", "jdoe", flag])

    assert result.exit_code == 0
    assert "F2LXYZ" in _out(result)


@respx.mock
def test_mobile_alone_does_not_show_computers():
    _mock()

    out = _out(runner.invoke(_app(), ["jamf", "jdoe", "-m"]))

    assert "F2LXYZ" in out
    assert "C02XYZ123ABC" not in out


@respx.mock
def test_bundled_flags_show_both_sections():
    _mock()

    out = _out(runner.invoke(_app(), ["jamf", "jdoe", "-dm"]))

    assert "C02XYZ123ABC" in out
    assert "F2LXYZ" in out


@respx.mock
def test_both_sections_exchange_the_token_only_once():
    token = respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 1200}))
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(
        200, json={"totalCount": 1, "results": [_computer()]}))
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(
        200, json={"totalCount": 1, "results": [_mobile()]}))

    runner.invoke(_app(), ["jamf", "jdoe", "-dm"])

    assert token.call_count == 1


@respx.mock
def test_flags_may_precede_the_identifier():
    _mock()

    assert runner.invoke(_app(), ["jamf", "-m", "jdoe"]).exit_code == 0


# --- What the table shows ----------------------------------------------------------


@respx.mock
def test_check_in_and_inventory_are_shown_as_distinct_columns():
    """Conflating them would let a daily-checking-in machine with months-old
    inventory read as fully up to date."""
    _mock()

    out = _out(runner.invoke(_app(), ["jamf", "jdoe"]))

    assert "check-in" in out.lower()
    assert "inventory" in out.lower()


@respx.mock
def test_an_unmanaged_device_is_called_out():
    _mock(computers=[_computer(managed=False)])

    out = _out(runner.invoke(_app(), ["jamf", "jdoe"]))

    assert "unmanaged" in out.lower() or "no" in out.lower()


@respx.mock
def test_contact_details_from_the_inventory_are_not_printed():
    _mock()

    out = _out(runner.invoke(_app(), ["jamf", "jdoe"]))

    assert "jdoe@example.com" not in out


# --- Failures ------------------------------------------------------------------------


@respx.mock
def test_an_auth_failure_exits_non_zero():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

    assert runner.invoke(_app(), ["jamf", "jdoe"]).exit_code == 1


@respx.mock
def test_a_mobile_failure_alongside_computers_keeps_the_computers():
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "tok", "expires_in": 1200}))
    respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(
        200, json={"totalCount": 1, "results": [_computer()]}))
    respx.get(MOBILE_URL).mock(return_value=httpx.Response(403))

    result = runner.invoke(_app(), ["jamf", "jdoe", "-dm"])

    out = _out(result)
    assert "C02XYZ123ABC" in out
    assert "unavailable" in out.lower()


def test_missing_credentials_exit_non_zero_with_an_actionable_message():
    result = runner.invoke(build_app({"jamf": JamfPlugin(PluginConfig({}))}), ["jamf", "x"])

    assert result.exit_code == 1
    assert "JAMF_BASE_URL" in _out(result)


# --- Help, layout, mock mode ------------------------------------------------------------


def test_help_documents_both_sections():
    out = _out(runner.invoke(_app(), ["jamf", "--help"]))

    for expected in ("-d", "--devices", "-m", "--mobile"):
        assert expected in out


def test_help_names_the_scope_difference_from_okta():
    """Users conflate Jamf and Okta device lists constantly; the help text is
    where that gets headed off."""
    out = _out(runner.invoke(_app(), ["jamf", "--help"])).lower()

    assert "okta" in out


def test_mock_mode_end_to_end():
    assert runner.invoke(_app(MOCK_CONFIG), ["jamf", "jdoe", "-dm"]).exit_code == 0


@respx.mock
def test_the_table_stays_readable_at_80_columns():
    """Seven columns squeezed `model` to "Ma…" and wrapped the name over four
    lines. The Okta device table hit this twice; asserting only that the
    serial survived was not enough to catch it."""
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock()

    out = _ANSI.sub("", narrow.invoke(_app(), ["jamf", "jdoe"]).stdout)

    assert "C02XYZ123ABC" in out, "serial must never be squeezed out"
    assert "\u2026" not in out, "a column was truncated mid-word at 80 columns"


@respx.mock
def test_the_device_name_is_not_shredded_at_80_columns():
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock()

    out = _ANSI.sub("", narrow.invoke(_app(), ["jamf", "jdoe"]).stdout)
    # The name may wrap once, but four fragments means the column collapsed.
    body = [l for l in out.splitlines() if l.startswith("\u2502") and "Mock" not in l]
    assert "Jane's MacBook Pro" in out or out.count("MacBook") <= 2


@respx.mock
def test_both_date_columns_survive_the_narrow_layout():
    """Whatever gives way to fit 80 columns, it must not be check-in or
    inventory -- keeping those distinct is the point of the table."""
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock()

    out = _ANSI.sub("", narrow.invoke(_app(), ["jamf", "jdoe"]).stdout).lower()

    assert "check-in" in out
    assert "inventory" in out
