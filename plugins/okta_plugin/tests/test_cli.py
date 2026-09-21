"""
Stage 2: the `add-it-all okta <identifier> [flags]` CLI surface.

Shape decided 2026-09-02: `<service> <person> [what you want]`, with no
noun subcommands. `okta status jdoe` / `okta devices jdoe` were removed --
they cannot coexist with `okta jdoe`, because a person whose login is
literally "status" or "devices" would silently resolve to the subcommand.
Stages 4-6 adopt the same shape.

Driven through the real core app via `build_app`, so these also prove the
plugin's sub-app is mounted the way a user would actually reach it.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx
from okta_plugin.plugin import OktaPlugin
from typer.testing import CliRunner

from add_it_all.cli import build_app
from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"
USER_ID = "00u1abcdefGHIJKLmno7"
DEVICES_URL = f"{USERS_URL}/{USER_ID}/devices"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})
MOCK_CONFIG = PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"})

# Pin the terminal width and disable colour: rich sizes tables to the terminal,
# so assertions on cell contents would otherwise depend on whoever's shell runs
# the suite.
runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    """CLI output with ANSI styling stripped.

    Belt and braces alongside NO_COLOR. CI rendered rich output in colour
    while local runs did not, and rich splits a styled token like `--status`
    across escape sequences -- so `"--status" in result.stdout` passed
    locally and failed in CI for a CLI that was working correctly. Assert on
    text, never on styling.
    """
    return _ANSI.sub("", result.stdout)


def _app(config: PluginConfig = CONFIG):
    return build_app({"okta": OktaPlugin(config)})


def _user_payload(status: str = "ACTIVE") -> dict:
    return {
        "id": USER_ID,
        "status": status,
        "statusChanged": "2025-11-02T09:00:00.000Z",
        "lastLogin": "2026-08-20T14:31:00.000Z",
        "profile": {"login": "jdoe", "email": "jdoe@example.com", "firstName": "Jane"},
    }


def _device_link(serial: str = "C02XYZ123ABC", name: str = "Jane's MacBook Pro") -> dict:
    return {
        "id": "guo1",
        "managementStatus": "MANAGED",
        "device": {
            "id": "guo1",
            "status": "ACTIVE",
            "profile": {
                "displayName": name,
                "platform": "MACOS",
                "model": "MacBookPro18,3",
                "osVersion": "15.6.0",
                "serialNumber": serial,
            },
        },
    }


def _mock_user(status: str = "ACTIVE"):
    return respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_payload(status))
    )


# --- Bare invocation: identifier alone is enough --------------------------------


@respx.mock
def test_identifier_alone_shows_status():
    _mock_user()

    result = runner.invoke(_app(), ["okta", "jdoe"])

    assert result.exit_code == 0
    assert "ACTIVE" in _out(result)


@respx.mock
def test_identifier_alone_does_not_call_the_devices_endpoint():
    _mock_user()
    devices_route = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    runner.invoke(_app(), ["okta", "jdoe"])

    assert not devices_route.called


def test_no_identifier_is_a_usage_error():
    result = runner.invoke(_app(), ["okta"])
    assert result.exit_code == 2


@respx.mock
def test_an_email_identifier_works():
    respx.get(f"{USERS_URL}/jdoe%40example.com").mock(
        return_value=httpx.Response(200, json=_user_payload())
    )

    result = runner.invoke(_app(), ["okta", "jdoe@example.com"])

    assert result.exit_code == 0


# --- -s / --status ---------------------------------------------------------------


@respx.mock
def test_explicit_status_flag_matches_the_default():
    _mock_user()
    result = runner.invoke(_app(), ["okta", "jdoe", "-s"])
    assert result.exit_code == 0
    assert "ACTIVE" in _out(result)


@respx.mock
def test_long_status_flag_works():
    _mock_user()
    assert runner.invoke(_app(), ["okta", "jdoe", "--status"]).exit_code == 0


@respx.mock
def test_deactivated_user_is_called_out_in_plain_language():
    """DEPROVISIONED is what the Okta admin UI calls "deactivated"; the raw
    enum alone makes an operator translate it in their head."""
    _mock_user("DEPROVISIONED")

    result = runner.invoke(_app(), ["okta", "jdoe", "-s"])

    assert result.exit_code == 0
    assert "DEPROVISIONED" in _out(result)
    assert "deactivated" in _out(result).lower()


@respx.mock
def test_suspended_is_distinguished_from_deactivated():
    """Both block login but they are different states, and conflating them
    would mislead someone verifying an offboarding actually completed."""
    _mock_user("SUSPENDED")

    result = runner.invoke(_app(), ["okta", "jdoe", "-s"])

    assert "SUSPENDED" in _out(result)
    assert "deactivated" not in _out(result).lower()


@respx.mock
def test_active_user_is_not_labelled_deactivated():
    _mock_user("ACTIVE")
    result = runner.invoke(_app(), ["okta", "jdoe"])
    assert "deactivated" not in _out(result).lower()


@pytest.mark.parametrize("status", ["LOCKED_OUT", "PASSWORD_EXPIRED", "STAGED", "PROVISIONED"])
@respx.mock
def test_other_statuses_are_reported_verbatim(status):
    _mock_user(status)
    result = runner.invoke(_app(), ["okta", "jdoe"])
    assert status in _out(result)


# --- -d / --devices ----------------------------------------------------------------


@respx.mock
def test_devices_flag_shows_devices():
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-d"])

    assert result.exit_code == 0
    assert "C02XYZ123ABC" in _out(result)


@respx.mock
def test_long_devices_flag_works():
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    result = runner.invoke(_app(), ["okta", "jdoe", "--devices"])

    assert result.exit_code == 0
    assert "C02XYZ123ABC" in _out(result)


@respx.mock
def test_devices_flag_alone_omits_the_status_table():
    """Flags select sections: `-d` means "devices", not "status and devices"."""
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-d"])

    assert "last_login" not in _out(result)


@respx.mock
def test_no_devices_message():
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-d"])

    assert result.exit_code == 0
    assert "No devices" in _out(result)


# --- Combining flags -----------------------------------------------------------------


@respx.mock
def test_both_flags_show_both_sections():
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-s", "-d"])

    assert "last_login" in _out(result)
    assert "C02XYZ123ABC" in _out(result)


@respx.mock
def test_bundled_short_flags_work():
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-sd"])

    assert "last_login" in _out(result)
    assert "C02XYZ123ABC" in _out(result)


@respx.mock
def test_flags_may_precede_the_identifier():
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    result = runner.invoke(_app(), ["okta", "-d", "jdoe"])

    assert result.exit_code == 0
    assert "C02XYZ123ABC" in _out(result)


@respx.mock
def test_both_flags_resolve_the_user_only_once():
    user_route = _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    runner.invoke(_app(), ["okta", "jdoe", "-sd"])

    assert user_route.call_count == 1


# --- Failure modes ----------------------------------------------------------------------


@respx.mock
def test_unknown_user_exits_zero_and_makes_no_device_call():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))
    devices_route = respx.get(url__startswith=USERS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = runner.invoke(_app(), ["okta", "ghost", "-d"])

    assert result.exit_code == 0
    assert "No Okta account" in _out(result)
    assert not devices_route.called


@respx.mock
def test_status_failure_exits_non_zero():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(503))

    result = runner.invoke(_app(), ["okta", "jdoe"])

    assert result.exit_code == 1


@respx.mock
def test_a_devices_failure_alongside_status_still_shows_the_status():
    """With `-sd` the status is a real answer already on screen; a device-API
    problem degrades that section rather than discarding both."""
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(404))

    result = runner.invoke(_app(), ["okta", "jdoe", "-sd"])

    assert "ACTIVE" in _out(result)
    assert "unavailable" in _out(result).lower()


@respx.mock
def test_a_devices_failure_alone_exits_non_zero():
    """With only `-d`, devices are the whole answer, so scripts must be able
    to trust the exit code."""
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(404))

    result = runner.invoke(_app(), ["okta", "jdoe", "-d"])

    assert result.exit_code == 1


def test_missing_credentials_exit_non_zero_with_an_actionable_message():
    result = runner.invoke(build_app({"okta": OktaPlugin(PluginConfig({}))}), ["okta", "x"])

    assert result.exit_code == 1
    assert "OKTA_ORG_URL" in _out(result)


# --- Help and mock mode ---------------------------------------------------------------------


def test_help_documents_both_flags():
    result = runner.invoke(_app(), ["okta", "--help"])

    for expected in ("-s", "--status", "-d", "--devices"):
        assert expected in _out(result)


def test_help_shows_the_identifier_as_a_direct_argument():
    result = runner.invoke(_app(), ["okta", "--help"])
    assert "identifier" in _out(result)


def test_removed_subcommands_are_gone():
    """`okta status jdoe` used to work. It must now read "status" as the
    identifier, not silently dispatch -- proving the ambiguity is gone."""
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "status"])

    assert result.exit_code == 0
    assert "status" in _out(result)  # treated as a username


def test_mock_mode_end_to_end():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "-sd"])

    assert result.exit_code == 0
    assert "ACTIVE" in _out(result)
    assert "C02MOCK00001" in _out(result)


# --- access blocked row -----------------------------------------------------------
#
# `profile.access_blocked` is a custom Universal Directory attribute (Profile
# Editor label "ACCESS BLOCKED"). Whatever Okta returns is displayed verbatim
# -- no translating booleans into yes/no -- so an operator sees the same value
# the Okta admin UI shows them.


def _user_with_access(value, key: str = "access_blocked") -> dict:
    payload = _user_payload()
    payload["profile"][key] = value
    return payload


def _row_value(output: str, field: str) -> str | None:
    """Pull one field's value out of the rendered rich table."""
    for line in output.splitlines():
        cells = [c.strip() for c in line.strip().strip("│").split("│")]
        if len(cells) == 2 and cells[0] == field:
            return cells[1]
    return None


@respx.mock
def test_string_value_is_shown_exactly_as_okta_returns_it():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access("ACCESS BLOCKED"))
    )

    result = runner.invoke(_app(), ["okta", "jdoe"])

    assert _row_value(_out(result), "access blocked") == "ACCESS BLOCKED"


@respx.mock
def test_boolean_true_renders_as_json_true_not_python_True():
    """Okta's JSON says `true`; Python's str() would say `True`. Verbatim
    means matching what the API actually returned."""
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access(True))
    )

    assert _row_value(_out(runner.invoke(_app(), ["okta", "jdoe"])), "access blocked") == "true"


@respx.mock
def test_boolean_false_renders_as_false_not_as_a_dash():
    """An explicit `false` is a real answer and must not look like "unset"."""
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access(False))
    )

    assert _row_value(_out(runner.invoke(_app(), ["okta", "jdoe"])), "access blocked") == "false"


@respx.mock
def test_unset_attribute_renders_as_a_dash():
    """The common case in this org -- the attribute exists but nobody set it."""
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))

    assert _row_value(_out(runner.invoke(_app(), ["okta", "jdoe"])), "access blocked") == "-"


@respx.mock
def test_null_attribute_renders_as_a_dash():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access(None))
    )

    assert _row_value(_out(runner.invoke(_app(), ["okta", "jdoe"])), "access blocked") == "-"


@respx.mock
def test_numeric_value_is_also_passed_through():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access(1))
    )

    assert _row_value(_out(runner.invoke(_app(), ["okta", "jdoe"])), "access blocked") == "1"


@respx.mock
def test_the_row_sits_directly_under_status():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access("ACCESS BLOCKED"))
    )

    lines = [line for line in _out(runner.invoke(_app(), ["okta", "jdoe"])).splitlines()]
    status_at = next(i for i, line in enumerate(lines) if "│ status " in line)
    access_at = next(i for i, line in enumerate(lines) if "access blocked" in line)

    assert access_at == status_at + 1


@respx.mock
def test_the_row_is_labelled_in_plain_words_not_the_raw_variable_name():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access("ACCESS BLOCKED"))
    )

    out = _out(runner.invoke(_app(), ["okta", "jdoe"]))

    assert "access blocked" in out


@respx.mock
def test_devices_only_view_does_not_show_the_row():
    respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_with_access("ACCESS BLOCKED"))
    )
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-d"]))

    assert "access blocked" not in out


# --- --last-signin ------------------------------------------------------------------
#
# Opt-in modifier on the devices view. Long-only by convention: short flags are
# section selectors (-s, -d), long flags modify how a section renders. That
# keeps -sd meaning "two sections" and leaves -l/-g/-a free for future sections.

LOGS_URL = f"{ORG_URL}/api/v1/logs"
MBP = "guoMACBOOK00000000001"


def _device_with_id(device_id: str, serial: str, name: str) -> dict:
    entry = _device_link(serial=serial, name=name)
    entry["id"] = device_id
    entry["device"]["id"] = device_id
    return entry


def _signin_event(device_id: str, published: str) -> dict:
    return {
        "published": published,
        "eventType": "user.session.start",
        "outcome": {"result": "SUCCESS"},
        "device": {"id": device_id},
    }


def _mock_devices_and_logs(events: list[dict] | None = None, log_status: int = 200):
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(200, json=[_device_with_id(MBP, "C02XYZ123ABC", "Jane's MBP")])
    )
    respx.get(LOGS_URL).mock(return_value=httpx.Response(log_status, json=events or []))


@respx.mock
def test_devices_alone_has_no_signin_column():
    _mock_devices_and_logs()

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-d"]))

    assert "last sign-in" not in out


@respx.mock
def test_devices_alone_does_not_touch_the_rate_limited_log_endpoint():
    respx.get(f"{USERS_URL}/jdoe").mock(return_value=httpx.Response(200, json=_user_payload()))
    respx.get(DEVICES_URL).mock(
        return_value=httpx.Response(200, json=[_device_with_id(MBP, "C02XYZ123ABC", "Jane's MBP")])
    )
    logs = respx.get(LOGS_URL).mock(return_value=httpx.Response(200, json=[]))

    runner.invoke(_app(), ["okta", "jdoe", "-d"])

    assert not logs.called


@respx.mock
def test_last_signin_adds_the_column_with_a_date():
    _mock_devices_and_logs([_signin_event(MBP, "2026-08-28T14:31:00.000Z")])

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-d", "--last-signin"]))

    assert "last sign-in" in out
    assert "2026-08-28" in out


@respx.mock
def test_last_signin_implies_devices():
    """Asking for per-device sign-ins obviously means you want the device table."""
    _mock_devices_and_logs([_signin_event(MBP, "2026-08-28T14:31:00.000Z")])

    result = runner.invoke(_app(), ["okta", "jdoe", "--last-signin"])

    assert result.exit_code == 0
    assert "C02XYZ123ABC" in _out(result)


@respx.mock
def test_device_with_no_signin_in_the_window_shows_a_placeholder():
    """Must not read as "never used" -- the window is all we can see."""
    _mock_devices_and_logs([])

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-d", "--last-signin"]))

    assert "last sign-in" in out


@respx.mock
def test_column_header_states_the_window():
    _mock_devices_and_logs([])

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-d", "--last-signin"]))

    assert "90d" in out


@respx.mock
def test_since_narrows_the_window_and_the_header_follows():
    _mock_devices_and_logs([])

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-d", "--last-signin", "--since", "30d"]))

    assert "30d" in out


@respx.mock
def test_invalid_since_is_rejected_with_a_clear_message():
    _mock_devices_and_logs([])

    result = runner.invoke(_app(), ["okta", "jdoe", "-d", "--last-signin", "--since", "banana"])

    assert result.exit_code != 0
    assert "since" in _out(result).lower()


@respx.mock
def test_a_log_failure_degrades_the_column_but_keeps_the_devices():
    """The inventory is still a real answer; losing sign-in times shouldn't
    discard it."""
    _mock_devices_and_logs(log_status=403)

    result = runner.invoke(_app(), ["okta", "jdoe", "-d", "--last-signin"])

    assert result.exit_code == 0
    out = _out(result)
    assert "C02XYZ123ABC" in out, "device inventory must survive"
    assert "?" in out


def test_help_documents_the_flag_and_since():
    out = _out(runner.invoke(_app(), ["okta", "--help"]))

    assert "--last-signin" in out
    assert "--since" in out


def test_last_signin_has_no_short_flag():
    """Short flags are reserved for section selectors (-s, -d)."""
    result = runner.invoke(_app(), ["okta", "jdoe", "-L"])

    assert result.exit_code != 0


def test_mock_mode_end_to_end_with_last_signin():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "-d", "--last-signin"])

    assert result.exit_code == 0
    assert "last sign-in" in _out(result)


@respx.mock
def test_model_gives_way_to_keep_the_table_readable_at_80_columns():
    """Six columns re-create the squeeze that going seven-to-five fixed."""
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock_devices_and_logs([_signin_event(MBP, "2026-08-28T14:31:00.000Z")])

    out = _ANSI.sub("", narrow.invoke(_app(), ["okta", "jdoe", "-d", "--last-signin"]).stdout)

    assert "C02XYZ123ABC" in out, "serial must never be squeezed out"
    assert "2026-08-28" in out
    assert "model" not in out


@respx.mock
def test_model_is_still_shown_without_the_signin_column():
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock_devices_and_logs()

    out = _ANSI.sub("", narrow.invoke(_app(), ["okta", "jdoe", "-d"]).stdout)

    assert "model" in out


# --- -a / -apps / --apps  and  -u / -authenticators / --authenticators ---------------
#
# Flag spelling decided 2026-09-02. Both a single-character short form and a
# multi-character single-dash form are declared for each section, so `-a` can
# bundle (`-sdau`) while `-apps` stays readable. `-sdapp` cannot be made to
# work and is not a bug: Click decomposes a single-dash string it cannot match
# as a whole into individual characters, so `-sdapp` reads as `-s -d -a -p -p`.

APPS_URL = f"{USERS_URL}/{USER_ID}/appLinks"
FACTORS_URL = f"{USERS_URL}/{USER_ID}/factors"


def _app_link(label: str = "Google Workspace", app_name: str = "google") -> dict:
    return {
        "id": "0oa1gjh63g214q0Hq0g4",
        "label": label,
        "appName": app_name,
        "hidden": False,
        "sortOrder": 0,
    }


def _factor(factor_type: str = "push", profile: dict | None = None) -> dict:
    return {
        "id": f"opf-{factor_type}",
        "factorType": factor_type,
        "provider": "OKTA",
        "status": "ACTIVE",
        "created": "2025-06-11T08:12:00.000Z",
        "profile": profile if profile is not None else {"name": "Jane's iPhone"},
    }


def _mock_everything(apps_status: int = 200, factors_status: int = 200):
    _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))
    respx.get(APPS_URL).mock(return_value=httpx.Response(apps_status, json=[_app_link()]))
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(factors_status, json=[_factor()]))


@pytest.mark.parametrize("flag", ["-a", "-apps", "--apps"])
@respx.mock
def test_every_apps_spelling_shows_the_apps_table(flag):
    _mock_everything()

    result = runner.invoke(_app(), ["okta", "jdoe", flag])

    assert result.exit_code == 0
    assert "Google Workspace" in _out(result)


@pytest.mark.parametrize("flag", ["-u", "-authenticators", "--authenticators"])
@respx.mock
def test_every_authenticators_spelling_shows_the_authenticators_table(flag):
    _mock_everything()

    result = runner.invoke(_app(), ["okta", "jdoe", flag])

    assert result.exit_code == 0
    assert "Okta Verify push" in _out(result)


@respx.mock
def test_apps_flag_alone_omits_the_other_sections():
    """Flags select sections: `-a` means apps, not apps-and-everything-else."""
    _mock_everything()

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-a"]))

    assert "Google Workspace" in out
    assert "last_login" not in out, "status table must not appear"
    assert "C02XYZ123ABC" not in out, "devices table must not appear"


@respx.mock
def test_apps_flag_alone_does_not_call_the_other_endpoints():
    _mock_user()
    devices = respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[]))
    factors = respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[_app_link()]))

    runner.invoke(_app(), ["okta", "jdoe", "-a"])

    assert not devices.called
    assert not factors.called


@respx.mock
def test_authenticators_flag_alone_does_not_call_the_apps_endpoint():
    _mock_user()
    apps = respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[_factor()]))

    runner.invoke(_app(), ["okta", "jdoe", "-u"])

    assert not apps.called


@respx.mock
def test_no_apps_message():
    _mock_user()
    respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-a"])

    assert result.exit_code == 0
    assert "No applications" in _out(result)


@respx.mock
def test_no_authenticators_message():
    _mock_user()
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-u"])

    assert result.exit_code == 0
    assert "No authenticators" in _out(result)


# --- Bundling ---------------------------------------------------------------------


@respx.mock
def test_au_bundles_to_apps_and_authenticators():
    """`-a` + `-u`, which is what the letters say. `-au` is deliberately NOT
    declared as an option of its own: if it were, `-au` would mean
    authenticators-only while `-sdau` still meant apps-and-authenticators, and
    the same two letters would mean two different things."""
    _mock_everything()

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-au"]))

    assert "Google Workspace" in out
    assert "Okta Verify push" in out


@respx.mock
def test_sdau_shows_all_four_sections():
    _mock_everything()

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-sdau"]))

    assert "ACTIVE" in out
    assert "C02XYZ123ABC" in out
    assert "Google Workspace" in out
    assert "Okta Verify push" in out


@respx.mock
def test_bundle_order_does_not_matter():
    _mock_everything()

    assert _out(runner.invoke(_app(), ["okta", "jdoe", "-ua"])) == _out(
        runner.invoke(_app(), ["okta", "jdoe", "-au"])
    )


@respx.mock
def test_all_four_sections_resolve_the_user_only_once():
    user_route = _mock_user()
    respx.get(DEVICES_URL).mock(return_value=httpx.Response(200, json=[_device_link()]))
    respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[_app_link()]))
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[_factor()]))

    runner.invoke(_app(), ["okta", "jdoe", "-sdau"])

    assert user_route.call_count == 1


@respx.mock
def test_apps_flag_may_precede_the_identifier():
    _mock_everything()

    result = runner.invoke(_app(), ["okta", "-a", "jdoe"])

    assert result.exit_code == 0
    assert "Google Workspace" in _out(result)


def test_sdapp_is_a_usage_error_not_a_silent_misparse():
    """Documents a known limit. Click reads an unmatched single-dash string
    character by character, so `-sdapp` is `-s -d -a -p -p` and there is no
    `-p`. Failing loudly is the correct outcome; the working spelling is
    `-sdau` or `-sd -apps`."""
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "-sdapp"])

    assert result.exit_code == 2


# --- Failure modes ----------------------------------------------------------------


@respx.mock
def test_an_apps_failure_alone_exits_non_zero():
    _mock_everything(apps_status=403)

    result = runner.invoke(_app(), ["okta", "jdoe", "-a"])

    assert result.exit_code == 1


@respx.mock
def test_an_apps_failure_alongside_status_still_shows_the_status():
    _mock_everything(apps_status=403)

    result = runner.invoke(_app(), ["okta", "jdoe", "-sa"])

    assert "ACTIVE" in _out(result)
    assert "unavailable" in _out(result).lower()


@respx.mock
def test_an_authenticators_failure_does_not_discard_the_apps_table():
    """Independent sections fail independently -- one dead endpoint should not
    take a good answer off the screen."""
    _mock_everything(factors_status=403)

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-au"]))

    assert "Google Workspace" in out
    assert "unavailable" in out.lower()


@respx.mock
def test_unknown_user_makes_no_apps_or_factors_call():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))
    others = respx.get(url__startswith=USERS_URL).mock(
        return_value=httpx.Response(200, json=[])
    )

    result = runner.invoke(_app(), ["okta", "ghost", "-au"])

    assert result.exit_code == 0
    assert "No Okta account" in _out(result)
    assert not others.called


# --- Help, layout, mock mode ---------------------------------------------------------


def test_help_documents_every_spelling_of_the_new_flags():
    out = _out(runner.invoke(_app(), ["okta", "--help"]))

    for expected in ("-a", "-apps", "--apps", "-u", "-authenticators", "--authenticators"):
        assert expected in out, f"{expected} missing from --help"


def test_mock_mode_end_to_end_with_all_four_sections():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "-sdau"])

    assert result.exit_code == 0
    assert "ACTIVE" in _out(result)
    assert "C02MOCK00001" in _out(result)


@respx.mock
def test_both_new_tables_stay_readable_at_80_columns():
    """Two tables have already been squeezed unreadable at a stock 80-column
    terminal; these are checked before anyone hits it."""
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock_user()
    respx.get(APPS_URL).mock(
        return_value=httpx.Response(200, json=[_app_link("AWS Production Account", "amazon_aws")])
    )
    respx.get(FACTORS_URL).mock(
        return_value=httpx.Response(
            200, json=[_factor("webauthn", profile={"authenticatorName": "YubiKey 5C NFC"})]
        )
    )

    out = _ANSI.sub("", narrow.invoke(_app(), ["okta", "jdoe", "-au"]).stdout)

    assert "AWS Production Account" in out
    assert "YubiKey 5C NFC" in out


@respx.mock
def test_the_security_question_text_never_reaches_the_terminal():
    _mock_user()
    respx.get(FACTORS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _factor(
                    "question",
                    profile={"question": "favorite_art_piece", "questionText": "Favourite art?"},
                )
            ],
        )
    )

    out = _out(runner.invoke(_app(), ["okta", "jdoe", "-u"]))

    assert "Security question" in out
    assert "Favourite art?" not in out


# --- --find : long-only, and deliberately not chainable -------------------------
#
# Short flags here are section selectors and they compose (`-sdau`). Search is
# not a section -- it answers "who is this person", not "what do you want to
# see about this person" -- so it composes with nothing. Long-only makes every
# bundled spelling a parse error for free; `--find -d` is caught at runtime.


def _search_payload(*users):
    return list(users)


def _found_user(login, first, last, status="ACTIVE", uid="00u1"):
    return {
        "id": uid,
        "status": status,
        "profile": {
            "login": login,
            "firstName": first,
            "lastName": last,
            "email": f"{login}@example.com",
        },
    }


def _mock_search(*users):
    return respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=list(users)))


DENNISES = (
    _found_user("dluo", "Dennis", "Luo", uid="00u1"),
    _found_user("dcarter", "Dennis", "Carter", status="DEPROVISIONED", uid="00u2"),
    _found_user("mdennison", "Marta", "Dennison", uid="00u3"),
)


@respx.mock
def test_find_lists_candidates_with_their_usernames():
    _mock_search(*DENNISES)

    result = runner.invoke(_app(), ["okta", "--find", "dennis"])

    assert result.exit_code == 0
    out = _out(result)
    for login in ("dluo", "dcarter", "mdennison"):
        assert login in out


@respx.mock
def test_find_shows_each_candidates_status():
    """Often enough to answer the question without a second command, and it
    is what distinguishes two people with the same first name."""
    _mock_search(*DENNISES)

    out = _out(runner.invoke(_app(), ["okta", "--find", "dennis"]))

    assert "ACTIVE" in out
    assert "DEPROVISIONED" in out


@respx.mock
def test_find_works_with_the_query_after_the_flag_or_before_it():
    _mock_search(*DENNISES)
    after = _out(runner.invoke(_app(), ["okta", "--find", "dennis"]))
    _mock_search(*DENNISES)
    before = _out(runner.invoke(_app(), ["okta", "dennis", "--find"]))

    assert "dluo" in after
    assert "dluo" in before


@respx.mock
def test_find_prints_a_copy_pasteable_next_command():
    """The two-step flow should be copy-paste, not retype."""
    _mock_search(*DENNISES)

    out = _out(runner.invoke(_app(), ["okta", "--find", "dennis"]))

    assert "add-it-all okta" in out


@respx.mock
def test_find_does_not_do_an_exact_lookup_first():
    """`--find` is explicit: go straight to search, one call not two."""
    exact = respx.get(f"{USERS_URL}/dennis").mock(
        return_value=httpx.Response(200, json=_user_payload())
    )
    _mock_search(*DENNISES)

    runner.invoke(_app(), ["okta", "--find", "dennis"])

    assert not exact.called


@respx.mock
def test_find_never_auto_resolves_a_single_match():
    """One candidate is not the same claim as the right person."""
    _mock_search(_found_user("mdennison", "Marta", "Dennison"))

    out = _out(runner.invoke(_app(), ["okta", "--find", "dennison"]))

    assert "mdennison" in out
    # Must be the chooser, not a status page for that person.
    assert "last_login" not in out


@respx.mock
def test_find_with_no_matches_says_so():
    _mock_search()

    result = runner.invoke(_app(), ["okta", "--find", "zzzznothing"])

    assert result.exit_code == 0
    assert "no" in _out(result).lower()


# --- --all : show everything, not just the first page ---------------------------


def _many(n):
    return [_found_user(f"dperson{i:03d}", "D", f"Person{i}", uid=f"00u{i}") for i in range(n)]


@respx.mock
def test_without_all_the_display_is_capped():
    from okta_plugin.plugin import MAX_MATCHES_SHOWN
    _mock_search(*_many(47))

    out = _out(runner.invoke(_app(), ["okta", "--find", "d"]))

    assert "47 people match" in out
    assert f"dperson{MAX_MATCHES_SHOWN - 1:03d}" in out
    assert f"dperson{MAX_MATCHES_SHOWN:03d}" not in out


@respx.mock
def test_the_cap_message_names_the_escape_hatch():
    """Telling someone to narrow without mentioning --all repeats the dead
    end that the --find hint exists to prevent."""
    _mock_search(*_many(47))

    out = _out(runner.invoke(_app(), ["okta", "--find", "d"]))

    assert "32 more not shown" in out
    assert "--all" in out


@respx.mock
def test_all_shows_every_match():
    _mock_search(*_many(47))

    out = _out(runner.invoke(_app(), ["okta", "--find", "d", "--all"]))

    assert "dperson000" in out
    assert "dperson046" in out
    assert "more not shown" not in out


@respx.mock
def test_all_works_before_or_after_the_query():
    _mock_search(*_many(20))
    a = _out(runner.invoke(_app(), ["okta", "--find", "d", "--all"]))
    _mock_search(*_many(20))
    b = _out(runner.invoke(_app(), ["okta", "--all", "--find", "d"]))

    assert "dperson019" in a
    assert "dperson019" in b


def test_all_without_find_is_a_usage_error():
    """--all modifies the search; there is no search to modify. Accepting it
    silently would leave someone believing they had asked for something."""
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "--all"])

    assert result.exit_code == 2
    assert "--all" in _out(result)


def test_all_combined_with_a_section_flag_is_a_usage_error():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "--all", "-d"])

    assert result.exit_code == 2


def test_all_has_no_short_flag():
    """`-a` already means apps. A short --all would be genuinely ambiguous."""
    assert runner.invoke(_app(MOCK_CONFIG), ["okta", "dennis", "--find", "-all"]).exit_code == 2


@respx.mock
def test_multi_token_search_narrows_from_the_cli():
    """The advice the cap message gives has to actually work."""
    route = _mock_search(_found_user("dluo", "Dennis", "Luo"))

    out = _out(runner.invoke(_app(), ["okta", "--find", "dennis luo"]))

    assert "dluo" in out
    assert " and " in route.calls.last.request.url.params["search"]


# --- Not chainable -----------------------------------------------------------


def test_find_has_no_short_flag():
    """`-f` is deliberately not declared. Declaring it would let `--find`
    bundle with the section flags and imply it composes with them."""
    assert runner.invoke(_app(MOCK_CONFIG), ["okta", "dennis", "-f"]).exit_code == 2


@pytest.mark.parametrize("bundle", ["-sdf", "-fd", "-sf", "-fa", "-sdauf"])
def test_no_bundled_spelling_of_find_parses(bundle):
    assert runner.invoke(_app(MOCK_CONFIG), ["okta", "dennis", bundle]).exit_code == 2


@pytest.mark.parametrize("section", ["-s", "-d", "-a", "-u", "--devices", "-sdau"])
def test_find_combined_with_a_section_flag_is_a_usage_error(section):
    """Silently ignoring a flag the user typed is the failure mode this CLI
    keeps designing out, so this errors rather than dropping the section."""
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "dennis", "--find", section])

    assert result.exit_code == 2


@respx.mock
def test_the_conflict_error_says_what_to_do_instead():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "dennis", "--find", "-d"])

    out = _out(result)
    assert "--find" in out
    assert "add-it-all okta" in out, "should show the two-step command"


def test_find_combined_with_last_signin_is_also_rejected():
    """--last-signin is a section modifier; there is no section to modify."""
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "dennis", "--find", "--last-signin"])

    assert result.exit_code == 2


# --- The discoverability hint ---------------------------------------------------


@respx.mock
def test_a_failed_exact_lookup_points_at_find():
    """Without this, someone who does not know --find exists still hits a
    dead end -- and not knowing the username is exactly the situation where
    you would not know the flag either."""
    respx.get(f"{USERS_URL}/dennis").mock(return_value=httpx.Response(404))

    result = runner.invoke(_app(), ["okta", "dennis"])

    assert result.exit_code == 0
    out = _out(result)
    assert "--find" in out
    assert "dennis" in out


@respx.mock
def test_the_hint_does_not_run_a_search_itself():
    """It is a hint, not an implicit fallback: `--find` stays explicit and
    the miss path stays one API call."""
    respx.get(f"{USERS_URL}/dennis").mock(return_value=httpx.Response(404))
    search = respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[]))

    runner.invoke(_app(), ["okta", "dennis"])

    assert not search.called


@respx.mock
def test_a_successful_lookup_shows_no_hint():
    _mock_user()

    out = _out(runner.invoke(_app(), ["okta", "jdoe"]))

    assert "--find" not in out


# --- Help, layout, failures ------------------------------------------------------


def test_help_documents_find_as_long_only():
    out = _out(runner.invoke(_app(), ["okta", "--help"]))

    assert "--find" in out


@respx.mock
def test_a_search_failure_exits_non_zero():
    respx.get(USERS_URL).mock(return_value=httpx.Response(403))

    assert runner.invoke(_app(), ["okta", "--find", "dennis"]).exit_code == 1


@respx.mock
def test_the_chooser_stays_readable_at_80_columns():
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock_search(*DENNISES)

    out = _ANSI.sub("", narrow.invoke(_app(), ["okta", "--find", "dennis"]).stdout)

    assert "dcarter" in out, "username must never be squeezed out"
    assert "DEPROVISIONED" in out


@respx.mock
def test_a_truncated_result_set_says_so_rather_than_looking_complete():
    from okta_plugin.plugin import MAX_SEARCH_RESULTS
    _mock_search(*[_found_user(f"user{i:03d}", "A", "B", uid=f"00u{i}")
                   for i in range(MAX_SEARCH_RESULTS)])

    out = _out(runner.invoke(_app(), ["okta", "--find", "a"]))

    assert "more" in out.lower() or "narrow" in out.lower()


def test_mock_mode_end_to_end_with_find():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "--find", "dennis"])

    assert result.exit_code == 0
    assert "DEPROVISIONED" in _out(result)
