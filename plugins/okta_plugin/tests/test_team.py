"""
Stage 2: a user's team and a comparison of everyone's group memberships.

`fetch_team()` resolves one person to the team they sit in. It does **not**
assume the person is a manager: it reads *their* manager from
`profile.<managerAttr>` (default `managerId`) and gathers everyone who reports
to that same manager -- the subject and their peers -- via
`GET /api/v1/users?search=profile.<managerAttr> eq "<managerRef>"`. So looking
up an IC compares them against their teammates, and looking up a manager
compares them against their peer managers. A subject with no manager on file
falls back to comparing their own direct reports.

`fetch_team_groups()` then fans out one `fetch_groups()` per member
(concurrently, which is the whole point of `fetch()` being async) and lines
the memberships up into a matrix so access drift is obvious.

**The manager key is org-specific and deliberately explicit.** Whether the
manager attribute holds the manager's login, email or employee id is a per-org
Universal Directory decision the mocks cannot prove -- see the Open Decisions
Log in docs/STAGES.md. These tests pin the shape and the comparison logic, not
any claim about a live org's schema.

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

SUBJECT_ID = "00uSUBJECT0000000000"
R1_ID = "00uPEER00000000000001"
R2_ID = "00uPEER00000000000002"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


def _user(
    login: str,
    okta_id: str,
    first: str,
    last: str,
    manager: str | None = None,
    hire: str | None = None,
    created: str | None = None,
    status: str = "ACTIVE",
) -> dict:
    profile = {"login": login, "email": f"{login}@example.com", "firstName": first, "lastName": last}
    if manager is not None:
        profile["managerId"] = manager
    if hire is not None:
        profile["hireDate"] = hire
    user = {"id": okta_id, "status": status, "profile": profile}
    if created is not None:
        user["created"] = created
    return user


def _group(name: str, group_id: str) -> dict:
    return {"id": group_id, "type": "OKTA_GROUP", "profile": {"name": name}}


def _mock_subject(login: str, okta_id: str = SUBJECT_ID, manager: str | None = "bigboss"):
    """The queried user. Reports to `manager` unless manager=None."""
    return respx.get(f"{USERS_URL}/{login}").mock(
        return_value=httpx.Response(200, json=_user(login, okta_id, "Jules", "Chen", manager))
    )


def _mock_cohort(*members: dict):
    """Mock the cohort search (base /users path with a query string)."""
    return respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=list(members)))


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
    assert comparison["groups"][0]["everyone"] is True


# --- fetch_team: the roster is the manager's cohort ---------------------------


@respx.mock
async def test_team_roster_is_the_subjects_manager_cohort():
    """Looking up jchen compares them against everyone reporting to jchen's
    manager (bigboss) -- the subject and their peers -- keyed on the manager,
    not on the subject."""
    _mock_subject("jchen", manager="bigboss")
    route = _mock_cohort(
        _user("jchen", SUBJECT_ID, "Jules", "Chen", manager="bigboss"),
        _user("arivera", R1_ID, "Ana", "Rivera", manager="bigboss"),
        _user("dsingh", R2_ID, "Dev", "Singh", manager="bigboss"),
    )

    result = await _plugin().fetch_team("jchen")

    assert result.ok
    assert result.data["found"] is True
    assert result.data["cohort"] == "peers"
    assert result.data["manager"] == "bigboss"
    # The cohort was searched by the MANAGER reference, not the subject login.
    assert 'profile.managerId eq "bigboss"' in route.calls.last.request.url.params["search"]

    logins = [m["login"] for m in result.data["members"]]
    assert logins[0] == "jchen"  # subject first
    assert set(logins) == {"jchen", "arivera", "dsingh"}
    assert result.data["count"] == 3
    subject = result.data["members"][0]
    assert subject["is_subject"] is True and subject["okta_id"] == SUBJECT_ID
    assert all(m["is_subject"] is False for m in result.data["members"][1:])


@respx.mock
async def test_non_manager_lookup_still_compares_peers_not_reports():
    """The headline requirement: an IC with no reports of their own still
    yields a comparison -- their teammates, found via their manager."""
    _mock_subject("aria", okta_id=SUBJECT_ID, manager="lead")
    _mock_cohort(
        _user("aria", SUBJECT_ID, "Aria", "Ng", manager="lead"),
        _user("bob", R1_ID, "Bob", "Fell", manager="lead"),
    )

    result = await _plugin().fetch_team("aria")

    assert result.data["cohort"] == "peers"
    assert {m["login"] for m in result.data["members"]} == {"aria", "bob"}
    # 'lead' is not in the comparison -- the manager is not a teammate.
    assert "lead" not in {m["login"] for m in result.data["members"]}


@respx.mock
async def test_subject_appears_even_if_the_cohort_search_omits_them():
    """Belt and braces: if the directory search doesn't echo the subject back,
    the person we were actually asked about must still be in the comparison."""
    _mock_subject("jchen", manager="bigboss")
    _mock_cohort(_user("arivera", R1_ID, "Ana", "Rivera", manager="bigboss"))

    result = await _plugin().fetch_team("jchen")

    logins = [m["login"] for m in result.data["members"]]
    assert "jchen" in logins
    assert result.data["members"][0]["login"] == "jchen"  # still first


@respx.mock
async def test_subject_without_a_manager_falls_back_to_their_reports():
    """Top-of-tree: no manager to key peers on, so compare their own reports."""
    _mock_subject("solo", manager=None)
    route = _mock_cohort(_user("intern", R1_ID, "In", "Tern", manager="solo"))

    result = await _plugin().fetch_team("solo")

    assert result.ok
    assert result.data["cohort"] == "reports"
    assert result.data["manager"] is None
    # Fallback keys the search on the subject's own login.
    assert 'eq "solo"' in route.calls.last.request.url.params["search"]
    assert {m["login"] for m in result.data["members"]} == {"solo", "intern"}


@respx.mock
async def test_unknown_subject_is_not_found_not_an_error():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_team("ghost")

    assert result.ok
    assert result.data["found"] is False
    assert "not-found" in result.tags


@respx.mock
async def test_manager_attribute_is_configurable():
    """Some orgs key the manager relationship on a custom attribute, and it is
    that attribute's value on the subject that the cohort is searched by."""
    config = PluginConfig(
        {"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "x", "OKTA_MANAGER_ATTRIBUTE": "managerEmail"}
    )
    subject = {
        "id": SUBJECT_ID,
        "status": "ACTIVE",
        "profile": {"login": "jchen", "email": "jchen@example.com", "managerEmail": "boss@example.com"},
    }
    respx.get(f"{USERS_URL}/jchen").mock(return_value=httpx.Response(200, json=subject))
    route = _mock_cohort(subject)

    await _plugin(config).fetch_team("jchen")

    sent = route.calls.last.request.url.params["search"]
    assert 'profile.managerEmail eq "boss@example.com"' in sent


# --- deactivated teammates ----------------------------------------------------


@respx.mock
async def test_deactivated_teammates_are_excluded_by_default():
    """The comparison is about who currently has access, so a deprovisioned
    teammate is dropped and counted rather than shown as a column of gaps."""
    _mock_subject("jchen", manager="bigboss")
    _mock_cohort(
        _user("jchen", SUBJECT_ID, "Jules", "Chen", manager="bigboss"),
        _user("arivera", R1_ID, "Ana", "Rivera", manager="bigboss"),
        _user("gone", R2_ID, "Gone", "Leaver", manager="bigboss", status="DEPROVISIONED"),
    )

    result = await _plugin().fetch_team("jchen")

    assert {m["login"] for m in result.data["members"]} == {"jchen", "arivera"}
    assert result.data["excluded_deactivated"] == 1


@respx.mock
async def test_include_deactivated_keeps_them():
    _mock_subject("jchen", manager="bigboss")
    _mock_cohort(
        _user("jchen", SUBJECT_ID, "Jules", "Chen", manager="bigboss"),
        _user("gone", R2_ID, "Gone", "Leaver", manager="bigboss", status="DEPROVISIONED"),
    )

    result = await _plugin().fetch_team("jchen", include_deactivated=True)

    assert "gone" in {m["login"] for m in result.data["members"]}
    assert result.data["excluded_deactivated"] == 0


@respx.mock
async def test_a_deactivated_subject_is_never_dropped():
    """You asked about this person by name; even deactivated, they stay in the
    comparison (checking whether *their* access was revoked is the point)."""
    subject = _user("jgone", SUBJECT_ID, "J", "Gone", manager="bigboss", status="DEPROVISIONED")
    respx.get(f"{USERS_URL}/jgone").mock(return_value=httpx.Response(200, json=subject))
    _mock_cohort(subject, _user("arivera", R1_ID, "Ana", "Rivera", manager="bigboss"))

    result = await _plugin().fetch_team("jgone")

    subject_member = next(m for m in result.data["members"] if m["is_subject"])
    assert subject_member["login"] == "jgone"
    assert result.data["excluded_deactivated"] == 0


@respx.mock
async def test_team_group_comparison_reports_excluded_deactivated_count():
    _mock_subject("jchen", manager="bigboss")
    _mock_cohort(
        _user("jchen", SUBJECT_ID, "Jules", "Chen", manager="bigboss"),
        _user("gone", R2_ID, "Gone", "Leaver", manager="bigboss", status="DEPROVISIONED"),
    )
    _mock_groups(SUBJECT_ID, "Everyone")

    result = await _plugin().fetch_team_groups("jchen")

    assert result.data["excluded_deactivated"] == 1
    assert {m["login"] for m in result.data["members"]} == {"jchen"}


# --- hire date -----------------------------------------------------------------


@respx.mock
async def test_members_carry_hire_date_preferring_attribute_then_created():
    """Hire date comes from the profile attribute, falling back to the Okta
    account `created` date so column ordering still works when it's unset."""
    subject = _user("jchen", SUBJECT_ID, "Jules", "Chen", manager="bigboss", hire="2021-03-01")
    respx.get(f"{USERS_URL}/jchen").mock(return_value=httpx.Response(200, json=subject))
    _mock_cohort(
        subject,
        # No hireDate, but an account created date -> used as the fallback.
        _user("arivera", R1_ID, "Ana", "Rivera", manager="bigboss", created="2019-01-01T00:00:00.000Z"),
    )

    result = await _plugin().fetch_team("jchen")

    by_login = {m["login"]: m for m in result.data["members"]}
    assert by_login["jchen"]["hire_date"] == "2021-03-01"
    assert by_login["arivera"]["hire_date"] == "2019-01-01T00:00:00.000Z"


@respx.mock
async def test_hire_date_attribute_is_configurable():
    config = PluginConfig(
        {"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "x", "OKTA_HIRE_DATE_ATTRIBUTE": "startDate"}
    )
    subject = {
        "id": SUBJECT_ID,
        "status": "ACTIVE",
        "profile": {"login": "jchen", "managerId": "bigboss", "startDate": "2022-07-07"},
    }
    respx.get(f"{USERS_URL}/jchen").mock(return_value=httpx.Response(200, json=subject))
    _mock_cohort(subject)

    result = await _plugin(config).fetch_team("jchen")

    assert result.data["members"][0]["hire_date"] == "2022-07-07"


# --- fetch_team_groups: the orchestration -------------------------------------


@respx.mock
async def test_team_group_comparison_lines_up_every_member():
    _mock_subject("jchen", manager="bigboss")
    _mock_cohort(
        _user("jchen", SUBJECT_ID, "Jules", "Chen", manager="bigboss"),
        _user("arivera", R1_ID, "Ana", "Rivera", manager="bigboss"),
        _user("dsingh", R2_ID, "Dev", "Singh", manager="bigboss"),
    )
    _mock_groups(SUBJECT_ID, "Everyone", "VPN-Users", "Okta-Admins")
    _mock_groups(R1_ID, "Everyone", "VPN-Users")
    _mock_groups(R2_ID, "Everyone")

    result = await _plugin().fetch_team_groups("jchen")

    assert result.ok
    assert result.data["found"] is True
    assert result.data["cohort"] == "peers"
    assert result.data["manager"] == "bigboss"
    summary = result.data["summary"]
    assert summary["member_count"] == 3
    assert summary["shared_by_all"] == 1  # Everyone
    assert summary["drift_count"] == 2    # VPN-Users, Okta-Admins


@respx.mock
async def test_one_members_failure_degrades_its_column_not_the_run():
    """A single member's forbidden groups call must not sink the comparison --
    the other members are still a real answer."""
    _mock_subject("jchen", manager="bigboss")
    _mock_cohort(
        _user("jchen", SUBJECT_ID, "Jules", "Chen", manager="bigboss"),
        _user("arivera", R1_ID, "Ana", "Rivera", manager="bigboss"),
    )
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
    comparison that reads as 'this person has no team'."""
    respx.get(f"{USERS_URL}/jchen").mock(return_value=httpx.Response(500))

    result = await _plugin().fetch_team_groups("jchen")

    assert not result.ok


async def test_team_group_comparison_mock_mode_shows_drift_without_network():
    """The mock demo must show the case the feature exists for -- a team that
    diverges -- via the peer-cohort path, not a flat matrix."""
    plugin = OktaPlugin(PluginConfig({"LOOKUP_CLI_MOCK_OKTA": "1"}))

    result = await plugin.fetch_team_groups("jchen")

    assert result.ok
    assert result.data["cohort"] == "peers"
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
    assert "http://" not in html and "https://" not in html
    assert "jchen" in html and "arivera" in html
    assert "Everyone" in html and "Okta-Admins" in html


def test_render_team_html_surfaces_the_drift_count():
    html = render_team_html(_sample_comparison(), subject_login="jchen")

    assert "drift" in html.lower()


def test_render_team_html_names_the_manager_in_peer_mode():
    html = render_team_html(
        _sample_comparison(), subject_login="jchen", manager="bigboss", cohort="peers"
    )

    assert "bigboss" in html


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


def test_render_team_html_escapes_the_manager_reference():
    html = render_team_html(
        _sample_comparison(),
        subject_login="jchen",
        manager="<script>alert(3)</script>",
        cohort="peers",
    )

    assert "<script>alert(3)</script>" not in html


def _dated_member(login: str, hire: str | None, groups, is_subject: bool = False) -> dict:
    return {"login": login, "name": login.title(), "is_subject": is_subject,
            "hire_date": hire, "groups": groups, "error": None}


def _heatmap_comparison() -> dict:
    return build_comparison(
        [
            _dated_member("aold", "2020-01-01", ["Everyone", "Rare"], is_subject=True),
            _dated_member("bmid", "2023-01-01", ["Everyone"]),
            _dated_member("cnew", "2025-01-01", ["Everyone"]),
            _dated_member("dunk", None, ["Everyone"]),  # unknown hire date
        ]
    )


def test_render_team_html_orders_columns_oldest_hire_date_first():
    html = render_team_html(_heatmap_comparison(), subject_login="aold")

    # Column headers appear left-to-right in document order: oldest first,
    # unknown-date member last.
    assert html.index("aold") < html.index("bmid") < html.index("cnew") < html.index("dunk")


def test_render_team_html_orders_rows_most_shared_first():
    html = render_team_html(_heatmap_comparison(), subject_login="aold")

    # 'Everyone' (all four) sits above 'Rare' (only one).
    assert html.index(">Everyone<") < html.index(">Rare<")


def test_render_team_html_has_a_prevalence_legend_and_band_colours():
    html = render_team_html(_heatmap_comparison(), subject_login="aold")

    assert "shared by all 4" in html
    assert "unique to 1 member" in html
    # Rows are shaded by an inline heat-map colour.
    assert "hsl(" in html
    # The individual-access divider from the reference sheet.
    assert "unique to one member" in html


def test_render_team_html_accents_the_subject_column():
    html = render_team_html(_heatmap_comparison(), subject_login="aold")

    assert "who subj" in html
    # The subject's whole column carries the highlight class, not just the header.
    assert html.count("subjcol") > 1


def test_render_team_html_shows_each_members_hire_date():
    html = render_team_html(_heatmap_comparison(), subject_login="aold")

    assert "2020-01-01" in html
    assert "2023-01-01" in html
    assert "2025-01-01" in html


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

    assert "arivera" in html
    assert "unavailable" in html.lower()
