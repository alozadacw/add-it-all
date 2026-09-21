"""
Stage 3: looking a single issue up by key -- `add-it-all jira ENG-42`.

The identifier is normally a person. An issue key is distinctive enough
(`ABC-123`) that it can be told apart without guessing: an email always
carries an `@`, and a display name never has the letters-hyphen-digits
shape. So the key is detected rather than hidden behind a flag.

Detection is **case-insensitive**, because Jira itself is: the live API
answers 200 for `eng-42` as readily as `ENG-42`. A case-sensitive
pattern would send the lowercase spelling down the person-search path and
fail confusingly.

There is deliberately **no fallback** from a failed key lookup to a person
search. A string shaped like an issue key that Jira does not know is a
typo, not a colleague, and silently searching for a person named
"ENG-42" would be the kind of confident-wrong answer this tool exists
to avoid.

Run just this stage:  pytest -m jira
"""

from __future__ import annotations

import httpx
import pytest
import respx
from jira_plugin.plugin import JiraPlugin, flatten_adf, looks_like_issue_key

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jira

BASE = "https://example.atlassian.net"
ISSUE_URL = f"{BASE}/rest/api/3/issue"

CONFIG = PluginConfig({
    "JIRA_BASE_URL": BASE, "JIRA_EMAIL": "svc@example.com", "JIRA_API_TOKEN": "not-a-real-token",
})


def _person(name):
    return {"displayName": name, "emailAddress": f"{name.split()[0].lower()}@example.com"}


def _adf(text):
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _issue(key="ENG-1", assignee="Dana Example", reporter="Ravi Patel",
           creator=None, resolution=None, description="Please provision access."):
    return {
        "id": "10001", "key": key,
        "fields": {
            "summary": "Cloud console admin access",
            "status": {"name": "Blocked", "statusCategory": {"name": "In Progress"}},
            "project": {"key": "ENG", "name": "Engineering"},
            "issuetype": {"name": "Task"},
            "priority": {"name": "High"},
            "assignee": _person(assignee) if assignee else None,
            "reporter": _person(reporter) if reporter else None,
            "creator": _person(creator) if creator else _person(reporter),
            "created": "2026-08-01T09:00:00.000+0000",
            "updated": "2026-09-08T14:00:00.000+0000",
            "resolution": {"name": resolution} if resolution else None,
            "labels": ["access", "cloud"],
            "description": _adf(description) if description else None,
        },
    }


def _plugin(config: PluginConfig = CONFIG) -> JiraPlugin:
    return JiraPlugin(config)


# --- Detecting an issue key --------------------------------------------------


@pytest.mark.parametrize(
    "value", ["ENG-1", "ENG-42", "ABC123-42", "A-1", "eng-1", "EnG-99"]
)
def test_issue_keys_are_recognised(value):
    assert looks_like_issue_key(value) is True


def test_detection_is_case_insensitive():
    """Jira answers 200 for a lowercase key, so the CLI must too -- otherwise
    `jira eng-42` silently becomes a person search."""
    assert looks_like_issue_key("eng-42") is True


@pytest.mark.parametrize(
    "value",
    [
        "dana@example.com",      # email -- has @
        "Dana Example",          # display name -- has a space
        "dana",                  # bare username
        "ENG-",                  # no number
        "-1",                    # no project
        "1ENG-1",                # project can't start with a digit
        "ENG 1",                 # space
        "",
        "ENG-1-2",               # not a key
    ],
)
def test_person_identifiers_are_not_mistaken_for_keys(value):
    assert looks_like_issue_key(value) is False


def test_surrounding_whitespace_is_tolerated():
    assert looks_like_issue_key("  ENG-1  ") is True


# --- Flattening Atlassian Document Format ------------------------------------


def test_adf_is_flattened_to_plain_text():
    """API v3 returns descriptions as a nested ADF document, not a string.
    Rendering the raw JSON would be unreadable."""
    assert flatten_adf(_adf("Hello there")) == "Hello there"


def test_adjacent_blocks_get_a_separator():
    """Caught live: two paragraphs rendered as "instance:1. Add/set up" --
    the sentence end and the next block ran together into a single word."""
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "changes:"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "1. Add keys"}]},
    ]}

    assert flatten_adf(doc) == "changes: 1. Add keys"


def test_list_items_are_separated_from_each_other():
    doc = {"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
        ]}]}

    assert flatten_adf(doc) == "one two"


def test_inline_runs_within_a_paragraph_are_not_separated():
    """Bold/plain runs inside one sentence are separate text nodes but one
    word sequence -- a space between them would break words apart."""
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "hello "},
        {"type": "text", "text": "world", "marks": [{"type": "strong"}]},
    ]}]}

    assert flatten_adf(doc) == "hello world"


def test_a_hard_break_becomes_a_space():
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [
        {"type": "text", "text": "a"}, {"type": "hardBreak"}, {"type": "text", "text": "b"},
    ]}]}

    assert flatten_adf(doc) == "a b"


def test_nested_adf_blocks_are_joined():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "First."}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Second."}]}]}]},
    ]}

    out = flatten_adf(doc)

    assert "First." in out
    assert "Second." in out


def test_adf_whitespace_is_collapsed():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "a\n\n  b"}]}]}

    assert flatten_adf(doc) == "a b"


def test_a_missing_description_is_none_not_an_empty_string():
    """`-` in the table means "no description"; "" would look like an empty
    one, which is a different thing."""
    assert flatten_adf(None) is None


def test_an_adf_document_with_no_text_is_none():
    assert flatten_adf({"type": "doc", "content": []}) is None


def test_unknown_adf_node_types_do_not_crash():
    doc = {"type": "doc", "content": [{"type": "mediaSingle", "attrs": {"id": "x"}}]}

    assert flatten_adf(doc) is None


# --- Fetching one issue ------------------------------------------------------


@respx.mock
async def test_an_issue_is_returned_normalised():
    respx.get(f"{ISSUE_URL}/ENG-1").mock(return_value=httpx.Response(200, json=_issue()))

    result = await _plugin().fetch_issue("ENG-1")

    assert result.ok
    issue = result.data["issue"]
    assert issue["key"] == "ENG-1"
    assert issue["summary"] == "Cloud console admin access"
    assert issue["status"] == "Blocked"
    assert issue["project"] == "ENG"
    assert issue["issue_type"] == "Task"
    assert issue["assignee"] == "Dana Example"
    assert issue["reporter"] == "Ravi Patel"


@respx.mock
async def test_the_description_is_flattened_onto_the_issue():
    respx.get(f"{ISSUE_URL}/ENG-1").mock(
        return_value=httpx.Response(200, json=_issue(description="Grant staging access."))
    )

    result = await _plugin().fetch_issue("ENG-1")

    assert result.data["issue"]["description"] == "Grant staging access."


@respx.mock
async def test_creator_is_carried_because_it_can_differ_from_reporter():
    """Someone raising a ticket on a colleague's behalf is common, and for a
    tool about people that difference is the interesting part."""
    respx.get(f"{ISSUE_URL}/ENG-1").mock(
        return_value=httpx.Response(200, json=_issue(reporter="Ravi Patel", creator="Sam Okafor"))
    )

    result = await _plugin().fetch_issue("ENG-1")

    assert result.data["issue"]["reporter"] == "Ravi Patel"
    assert result.data["issue"]["creator"] == "Sam Okafor"


@respx.mock
async def test_an_unassigned_issue_does_not_crash():
    respx.get(f"{ISSUE_URL}/ENG-1").mock(
        return_value=httpx.Response(200, json=_issue(assignee=None))
    )

    result = await _plugin().fetch_issue("ENG-1")

    assert result.ok
    assert result.data["issue"]["assignee"] is None


@respx.mock
async def test_a_lowercase_key_is_looked_up_as_typed():
    """Jira is case-insensitive, so no normalisation is needed on the wire."""
    route = respx.get(f"{ISSUE_URL}/eng-1").mock(
        return_value=httpx.Response(200, json=_issue())
    )

    await _plugin().fetch_issue("eng-1")

    assert route.called


@respx.mock
async def test_only_the_rendered_fields_are_requested():
    """The full issue is 46KB on the live instance -- roughly 90 custom
    fields, comments, worklog and attachments. Everything in `data` reaches
    the plaintext local cache, so unrendered fields are not asked for."""
    route = respx.get(f"{ISSUE_URL}/ENG-1").mock(return_value=httpx.Response(200, json=_issue()))

    await _plugin().fetch_issue("ENG-1")

    fields = route.calls.last.request.url.params["fields"]
    assert "summary" in fields
    assert "comment" not in fields
    assert "worklog" not in fields
    assert "attachment" not in fields


# --- Failure modes -----------------------------------------------------------


@respx.mock
async def test_an_unknown_key_is_a_success_with_found_false():
    respx.get(f"{ISSUE_URL}/ENG-9999").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_issue("ENG-9999")

    assert result.ok
    assert result.data["found"] is False
    assert "not-found" in result.tags


@respx.mock
async def test_not_found_wording_admits_it_may_be_a_permission_problem():
    """Jira returns 404 both for issues that don't exist and for issues the
    caller can't see -- deliberately, so existence isn't leaked. Saying only
    "no such issue" would send someone hunting for a typo that isn't there."""
    respx.get(f"{ISSUE_URL}/ENG-9999").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_issue("ENG-9999")

    assert "permission" in result.data["not_found_reason"].lower()


@respx.mock
async def test_unauthorized_is_an_error_not_a_missing_issue():
    respx.get(f"{ISSUE_URL}/ENG-1").mock(return_value=httpx.Response(401))

    result = await _plugin().fetch_issue("ENG-1")

    assert not result.ok


@respx.mock
async def test_timeout_is_an_error_result():
    respx.get(f"{ISSUE_URL}/ENG-1").mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch_issue("ENG-1")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await JiraPlugin(PluginConfig({})).fetch_issue("ENG-1")

    assert not result.ok
    assert "JIRA_BASE_URL" in result.error


@respx.mock
async def test_the_api_token_never_appears_in_an_issue_error():
    token = "s3cr3t-jira-token-value"
    config = PluginConfig({
        "JIRA_BASE_URL": BASE, "JIRA_EMAIL": "svc@example.com", "JIRA_API_TOKEN": token})
    respx.get(f"{ISSUE_URL}/ENG-1").mock(side_effect=httpx.HTTPError(f"Basic {token} failed"))

    result = await _plugin(config).fetch_issue("ENG-1")

    assert token not in result.error


# --- Mock mode ---------------------------------------------------------------


async def test_mock_mode_returns_a_fixture_issue():
    plugin = JiraPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JIRA": "1"}))

    result = await plugin.fetch_issue("MOCK-1")

    assert result.ok
    assert result.data["issue"]["key"]
    assert result.data["issue"]["assignee"]
