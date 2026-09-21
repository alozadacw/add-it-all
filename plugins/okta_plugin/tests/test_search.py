"""
Stage 2: `okta --find <name>` -- resolve a partial name to a username.

The problem this solves: someone knows a colleague's first or surname but
not their Okta login, and `okta dennis` is a dead end.

**`--find` is long-only and deliberately not chainable.** Short flags in
this CLI are section selectors (`-s`, `-d`, `-a`, `-u`) and they compose.
Search is not a section -- it answers "who is this person", not "what do
you want to see about this person" -- so it composes with nothing. Dropping
the short form makes every bundled spelling (`-sdf`, `-fd`) a parse error
for free; the `--find -d` case is caught at runtime instead.

**Deactivated users must appear.** Okta's List Users endpoint excludes
`DEPROVISIONED` users by default. For a tool whose main question is "did
this person's access actually get revoked", a search that silently omits
the deactivated Dennis is worse than no search at all -- it looks complete.
The tests here pin that *this client* never filters by status; whether the
API's `search=` parameter inherits the default exclusion is a server-side
behaviour that mocks cannot prove and is flagged for the live smoke test.

**Search results are never cached.** A cached hit could report someone
`ACTIVE` minutes after they were deactivated, which is the wrong answer in
precisely the situation that matters.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import httpx
import pytest
import respx
from okta_plugin.plugin import MAX_SEARCH_RESULTS, OktaPlugin, build_search_expression

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _user(login="dluo", first="Dennis", last="Luo", status="ACTIVE", uid="00u1"):
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


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


# --- The search expression ---------------------------------------------------


def test_expression_covers_the_fields_someone_might_know():
    expr = build_search_expression("dennis")

    for field in ("profile.firstName", "profile.lastName", "profile.login", "profile.email"):
        assert field in expr


def test_expression_uses_startswith_and_ors_the_fields():
    expr = build_search_expression("dennis")

    assert expr.count(' sw "dennis"') == 4
    assert " or " in expr


def test_expression_does_not_constrain_status():
    """Any status clause would risk excluding DEPROVISIONED users, which is
    the population this tool most needs to find."""
    expr = build_search_expression("dennis")

    assert "status" not in expr


def test_expression_escapes_embedded_quotes():
    """A name with a quote would otherwise break out of the filter string."""
    expr = build_search_expression('den"nis')

    assert '\\"' in expr or 'den"nis' not in expr


def test_expression_is_built_from_a_trimmed_query():
    assert build_search_expression("  dennis  ") == build_search_expression("dennis")


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_query_is_rejected(blank):
    with pytest.raises(ValueError):
        build_search_expression(blank)


# --- Multi-token queries ------------------------------------------------------


def test_a_single_token_query_is_a_plain_or_group():
    expr = build_search_expression("dennis")

    assert " and " not in expr
    assert expr.count(" or ") == 3


def test_a_full_name_ands_the_tokens_together():
    """Before this, `--find "dennis luo"` matched nobody: the whole string
    became one startsWith term and no first name begins "dennis luo". The
    most natural way to narrow a search was the one guaranteed to fail."""
    expr = build_search_expression("dennis luo")

    assert " and " in expr
    assert 'sw "dennis"' in expr
    assert 'sw "luo"' in expr
    assert 'sw "dennis luo"' not in expr


def test_each_token_is_ored_across_every_field():
    """So "luo dennis" works as well as "dennis luo" -- nobody should have to
    know which order the directory stores names in."""
    expr = build_search_expression("dennis luo")

    for token in ("dennis", "luo"):
        for field in ("profile.firstName", "profile.lastName"):
            assert f'{field} sw "{token}"' in expr


def test_token_groups_are_parenthesised():
    """Without parentheses, `a or b and c or d` binds wrongly and the AND
    silently stops narrowing anything."""
    expr = build_search_expression("dennis luo")

    assert expr.startswith("(")
    assert ") and (" in expr


def test_repeated_whitespace_between_tokens_is_collapsed():
    assert build_search_expression("dennis   luo") == build_search_expression("dennis luo")


def test_three_tokens_all_narrow():
    expr = build_search_expression("mary jane watson")

    assert expr.count(") and (") == 2


# --- Results -----------------------------------------------------------------


@respx.mock
async def test_matches_are_returned_normalised():
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[_user()]))

    result = await _plugin().fetch_search("dennis")

    assert result.ok
    assert result.data["count"] == 1
    match = result.data["matches"][0]
    assert match["login"] == "dluo"
    assert match["name"] == "Dennis Luo"
    assert match["email"] == "dluo@example.com"
    assert match["status"] == "ACTIVE"


@respx.mock
async def test_deactivated_users_are_included_in_results():
    """The whole point. A search that hides the deprovisioned person looks
    complete and is wrong."""
    respx.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _user("dluo", status="ACTIVE", uid="00u1"),
                _user("dcarter", "Dennis", "Carter", status="DEPROVISIONED", uid="00u2"),
            ],
        )
    )

    result = await _plugin().fetch_search("dennis")

    statuses = {m["status"] for m in result.data["matches"]}
    assert "DEPROVISIONED" in statuses
    assert result.data["count"] == 2


@respx.mock
async def test_a_single_match_is_still_returned_as_a_match():
    """Never auto-resolve. One candidate is not the same claim as the right
    person, and the sections act on whoever you name next."""
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[_user()]))

    result = await _plugin().fetch_search("dennis")

    assert result.data["count"] == 1
    assert "matches" in result.data


@respx.mock
async def test_no_matches_is_a_success():
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = await _plugin().fetch_search("zzzz")

    assert result.ok
    assert result.data["matches"] == []
    assert result.data["count"] == 0


@respx.mock
async def test_matches_are_sorted_by_login():
    """Login is the field you copy into the next command, so it is the one
    worth scanning in order."""
    respx.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _user("zwu", "Zoe", "Wu", uid="00u3"),
                _user("acarter", "Ann", "Carter", uid="00u1"),
                _user("mdennison", "Marta", "Dennison", uid="00u2"),
            ],
        )
    )

    result = await _plugin().fetch_search("d")

    assert [m["login"] for m in result.data["matches"]] == ["acarter", "mdennison", "zwu"]


@respx.mock
async def test_a_missing_name_does_not_produce_a_stray_space():
    respx.get(USERS_URL).mock(
        return_value=httpx.Response(200, json=[_user("dluo", first="Dennis", last=None)])
    )

    result = await _plugin().fetch_search("dennis")

    assert result.data["matches"][0]["name"] == "Dennis"


@respx.mock
async def test_a_user_with_no_name_at_all_is_still_listed():
    respx.get(USERS_URL).mock(
        return_value=httpx.Response(200, json=[{"id": "00u9", "status": "ACTIVE",
                                                "profile": {"login": "ghost"}}])
    )

    result = await _plugin().fetch_search("ghost")

    assert result.data["matches"][0]["login"] == "ghost"
    assert result.data["matches"][0]["name"] is None


# --- Request shape -----------------------------------------------------------


@respx.mock
async def test_the_search_parameter_is_used_not_q():
    """`q` only does startsWith on firstName/lastName/email and is built for
    typeahead; `search` is the expressive one and supports paging."""
    route = respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_search("dennis")

    params = route.calls.last.request.url.params
    assert "search" in params
    assert "q" not in params


@respx.mock
async def test_no_status_filter_is_sent():
    route = respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_search("dennis")

    assert "filter" not in route.calls.last.request.url.params


@respx.mock
async def test_a_result_limit_is_requested():
    """The register is org-sized; an unbounded search on one letter would
    pull thousands of records to display fifteen."""
    route = respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_search("d")

    assert int(route.calls.last.request.url.params["limit"]) == MAX_SEARCH_RESULTS


@respx.mock
async def test_one_request_only_no_pagination_following():
    """Search is interactive, not an audit. Following Link headers to page
    4000 users would burn rate limit to render a 15-row table."""
    route = respx.get(USERS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[_user()],
            headers={"Link": f'<{USERS_URL}?after=next>; rel="next"'},
        )
    )

    await _plugin().fetch_search("dennis")

    assert route.call_count == 1


@respx.mock
async def test_hitting_the_limit_is_reported_as_possibly_truncated():
    """A full page back means the API may have more. Saying nothing would let
    a short list read as 'that is everyone'."""
    respx.get(USERS_URL).mock(
        return_value=httpx.Response(
            200, json=[_user(f"user{i}", uid=f"00u{i}") for i in range(MAX_SEARCH_RESULTS)]
        )
    )

    result = await _plugin().fetch_search("d")

    assert result.data["truncated"] is True


@respx.mock
async def test_a_partial_page_is_not_truncated():
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[_user()]))

    result = await _plugin().fetch_search("dennis")

    assert result.data["truncated"] is False


# --- --all : fetch beyond the first page --------------------------------------


@respx.mock
async def test_all_follows_pagination():
    """Without --all a single page is the answer; with it, the caller has
    explicitly asked to pay for more requests."""
    page_two = f"{USERS_URL}?after=cursor2"
    respx.get(USERS_URL, params={"after": "cursor2"}).mock(
        return_value=httpx.Response(200, json=[_user("zwu", "Zoe", "Wu", uid="00u9")])
    )
    first = respx.get(USERS_URL).mock(
        return_value=httpx.Response(
            200, json=[_user()], headers={"Link": f'<{page_two}>; rel="next"'}
        )
    )

    result = await _plugin().fetch_search("d", fetch_all=True)

    assert first.call_count == 1
    assert {m["login"] for m in result.data["matches"]} == {"dluo", "zwu"}


@respx.mock
async def test_without_all_a_next_link_is_ignored():
    route = respx.get(USERS_URL).mock(
        return_value=httpx.Response(
            200, json=[_user()], headers={"Link": f'<{USERS_URL}?after=x>; rel="next"'}
        )
    )

    await _plugin().fetch_search("d")

    assert route.call_count == 1


@respx.mock
async def test_all_still_reports_truncation_if_it_runs_out_of_pages():
    """A hard page bound still exists so a looping `next` cannot hang the
    CLI. Hitting it is still an incomplete answer and must say so."""
    respx.get(USERS_URL).mock(
        return_value=httpx.Response(
            200, json=[_user()], headers={"Link": f'<{USERS_URL}?after=loop>; rel="next"'}
        )
    )

    result = await _plugin().fetch_search("d", fetch_all=True)

    assert result.data["truncated"] is True


@respx.mock
async def test_all_that_reaches_the_end_is_not_truncated():
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[_user()]))

    result = await _plugin().fetch_search("d", fetch_all=True)

    assert result.data["truncated"] is False


# --- Failure modes -----------------------------------------------------------


@respx.mock
async def test_missing_search_permission_is_an_actionable_error():
    respx.get(USERS_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch_search("dennis")

    assert not result.ok
    assert "search" in result.error.lower() or "read" in result.error.lower()


@respx.mock
async def test_a_rejected_search_expression_is_an_actionable_error():
    """Okta answers 400 for a malformed filter. The raw body is not useful."""
    respx.get(USERS_URL).mock(return_value=httpx.Response(400, json={"errorSummary": "bad filter"}))

    result = await _plugin().fetch_search("dennis")

    assert not result.ok
    assert "search" in result.error.lower()


@respx.mock
async def test_rate_limited_is_an_error_result():
    respx.get(USERS_URL).mock(return_value=httpx.Response(429))

    result = await _plugin().fetch_search("dennis")

    assert not result.ok


@respx.mock
async def test_timeout_is_an_error_result():
    respx.get(USERS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch_search("dennis")

    assert not result.ok


async def test_a_blank_query_is_an_error_result_not_a_crash():
    result = await _plugin().fetch_search("   ")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await OktaPlugin(PluginConfig({})).fetch_search("dennis")

    assert not result.ok
    assert "OKTA_ORG_URL" in result.error


@respx.mock
async def test_the_api_token_never_appears_in_a_search_error():
    token = "s3cr3t-okta-token-value"
    config = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": token})
    respx.get(USERS_URL).mock(side_effect=httpx.HTTPError(f"failed using SSWS {token}"))

    result = await _plugin(config).fetch_search("dennis")

    assert token not in result.error


# --- Mock mode ---------------------------------------------------------------


async def test_mock_mode_returns_fixture_matches_without_network():
    plugin = OktaPlugin(PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"}))

    result = await plugin.fetch_search("dennis")

    assert result.ok
    assert result.data["count"] >= 2, "fixture should exercise the chooser, not a single hit"


async def test_the_mock_fixture_includes_a_deactivated_user():
    """So the mock demo shows the case the feature exists for."""
    plugin = OktaPlugin(PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"}))

    result = await plugin.fetch_search("dennis")

    assert "DEPROVISIONED" in {m["status"] for m in result.data["matches"]}


# --- Search results are not cached -------------------------------------------


@respx.mock
async def test_search_results_are_not_written_to_the_cache():
    """A cached hit could report someone ACTIVE minutes after they were
    deactivated -- wrong in exactly the case that matters. The marker keeps
    Stage 7's cache integration from picking these up later."""
    respx.get(USERS_URL).mock(return_value=httpx.Response(200, json=[_user()]))

    result = await _plugin().fetch_search("dennis")

    assert result.data["cacheable"] is False
