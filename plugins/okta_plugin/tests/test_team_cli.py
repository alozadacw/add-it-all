"""
Stage 2: the `--groups` section and the `--team` comparison CLI surface.

`--groups` (`-g` / `-groups` / `--groups`) is a normal section flag: it
bundles with the others (`-sdaug`) and shows one user's Okta groups.

`--team` is a *mode* flag in the sense the CLI-shape note defines: long-only,
composing with nothing. It replaces "describe one person" with "compare a
team", so -- exactly like `--find` -- pairing it with a section flag is a
usage error rather than a silently dropped request. `--html PATH` writes the
comparison as a standalone web page and only means anything in team mode.

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

from lookup_cli.cli import build_app
from lookup_cli.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"
USER_ID = "00u1abcdefGHIJKLmno7"
GROUPS_URL = f"{USERS_URL}/{USER_ID}/groups"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})
MOCK_CONFIG = PluginConfig({"LOOKUP_CLI_MOCK_OKTA": "1"})

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    return _ANSI.sub("", result.stdout)


def _app(config: PluginConfig = CONFIG):
    return build_app({"okta": OktaPlugin(config)})


def _user_payload() -> dict:
    return {
        "id": USER_ID,
        "status": "ACTIVE",
        "profile": {"login": "jdoe", "email": "jdoe@example.com", "firstName": "Jane"},
    }


def _mock_user():
    return respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_payload())
    )


def _group(name: str, group_id: str = "00g1") -> dict:
    return {"id": group_id, "type": "OKTA_GROUP", "profile": {"name": name}}


# --- The --groups section -----------------------------------------------------


@respx.mock
def test_groups_flag_lists_a_users_groups():
    _mock_user()
    respx.get(GROUPS_URL).mock(
        return_value=httpx.Response(200, json=[_group("Eng-GitHub"), _group("VPN-Users", "00g2")])
    )

    result = runner.invoke(_app(), ["okta", "jdoe", "--groups"])

    assert result.exit_code == 0
    out = _out(result)
    assert "Eng-GitHub" in out
    assert "VPN-Users" in out


@respx.mock
def test_groups_bundles_with_other_sections():
    """-g must compose in a bundle like every other single-char section flag."""
    _mock_user()
    respx.get(GROUPS_URL).mock(return_value=httpx.Response(200, json=[_group("Everyone")]))

    result = runner.invoke(_app(), ["okta", "jdoe", "-sg"])

    assert result.exit_code == 0
    out = _out(result)
    assert "ACTIVE" in out       # status section
    assert "Everyone" in out     # groups section


@respx.mock
def test_bare_lookup_does_not_call_the_groups_endpoint():
    _mock_user()
    groups_route = respx.get(GROUPS_URL).mock(return_value=httpx.Response(200, json=[]))

    runner.invoke(_app(), ["okta", "jdoe"])

    assert not groups_route.called


# --- The --team comparison ----------------------------------------------------


def test_team_mode_renders_a_comparison_in_mock_mode():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jchen", "--team"])

    assert result.exit_code == 0
    out = _out(result)
    # A recognisable comparison: the drift concept is surfaced to the operator.
    assert "drift" in out.lower()


def test_team_cannot_be_combined_with_a_section_flag():
    """--team replaces the operation; a section flag alongside it is the same
    'silently dropped request' failure --find is designed to reject."""
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jchen", "--team", "-d"])

    assert result.exit_code == 2
    assert "--team" in _out(result)


def test_team_cannot_be_combined_with_find():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jchen", "--team", "--find"])

    assert result.exit_code == 2


# --- The --html output --------------------------------------------------------


def test_html_writes_a_file_and_reports_the_path(tmp_path):
    out_file = tmp_path / "team.html"

    result = runner.invoke(
        _app(MOCK_CONFIG), ["okta", "jchen", "--team", "--html", str(out_file)]
    )

    assert result.exit_code == 0
    assert out_file.exists()
    body = out_file.read_text()
    assert body.lstrip().lower().startswith("<!doctype html>")
    # The command tells the operator where the file landed.
    assert str(out_file) in _out(result)


def test_include_deactivated_requires_team_mode():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "--include-deactivated"])

    assert result.exit_code == 2
    assert "--team" in _out(result)


def test_team_accepts_include_deactivated():
    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jchen", "--team", "--include-deactivated"])

    assert result.exit_code == 0
    assert "drift" in _out(result).lower()


def test_html_requires_team_mode(tmp_path):
    """--html renders the team comparison; without --team there is nothing to
    render, so accepting it would leave someone believing they'd asked for
    something."""
    out_file = tmp_path / "team.html"

    result = runner.invoke(_app(MOCK_CONFIG), ["okta", "jdoe", "--html", str(out_file)])

    assert result.exit_code == 2
    assert not out_file.exists()
    assert "--team" in _out(result)
