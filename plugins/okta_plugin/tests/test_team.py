"""
Stage 2: a user's team and a comparison of everyone's group memberships.

`fetch_team()` resolves one person to their team roster -- the person plus
their direct reports -- via `GET /api/v1/users/{login}` for the subject and
`GET /api/v1/users?search=profile.managerId eq "<login>"` for the reports.
`fetch_team_groups()` then fans out one `fetch_groups()` per member
(concurrently, which is the whole point of `fetch()` being async) and lines
the memberships up into a matrix so access drift is obvious.

**Team definition is org-specific and deliberately explicit.** "Team" here
means the subject and whoever reports to them, matched on the manager
attribute (`OKTA_MANAGER_ATTRIBUTE`, default `managerId`). Whether that
attribute holds the manager's login, email or employee id is a per-org
Universal Directory decision the mocks cannot prove -- see the Open
Decisions Log in docs/STAGES.md. These tests pin the shape and the
comparison logic, not any claim about a live org's schema.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import httpx
import pytest
import respx
from okta_plugin.plugin import OktaPlugin, build_comparison, render_team_html

from lookup_cli.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"

SUBJECT_ID = "00uMGR0000000000000A"
R1_ID = "00uRPT0000000000000B"
R2_ID = "00uRPT0000000000000C"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


def _user(login: str, okta_id: str, first: str, last: str, manager: str | None = None) -> dict:
    profile = {"login": login, "email": f"{login}@example.com", "firstName": first, "lastName": last}
    if manager is not None:
        profile["managerId"] = manager
    return {"id": okta_id, "status": "ACTIVE", "profile": profile}


def _group(name: str, group_id: str) -> dict:
    return {"id": group_id, "type": "OKTA_GROUP", "profile": {"name": name}}


def _mock_subject(login: str = "jchen"):
    return respx.get(f"{USERS_URL}/{login}").mock(
        return_value=httpx.Response(200, json=_user(login, SUBJECT_ID, "Jules", "Chen"))
    )


def _mock_reports(*reports: dict):
    """Mock the direct-reports search (base /users path with a query string)."""
    return respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=list(reports)))


def _mock_groups(okta_id: str, *names: str):
    url = f"{USERS_URL}/{okta_id}/groups"
    return respx.get(url).mock(
        return_value=httpx.Response(200, json=[_group(n, f"00g{i}") for i, n in enumerate(names)])
    )


# --- build_comparison: the pure matrix logic ----------------------------------


def test_build_comparison_flags_shared_and_drifted_groups():
    members = [
        {"login": "jchen", "name": "Jules Chen", "is_subject": True,
         "groups": ["Everyone", "VPN-Users", "Okta-Admins"], "error": None},
        {"login": "arivera", "name": "Ana Rivera", "is_subject": False,
         "groups": ["Everyone", "VPN-Users"], "error": None},
        {"login": "dsingh", "name": "Dev Singh", "is_subject": False,
         "groups": ["Everyone"], "error": None},
    ]

    comparison = build_comparison(members)

    by_name = {row["name"]: row for row in comparison["groups"]}
    assert by_name["Everyone"]["everyone"] is True
    assert by_name["Everyone"]["drift"] is False
    assert by_name["VPN-Users"]["drift"] is True
    assert by_name["VPN-Users"]["count"] == 2
    assert by_name["Okta-Admins"]["count"] == 1
    # Per-member coverage is addressable by login, which the matrix renders on.
    assert by_name["VPN-Users"]["coverage"] == {"jchen": True, "arivera": True, "dsingh": False}

    summary = comparison["summary"]
    assert summary["member_count"] == 3
    assert summary["group_count"] == 3
    assert summary["shared_by_all"] == 1
    assert summary["drift_count"] == 2


def test_build_comparison_groups_are_sorted_case_insensitively():
    members = [
        {"login": "a", "name": "A", "is_subject": True,
         "groups": ["Zeta", "alpha", "Beta"], "error": None},
    ]

    comparison = build_comparison(members)

    assert [row["name"] for row in comparison["groups"]] == ["alpha", "Beta", "Zeta"]


def test_build_comparison_excludes_errored_members_from_coverage_denominator():
    """A member whose groups could not be fetched must not silently count as
    'not in the group' -- that would invent drift that may not exist. They are
    excluded from the denominator and surfaced separately."""
    members = [
        {"login": "jchen", "name": "Jules Chen", "is_subject": True,
         "groups": ["Everyone"], "error": None},
        {"login": "arivera", "name": "Ana Rivera", "is_subject": False,
         "groups": ["Everyone"], "error": None},
        {"login": "dsingh", "name": "Dev Singh", "is_subject": False,
         "groups": None, "error": "Okta refused the group request."},
    ]

    comparison = build_comparison(members)

    assert comparison["summary"]["present_count"] == 2
    assert comparison["summary"]["error_count"] == 1
    # Everyone is shared by all *present* members, not dragged to drift by the
    # member we could not read.
    assert comparison["groups"][0]["everyone"] is True


# --- fetch_team: the roster ---------------------------------------------------


@respx.mock
async def test_team_roster_is_subject_plus_direct_reports():
    _mock_subject("jchen")
    _mock_reports(
        _user("arivera", R1_ID, "Ana", "Rivera", manager="jchen"),
        _user("dsingh", R2_ID, "Dev", "Singh", manager="jchen"),
    )

    result = await _plugin().fetch_team("jchen")

    assert result.ok
    assert result.data["found"] is True
    logins = [m["login"] for m in result.data["members"]]
    # Subject first, reports after.
    assert logins[0] == "jchen"
    assert set(logins) == {"jchen", "arivera", "dsingh"}
    assert result.data["count"] == 3
    subject = result.data["members"][0]
    assert subject["is_subject"] is True
    assert subject["okta_id"] == SUBJECT_ID
    assert all(m["is_subject"] is False for m in result.data["members"][1:])


@respx.mock
async def test_team_with_no_reports_is_just_the_subject():
    _mock_subject("solo")
    _mock_reports()  # empty search result

    result = await _plugin().fetch_team("solo")

    assert result.ok
    assert result.data["count"] == 1
    assert result.data["members"][0]["is_subject"] is True


@respx.mock
async def test_unknown_subject_is_not_found_not_an_error():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_team("ghost")

    assert result.ok
    assert result.data["found"] is False
    assert "not-found" in result.tags


@respx.mock
async def test_manager_attribute_is_configurable():
    """Some orgs key the manager relationship on a custom attribute rather
    than the stock `managerId`."""
    config = PluginConfig(
        {"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "x", "OKTA_MANAGER_ATTRIBUTE": "managerEmail"}
    )
    _mock_subject("jchen")
    route = _mock_reports()

    await _plugin(config).fetch_team("jchen")

    assert route.called
    sent = route.calls.last.request.url.params["search"]
    assert "profile.managerEmail eq" in sent


# --- fetch_team_groups: the orchestration -------------------------------------


@respx.mock
async def test_team_group_comparison_lines_up_every_member():
    _mock_subject("jchen")
    _mock_reports(
        _user("arivera", R1_ID, "Ana", "Rivera", manager="jchen"),
        _user("dsingh", R2_ID, "Dev", "Singh", manager="jchen"),
    )
    _mock_groups(SUBJECT_ID, "Everyone", "VPN-Users", "Okta-Admins")
    _mock_groups(R1_ID, "Everyone", "VPN-Users")
    _mock_groups(R2_ID, "Everyone")

    result = await _plugin().fetch_team_groups("jchen")

    assert result.ok
    assert result.data["found"] is True
    summary = result.data["summary"]
    assert summary["member_count"] == 3
    assert summary["shared_by_all"] == 1  # Everyone
    assert summary["drift_count"] == 2    # VPN-Users, Okta-Admins


@respx.mock
async def test_one_members_failure_degrades_its_column_not_the_run():
    """A single member's forbidden groups call must not sink the comparison --
    the other members are still a real answer."""
    _mock_subject("jchen")
    _mock_reports(_user("arivera", R1_ID, "Ana", "Rivera", manager="jchen"))
    _mock_groups(SUBJECT_ID, "Everyone")
    respx.get(f"{USERS_URL}/{R1_ID}/groups").mock(return_value=httpx.Response(403))

    result = await _plugin().fetch_team_groups("jchen")

    assert result.ok
    assert result.data["summary"]["present_count"] == 1
    assert result.data["summary"]["error_count"] == 1
    errored = [m for m in result.data["members"] if m["groups"] is None]
    assert errored and errored[0]["login"] == "arivera"


@respx.mock
async def test_team_group_comparison_propagates_a_roster_failure():
    """If we can't even build the roster, that's a hard failure, not an empty
    comparison that reads as 'this manager has no team'."""
    respx.get(f"{USERS_URL}/jchen").mock(return_value=httpx.Response(500))

    result = await _plugin().fetch_team_groups("jchen")

    assert not result.ok


async def test_team_group_comparison_mock_mode_shows_drift_without_network():
    """The mock demo must show the case the feature exists for -- a team that
    diverges -- not a flat matrix where everyone matches."""
    plugin = OktaPlugin(PluginConfig({"LOOKUP_CLI_MOCK_OKTA": "1"}))

    result = await plugin.fetch_team_groups("jchen")

    assert result.ok
    assert result.data["summary"]["member_count"] >= 2
    assert result.data["summary"]["drift_count"] >= 1


# --- render_team_html: the GUI ------------------------------------------------


def _sample_comparison() -> dict:
    return build_comparison(
        [
            {"login": "jchen", "name": "Jules Chen", "is_subject": True,
             "groups": ["Everyone", "Okta-Admins"], "error": None},
            {"login": "arivera", "name": "Ana Rivera", "is_subject": False,
             "groups": ["Everyone"], "error": None},
        ]
    )


def test_render_team_html_is_a_complete_standalone_document():
    html = render_team_html(_sample_comparison(), subject_login="jchen")

    assert html.lstrip().lower().startswith("<!doctype html>")
    assert "</html>" in html
    # No external resources -- it must open straight from disk.
    assert "http://" not in html and "https://" not in html
    # The people and the groups both appear.
    assert "jchen" in html and "arivera" in html
    assert "Everyone" in html and "Okta-Admins" in html


def test_render_team_html_surfaces_the_drift_count():
    html = render_team_html(_sample_comparison(), subject_login="jchen")

    assert "drift" in html.lower()


def test_render_team_html_escapes_group_and_member_names():
    """Group and display names are attacker-influenceable directory data and
    end up in a file someone opens in a browser -- they must be escaped."""
    comparison = build_comparison(
        [
            {"login": "evil", "name": "<script>alert(1)</script>", "is_subject": True,
             "groups": ["<img src=x onerror=alert(2)>"], "error": None},
        ]
    )

    html = render_team_html(comparison, subject_login="evil")

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html


def test_render_team_html_marks_an_unreadable_member():
    comparison = build_comparison(
        [
            {"login": "jchen", "name": "Jules Chen", "is_subject": True,
             "groups": ["Everyone"], "error": None},
            {"login": "arivera", "name": "Ana Rivera", "is_subject": False,
             "groups": None, "error": "Okta refused the group request."},
        ]
    )

    html = render_team_html(comparison, subject_login="jchen")

    # The unreadable member is shown, but flagged rather than rendered as a
    # column of empty cells that would read as "in no groups".
    assert "arivera" in html
    assert "unavailable" in html.lower()
