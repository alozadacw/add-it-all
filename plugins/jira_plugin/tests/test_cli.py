"""
Stage 3: the `add-it-all jira <identifier> [flags]` CLI surface.

Same shape as every other connector: identifier as a direct argument, no
noun subcommands, flags select sections and bundle.

    jira dana            assigned issues (the primary view)
    jira dana -t         assigned, explicitly
    jira dana -r         reported
    jira dana -tr        both

Run just this stage:  pytest -m jira
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx
from jira_plugin.plugin import JiraPlugin
from typer.testing import CliRunner

from add_it_all.cli import build_app
from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jira

BASE = "https://example.atlassian.net"
USER_SEARCH_URL = f"{BASE}/rest/api/3/user/search"
SEARCH_URL = f"{BASE}/rest/api/3/search/jql"

CONFIG = PluginConfig({
    "JIRA_BASE_URL": BASE, "JIRA_EMAIL": "svc@example.com", "JIRA_API_TOKEN": "not-a-real-token",
})
MOCK_CONFIG = PluginConfig({"ADD_IT_ALL_MOCK_JIRA": "1"})

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    return _ANSI.sub("", result.stdout)


def _app(config: PluginConfig = CONFIG):
    return build_app({"jira": JiraPlugin(config)})


def _account(account_id="acct-1", name="Dana Example", active=True):
    return {"accountId": account_id, "displayName": name, "emailAddress": "dana@example.com",
            "accountType": "atlassian", "active": active}


def _issue(key="ENG-1", summary="Fix the thing", status="In Progress", project="ENG"):
    return {"id": "1", "key": key, "fields": {
        "summary": summary,
        "status": {"name": status, "statusCategory": {"name": "In Progress"}},
        "project": {"key": project, "name": "Engineering"},
        "priority": {"name": "High"},
        "updated": "2026-09-01T10:00:00.000+0000"}}


def _mock(issues=None, is_last=True, accounts=None):
    respx.get(USER_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=accounts if accounts is not None else [_account()]))
    body = {"issues": issues if issues is not None else [_issue()], "isLast": is_last}
    if not is_last:
        body["nextPageToken"] = "tok"
    return respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=body))


# --- Default view ------------------------------------------------------------


@respx.mock
def test_identifier_alone_shows_assigned_issues():
    _mock()

    result = runner.invoke(_app(), ["jira", "dana@example.com"])

    assert result.exit_code == 0
    out = _out(result)
    assert "ENG-1" in out
    assert "Fix the thing" in out


@respx.mock
def test_the_default_view_is_labelled_assigned():
    """A table of tickets is ambiguous unless it says which relationship it
    is showing -- assigned and reported are different populations."""
    _mock()

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com"]))

    assert "assigned" in out.lower()


def test_no_identifier_is_a_usage_error():
    assert runner.invoke(_app(), ["jira"]).exit_code == 2


@respx.mock
def test_no_issues_says_so():
    _mock(issues=[])

    result = runner.invoke(_app(), ["jira", "dana@example.com"])

    assert result.exit_code == 0
    assert "no" in _out(result).lower()


# --- Sections and bundling ---------------------------------------------------


@pytest.mark.parametrize("flag", ["-t", "-tickets", "--tickets"])
@respx.mock
def test_every_assigned_spelling_works(flag):
    _mock()

    assert runner.invoke(_app(), ["jira", "dana@example.com", flag]).exit_code == 0


@pytest.mark.parametrize("flag", ["-r", "-reported", "--reported"])
@respx.mock
def test_every_reported_spelling_works(flag):
    _mock()

    result = runner.invoke(_app(), ["jira", "dana@example.com", flag])

    assert result.exit_code == 0
    assert "reported" in _out(result).lower()


@respx.mock
def test_reported_alone_does_not_show_assigned():
    """Flags select sections, as everywhere else in this CLI."""
    _mock()

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com", "-r"]))

    assert "reported" in out.lower()
    assert "assigned" not in out.lower()


@respx.mock
def test_bundled_flags_show_both_sections():
    _mock()

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com", "-tr"]))

    assert "assigned" in out.lower()
    assert "reported" in out.lower()


@respx.mock
def test_both_sections_resolve_the_person_only_once():
    user_route = respx.get(USER_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=[_account()]))
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"issues": [_issue()], "isLast": True}))

    runner.invoke(_app(), ["jira", "dana@example.com", "-tr"])

    assert user_route.call_count == 1


@respx.mock
def test_flags_may_precede_the_identifier():
    _mock()

    assert runner.invoke(_app(), ["jira", "-r", "dana@example.com"]).exit_code == 0


# --- The missing total -------------------------------------------------------


@respx.mock
def test_a_complete_result_states_the_count():
    _mock(issues=[_issue("ENG-1"), _issue("ENG-2")])

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com"]))

    assert "2" in out


@respx.mock
def test_an_incomplete_result_never_shows_a_bare_count():
    """Jira's new search API returns no `total`. Printing the page size as
    though it were the count would understate someone's workload."""
    _mock(issues=[_issue(f"ENG-{i}") for i in range(5)], is_last=False)

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com"]))

    assert "at least" in out.lower() or "more" in out.lower()


@respx.mock
def test_the_incomplete_message_names_the_escape_hatch():
    _mock(issues=[_issue(f"ENG-{i}") for i in range(5)], is_last=False)

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com"]))

    assert "--all" in out


@respx.mock
def test_all_pages_through_and_reports_a_real_count():
    respx.get(USER_SEARCH_URL).mock(return_value=httpx.Response(200, json=[_account()]))
    respx.get(SEARCH_URL, params={"nextPageToken": "tok"}).mock(
        return_value=httpx.Response(200, json={"issues": [_issue("ENG-2")], "isLast": True}))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(
        200, json={"issues": [_issue("ENG-1")], "isLast": False, "nextPageToken": "tok"}))

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com", "--all"]))

    assert "ENG-1" in out and "ENG-2" in out
    assert "at least" not in out.lower()


@respx.mock
def test_a_long_list_is_capped_and_names_the_escape_hatch():
    from jira_plugin.plugin import MAX_ISSUES_SHOWN
    _mock(issues=[_issue(f"ENG-{i}") for i in range(40)])

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com"]))

    assert f"ENG-{MAX_ISSUES_SHOWN - 1}" in out
    assert f"ENG-{MAX_ISSUES_SHOWN}" not in out
    assert "more not shown" in out
    assert "--all" in out


@respx.mock
def test_all_shows_every_row_not_just_the_first_screenful():
    """Caught live: --all fetched 236 issues, rendered 15, and told the user
    to "use --all" -- advice they had just followed."""
    _mock(issues=[_issue(f"ENG-{i}") for i in range(40)])

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com", "--all"]))

    assert "ENG-0" in out
    assert "ENG-39" in out


@respx.mock
def test_all_never_suggests_using_all():
    _mock(issues=[_issue(f"ENG-{i}") for i in range(40)])

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com", "--all"]))

    assert "more not shown" not in out
    assert "--all" not in out, "must not advise a flag already in use"


# --- Ambiguity and failures --------------------------------------------------


@respx.mock
def test_several_matches_print_a_chooser_not_a_guess():
    _mock(accounts=[_account("acct-1", "Dana Example"),
                    _account("acct-2", "Dana Other")])

    result = runner.invoke(_app(), ["jira", "dana"])

    assert result.exit_code == 0
    out = _out(result)
    assert "Dana Example" in out
    assert "Dana Other" in out


@respx.mock
def test_an_unknown_person_says_so():
    _mock(accounts=[])

    result = runner.invoke(_app(), ["jira", "ghost@example.com"])

    assert result.exit_code == 0
    assert "no jira" in _out(result).lower()


@respx.mock
def test_an_api_failure_exits_non_zero():
    respx.get(USER_SEARCH_URL).mock(return_value=httpx.Response(503))

    assert runner.invoke(_app(), ["jira", "dana@example.com"]).exit_code == 1


def test_missing_credentials_exit_non_zero_with_an_actionable_message():
    result = runner.invoke(build_app({"jira": JiraPlugin(PluginConfig({}))}), ["jira", "x"])

    assert result.exit_code == 1
    assert "JIRA_BASE_URL" in _out(result)


@respx.mock
def test_an_inactive_jira_account_is_called_out():
    """The state an offboarding check is asking about."""
    _mock(accounts=[_account(active=False)])

    out = _out(runner.invoke(_app(), ["jira", "dana@example.com"]))

    assert "inactive" in out.lower() or "deactivated" in out.lower()


# --- Looking a ticket up by key ------------------------------------------------

ISSUE_URL = f"{BASE}/rest/api/3/issue"


def _adf(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _full_issue(key="ENG-1"):
    return {"id": "1", "key": key, "fields": {
        "summary": "Cloud console admin access",
        "status": {"name": "Blocked", "statusCategory": {"name": "In Progress"}},
        "project": {"key": "ENG", "name": "Engineering"},
        "issuetype": {"name": "Task"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Dana Example", "emailAddress": "dana@example.com"},
        "reporter": {"displayName": "Ravi Patel", "emailAddress": "ravi@example.com"},
        "creator": {"displayName": "Ravi Patel", "emailAddress": "ravi@example.com"},
        "created": "2026-08-01T09:00:00.000+0000",
        "updated": "2026-09-08T14:00:00.000+0000",
        "resolution": None, "labels": ["access"],
        "description": _adf("Please provision staging access."),
    }}


def _mock_issue(key="ENG-1", status=200, body=None):
    return respx.get(f"{ISSUE_URL}/{key}").mock(
        return_value=httpx.Response(status, json=body if body is not None else _full_issue(key)))


@respx.mock
def test_an_issue_key_is_looked_up_as_an_issue():
    _mock_issue()

    result = runner.invoke(_app(), ["jira", "ENG-1"])

    assert result.exit_code == 0
    out = _out(result)
    assert "ENG-1" in out
    assert "Cloud console admin access" in out


@respx.mock
def test_an_issue_key_does_not_trigger_a_person_search():
    """The two paths are mutually exclusive; searching for a person named
    'ENG-1' would be a confidently wrong answer."""
    _mock_issue()
    user_route = respx.get(USER_SEARCH_URL).mock(return_value=httpx.Response(200, json=[]))

    runner.invoke(_app(), ["jira", "ENG-1"])

    assert not user_route.called


@respx.mock
def test_a_lowercase_key_still_reaches_the_issue_path():
    _mock_issue("eng-1")
    user_route = respx.get(USER_SEARCH_URL).mock(return_value=httpx.Response(200, json=[]))

    result = runner.invoke(_app(), ["jira", "eng-1"])

    assert result.exit_code == 0
    assert not user_route.called


@respx.mock
def test_an_email_still_reaches_the_person_path():
    _mock()
    issue_route = respx.get(url__startswith=ISSUE_URL).mock(
        return_value=httpx.Response(200, json=_full_issue()))

    runner.invoke(_app(), ["jira", "dana@example.com"])

    assert not issue_route.called


@respx.mock
def test_the_issue_view_shows_the_people_on_the_ticket():
    """This is a tool about people; assignee and reporter are the fields
    that let you pivot to another connector."""
    _mock_issue()

    out = _out(runner.invoke(_app(), ["jira", "ENG-1"]))

    assert "Dana Example" in out
    assert "Ravi Patel" in out


@respx.mock
def test_the_issue_view_shows_the_description():
    _mock_issue()

    out = _out(runner.invoke(_app(), ["jira", "ENG-1"]))

    assert "Please provision staging access." in out


@respx.mock
def test_an_unknown_key_explains_it_may_be_a_permission_problem():
    _mock_issue("ENG-9999", status=404, body={"errorMessages": ["Issue does not exist"]})

    result = runner.invoke(_app(), ["jira", "ENG-9999"])

    assert result.exit_code == 0
    out = _out(result).lower()
    assert "eng-9999" in out
    assert "permission" in out


@pytest.mark.parametrize("flag", ["-t", "-r", "-tr", "--all"])
@respx.mock
def test_section_flags_are_rejected_for_an_issue_key(flag):
    """`-r` means "reported by this person" -- meaningless for a ticket.
    Silently ignoring it would leave someone believing they asked for
    something."""
    _mock_issue()

    result = runner.invoke(_app(), ["jira", "ENG-1", flag])

    assert result.exit_code == 2
    assert "ENG-1" in _out(result)


@respx.mock
def test_the_issue_view_stays_readable_at_80_columns():
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock_issue()

    out = _ANSI.sub("", narrow.invoke(_app(), ["jira", "ENG-1"]).stdout)

    assert "ENG-1" in out
    assert "Dana Example" in out


def test_mock_mode_end_to_end_with_an_issue_key():
    result = runner.invoke(_app(MOCK_CONFIG), ["jira", "MOCK-1"])

    assert result.exit_code == 0


# --- Help, layout, mock mode -------------------------------------------------


def test_help_documents_both_sections():
    out = _out(runner.invoke(_app(), ["jira", "--help"]))

    for expected in ("-t", "--tickets", "-r", "--reported", "--all"):
        assert expected in out


def test_mock_mode_end_to_end():
    result = runner.invoke(_app(MOCK_CONFIG), ["jira", "dana@example.com", "-tr"])

    assert result.exit_code == 0


@respx.mock
def test_the_table_stays_readable_at_80_columns():
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock(issues=[_issue("ENG-1234", "A fairly long ticket summary that will need wrapping")])

    out = _ANSI.sub("", narrow.invoke(_app(), ["jira", "dana@example.com"]).stdout)

    assert "ENG-1234" in out, "issue key must never be squeezed out"
