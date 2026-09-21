"""
Stage 3: Jira connector -- issues assigned to and reported by a person.

Three facts about Jira Cloud drive this module's shape, all established by
probing the real instance rather than from documentation:

1. **`/rest/api/3/search` is gone.** It answers `410 Gone` and points at
   `/rest/api/3/search/jql`. Most tutorials still show the old one.
2. **The new endpoint refuses unbounded JQL** ("Please add a search
   restriction to your query"), and its paging is cursor-based
   (`nextPageToken` / `isLast`) rather than `startAt` offsets. Critically it
   returns **no `total`** -- there is no cheap way to say "247 tickets", so
   the CLI must not pretend to know a count it was never given.
3. **JQL cannot take a username.** GDPR-era changes removed usernames and
   emails from JQL, so a lookup is two steps: resolve the identifier to an
   `accountId` via `/user/search`, then query with that.

Scope decision (2026-09-07): the default view is **assigned** issues, with
reported available separately. Assigned is the actionable set -- open work
that needs reassigning when someone leaves, or that says what they are
stuck on. Reported is historical. On the real instance the two differ
substantially for the same person, so they are never conflated.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m jira
"""

from __future__ import annotations

import httpx
import pytest
import respx
from jira_plugin.plugin import JiraPlugin, build_jql

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jira

BASE = "https://example.atlassian.net"
USER_SEARCH_URL = f"{BASE}/rest/api/3/user/search"
SEARCH_URL = f"{BASE}/rest/api/3/search/jql"
ACCOUNT_ID = "712020:00000000-0000-0000-0000-000000000001"

CONFIG = PluginConfig({
    "JIRA_BASE_URL": BASE,
    "JIRA_EMAIL": "svc@example.com",
    "JIRA_API_TOKEN": "not-a-real-token",
})


def _account(account_id=ACCOUNT_ID, name="Dana Example", active=True):
    return {
        "accountId": account_id,
        "displayName": name,
        "emailAddress": "dana@example.com",
        "accountType": "atlassian",
        "active": active,
    }


def _issue(key="ENG-1", summary="Fix the thing", status="In Progress",
           category="In Progress", project="ENG", priority="High"):
    return {
        "id": "10001",
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status, "statusCategory": {"name": category}},
            "project": {"key": project, "name": "Engineering"},
            "priority": {"name": priority},
            "updated": "2026-09-01T10:00:00.000+0000",
        },
    }


def _page(issues, is_last=True, token=None):
    body = {"issues": issues, "isLast": is_last}
    if token:
        body["nextPageToken"] = token
    return body


def _plugin(config: PluginConfig = CONFIG) -> JiraPlugin:
    return JiraPlugin(config)


_DEFAULT = object()


def _mock_user(*accounts):
    """No args -> one default account. `_mock_user(*[])` can't express "none",
    so use `_mock_no_user()` for that."""
    return respx.get(USER_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=list(accounts) if accounts else [_account()])
    )


def _mock_no_user():
    return respx.get(USER_SEARCH_URL).mock(return_value=httpx.Response(200, json=[]))


# --- Contract ---------------------------------------------------------------


def test_required_credentials_are_declared():
    assert set(JiraPlugin.required_credentials) == {
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"
    }


# --- JQL construction --------------------------------------------------------


def test_jql_queries_by_account_id_not_username():
    """JQL has not accepted usernames since the GDPR changes; passing one
    silently matches nothing rather than erroring."""
    jql = build_jql("assignee", ACCOUNT_ID)

    assert ACCOUNT_ID in jql
    assert "assignee" in jql


def test_jql_is_bounded():
    """The endpoint rejects unbounded queries outright, so every query this
    module builds must carry a restriction."""
    jql = build_jql("assignee", ACCOUNT_ID)

    assert jql.strip() != ""
    assert "=" in jql


def test_jql_orders_by_most_recently_updated():
    """Without an explicit order the first page is arbitrary, and the first
    page is all most people will read."""
    assert "ORDER BY updated DESC" in build_jql("assignee", ACCOUNT_ID)


def test_jql_quotes_the_account_id():
    """accountIds contain a colon, which is a JQL operator."""
    assert f'"{ACCOUNT_ID}"' in build_jql("assignee", ACCOUNT_ID)


def test_jql_rejects_an_unknown_relationship():
    """Guards against a caller smuggling arbitrary JQL through the field."""
    with pytest.raises(ValueError):
        build_jql("summary ~ evil OR assignee", ACCOUNT_ID)


@pytest.mark.parametrize("relationship", ["assignee", "reporter"])
def test_both_relationships_are_supported(relationship):
    assert relationship in build_jql(relationship, ACCOUNT_ID)


# --- Resolving a person ------------------------------------------------------


@respx.mock
async def test_an_identifier_is_resolved_to_an_account_id():
    _mock_user()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([_issue()])))

    result = await _plugin().fetch("dana@example.com")

    assert result.ok
    assert result.data["found"] is True
    assert result.data["account"]["display_name"] == "Dana Example"


@respx.mock
async def test_an_unknown_person_is_a_success_not_an_error():
    """Consistent with Okta: "no Jira account" is a real answer, and an
    error= would be indistinguishable from "Jira was unreachable"."""
    _mock_no_user()

    result = await _plugin().fetch("ghost@example.com")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["issues"] == []
    assert "not-found" in result.tags


@respx.mock
async def test_several_matching_people_is_ambiguous_not_a_guess():
    """Picking the first would attribute someone else's tickets to the
    person you asked about."""
    _mock_user(_account("acct-1", "Dana Example"), _account("acct-2", "Dana Examplé"))

    result = await _plugin().fetch("dana")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["ambiguous"] is True
    assert len(result.data["candidates"]) == 2


@respx.mock
async def test_an_inactive_account_is_still_resolved():
    """A deactivated Jira user is exactly who an offboarding check asks
    about; skipping them would answer the wrong question."""
    _mock_user(_account(active=False))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    result = await _plugin().fetch("dana@example.com")

    assert result.data["found"] is True
    assert result.data["account"]["active"] is False


@respx.mock
async def test_the_user_search_is_not_repeated_when_an_account_id_is_given():
    user_route = _mock_user()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("dana@example.com", account_id=ACCOUNT_ID)

    assert not user_route.called


# --- Issues ------------------------------------------------------------------


@respx.mock
async def test_issues_are_returned_normalised():
    _mock_user()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([_issue()])))

    result = await _plugin().fetch("dana@example.com")

    issue = result.data["issues"][0]
    assert issue["key"] == "ENG-1"
    assert issue["summary"] == "Fix the thing"
    assert issue["status"] == "In Progress"
    assert issue["project"] == "ENG"
    assert issue["priority"] == "High"


@respx.mock
async def test_the_default_relationship_is_assignee():
    """Decided 2026-09-07: assigned is the actionable set."""
    _mock_user()
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("dana@example.com")

    assert "assignee" in route.calls.last.request.url.params["jql"]


@respx.mock
async def test_reported_issues_can_be_requested_instead():
    _mock_user()
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("dana@example.com", relationship="reporter")

    assert "reporter" in route.calls.last.request.url.params["jql"]


@respx.mock
async def test_missing_optional_fields_do_not_crash():
    _mock_user()
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_page([{"key": "ENG-9", "fields": {}}]))
    )

    result = await _plugin().fetch("dana@example.com")

    assert result.ok
    assert result.data["issues"][0]["key"] == "ENG-9"
    assert result.data["issues"][0]["priority"] is None


@respx.mock
async def test_a_person_with_no_issues_is_a_success():
    _mock_user()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    result = await _plugin().fetch("dana@example.com")

    assert result.ok
    assert result.data["issues"] == []
    assert "no-issues" in result.tags


# --- The missing `total` -----------------------------------------------------


@respx.mock
async def test_a_complete_page_reports_a_known_count():
    _mock_user()
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_page([_issue()], is_last=True))
    )

    result = await _plugin().fetch("dana@example.com")

    assert result.data["count"] == 1
    assert result.data["complete"] is True


@respx.mock
async def test_an_incomplete_page_never_claims_a_total():
    """The API returns no `total`. Reporting the page size as the count
    would understate someone's workload -- and this tool exists to catch
    exactly that kind of quietly-wrong answer."""
    _mock_user()
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_page([_issue()], is_last=False, token="tok"))
    )

    result = await _plugin().fetch("dana@example.com")

    assert result.data["complete"] is False


@respx.mock
async def test_all_pages_through_the_cursor():
    _mock_user()
    respx.get(SEARCH_URL, params={"nextPageToken": "tok"}).mock(
        return_value=httpx.Response(200, json=_page([_issue("ENG-2")], is_last=True))
    )
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_page([_issue("ENG-1")], is_last=False, token="tok"))
    )

    result = await _plugin().fetch("dana@example.com", fetch_all=True)

    assert {i["key"] for i in result.data["issues"]} == {"ENG-1", "ENG-2"}
    assert result.data["complete"] is True


@respx.mock
async def test_without_all_only_one_page_is_fetched():
    _mock_user()
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_page([_issue()], is_last=False, token="tok"))
    )

    await _plugin().fetch("dana@example.com")

    assert route.call_count == 1


# --- Failure modes -----------------------------------------------------------


@respx.mock
async def test_the_removed_search_endpoint_is_never_called():
    """`/rest/api/3/search` answers 410 Gone on Jira Cloud. Calling it would
    fail for every user, so this pins the migration."""
    _mock_user()
    old = respx.get(f"{BASE}/rest/api/3/search").mock(return_value=httpx.Response(410))
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("dana@example.com")

    assert not old.called


@respx.mock
async def test_unauthorized_is_an_actionable_error():
    respx.get(USER_SEARCH_URL).mock(return_value=httpx.Response(401))

    result = await _plugin().fetch("dana@example.com")

    assert not result.ok
    assert "jira" in result.error.lower() or "token" in result.error.lower()


@respx.mock
async def test_a_rejected_jql_query_is_an_actionable_error():
    _mock_user()
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(400, json={"errorMessages": ["Unbounded JQL"]})
    )

    result = await _plugin().fetch("dana@example.com")

    assert not result.ok
    assert "jql" in result.error.lower() or "query" in result.error.lower()


@respx.mock
async def test_rate_limited_is_an_error_result():
    _mock_user()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))

    result = await _plugin().fetch("dana@example.com")

    assert not result.ok


@respx.mock
async def test_timeout_is_an_error_result():
    respx.get(USER_SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch("dana@example.com")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await JiraPlugin(PluginConfig({})).fetch("dana@example.com")

    assert not result.ok
    assert "JIRA_BASE_URL" in result.error


@respx.mock
async def test_the_api_token_never_appears_in_an_error():
    token = "s3cr3t-jira-token-value"
    config = PluginConfig({
        "JIRA_BASE_URL": BASE, "JIRA_EMAIL": "svc@example.com", "JIRA_API_TOKEN": token,
    })
    respx.get(USER_SEARCH_URL).mock(side_effect=httpx.HTTPError(f"failed using Basic {token}"))

    result = await _plugin(config).fetch("dana@example.com")

    assert token not in result.error


@respx.mock
async def test_basic_auth_is_used():
    route = _mock_user()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin().fetch("dana@example.com")

    assert route.calls.last.request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    config = PluginConfig({
        "JIRA_BASE_URL": f"{BASE}/", "JIRA_EMAIL": "svc@example.com", "JIRA_API_TOKEN": "t",
    })
    route = _mock_user()
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=_page([])))

    await _plugin(config).fetch("dana@example.com")

    assert route.called


# --- Mock mode ---------------------------------------------------------------


async def test_mock_mode_works_with_no_credentials():
    plugin = JiraPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JIRA": "1"}))

    result = await plugin.fetch("dana@example.com")

    assert result.ok
    assert result.data["issues"]
