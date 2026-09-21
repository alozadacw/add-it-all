"""
Stage 2: applications assigned to a user (`add-it-all okta <user> -a`).

Source: `GET /api/v1/users/{userId}/appLinks` -- the same list that builds
the user's Okta dashboard. Scope caveat worth knowing: appLinks says *what*
a user can open, not *how* they got it. Direct-vs-group assignment lives on
`/apps/{appId}/users/{userId}` and costs one call per app, so it is
deliberately not fetched here.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import httpx
import pytest
import respx
from okta_plugin.plugin import OktaPlugin

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"
USER_ID = "00u1abcdefGHIJKLmno7"
APPS_URL = f"{USERS_URL}/{USER_ID}/appLinks"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _user_payload() -> dict:
    return {
        "id": USER_ID,
        "status": "ACTIVE",
        "profile": {"login": "jdoe", "email": "jdoe@example.com"},
    }


def _app_link(
    label: str = "Google Workspace",
    app_name: str = "google",
    app_id: str = "0oa1gjh63g214q0Hq0g4",
    hidden: bool = False,
) -> dict:
    return {
        "id": app_id,
        "label": label,
        "linkUrl": f"{ORG_URL}/home/{app_name}/{app_id}/1234",
        "logoUrl": f"{ORG_URL}/img/logos/{app_name}.png",
        "appName": app_name,
        "appInstanceId": app_id,
        "appAssignmentId": "0ua1i0dabcAeAbC0Hq0g4",
        "credentialsSetup": False,
        "hidden": hidden,
        "sortOrder": 0,
    }


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


def _mock_user():
    return respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_payload())
    )


# --- Happy path ---------------------------------------------------------------


@respx.mock
async def test_apps_are_returned_and_normalised():
    _mock_user()
    respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[_app_link()]))

    result = await _plugin().fetch_apps("jdoe")

    assert result.ok
    assert result.data["count"] == 1
    app = result.data["apps"][0]
    assert app["label"] == "Google Workspace"
    assert app["app_name"] == "google"
    assert app["app_id"] == "0oa1gjh63g214q0Hq0g4"
    assert app["hidden"] is False


@respx.mock
async def test_multiple_apps_are_all_returned():
    _mock_user()
    respx.get(APPS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _app_link("Slack", "slack", "0oa2"),
                _app_link("AWS Production", "amazon_aws", "0oa3"),
            ],
        )
    )

    result = await _plugin().fetch_apps("jdoe")

    assert result.data["count"] == 2
    assert {a["label"] for a in result.data["apps"]} == {"Slack", "AWS Production"}


@respx.mock
async def test_apps_are_sorted_by_label_case_insensitively():
    """Okta returns dashboard sort order, which is per-user and arbitrary.
    Alphabetical means two people's app lists can actually be compared."""
    _mock_user()
    respx.get(APPS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _app_link("zoom", "zoom", "0oa1"),
                _app_link("AWS Production", "amazon_aws", "0oa2"),
                _app_link("Slack", "slack", "0oa3"),
            ],
        )
    )

    result = await _plugin().fetch_apps("jdoe")

    assert [a["label"] for a in result.data["apps"]] == ["AWS Production", "Slack", "zoom"]


@respx.mock
async def test_hidden_apps_are_still_listed():
    """A hidden tile is still an active assignment -- exactly the kind of
    access an offboarding check must not miss just because it isn't on the
    user's dashboard."""
    _mock_user()
    respx.get(APPS_URL).mock(
        return_value=httpx.Response(200, json=[_app_link("GitHub", "github", "0oa5", hidden=True)])
    )

    result = await _plugin().fetch_apps("jdoe")

    assert result.data["count"] == 1
    assert result.data["apps"][0]["hidden"] is True


@respx.mock
async def test_user_with_no_apps_is_a_success_with_an_empty_list():
    _mock_user()
    respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = await _plugin().fetch_apps("jdoe")

    assert result.ok
    assert result.data["count"] == 0
    assert result.data["apps"] == []
    assert "no-apps" in result.tags


@respx.mock
async def test_missing_app_fields_do_not_crash():
    _mock_user()
    respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[{"id": "0oa1"}]))

    result = await _plugin().fetch_apps("jdoe")

    assert result.ok
    assert result.data["apps"][0]["label"] is None
    assert result.data["apps"][0]["app_id"] == "0oa1"


# --- Pagination ---------------------------------------------------------------


@respx.mock
async def test_paginated_app_lists_are_fully_assembled():
    """Under-reporting someone's app access is the worst possible failure for
    an offboarding tool, so page one is not the answer."""
    page_two = f"{APPS_URL}?after=abc"
    _mock_user()
    # Register the more specific route first: respx ignores the query string
    # when a pattern has none, so the bare route would otherwise swallow both
    # requests and this would pass while paging silently did nothing.
    respx.get(APPS_URL, params={"after": "abc"}).mock(
        return_value=httpx.Response(200, json=[_app_link("Slack", "slack", "0oa2")])
    )
    respx.get(APPS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[_app_link()],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )
    )

    result = await _plugin().fetch_apps("jdoe")

    assert result.data["count"] == 2


@respx.mock
async def test_self_referential_next_link_cannot_loop_forever():
    _mock_user()
    respx.get(APPS_URL).mock(
        return_value=httpx.Response(
            200, json=[_app_link()], headers={"Link": f'<{APPS_URL}>; rel="next"'}
        )
    )

    result = await _plugin().fetch_apps("jdoe")

    assert result.ok  # bounded, not hung


# --- Avoiding a redundant lookup ------------------------------------------------


@respx.mock
async def test_supplying_a_known_okta_id_skips_the_user_lookup():
    user_route = _mock_user()
    respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_apps("jdoe", okta_id=USER_ID)

    assert not user_route.called


# --- Failure modes ---------------------------------------------------------------


@respx.mock
async def test_unknown_user_reports_not_found_rather_than_an_error():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_apps("ghost")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["apps"] == []
    assert "not-found" in result.tags


@respx.mock
async def test_forbidden_is_an_actionable_error_naming_the_scope():
    """A read-only token can lack app-read access; say which permission."""
    _mock_user()
    respx.get(APPS_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch_apps("jdoe")

    assert not result.ok
    assert "app" in result.error.lower()


@respx.mock
async def test_rate_limited_is_an_error_result():
    _mock_user()
    respx.get(APPS_URL).mock(return_value=httpx.Response(429))

    result = await _plugin().fetch_apps("jdoe")

    assert not result.ok


@respx.mock
async def test_timeout_is_an_error_result():
    _mock_user()
    respx.get(APPS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch_apps("jdoe")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await OktaPlugin(PluginConfig({})).fetch_apps("jdoe")

    assert not result.ok
    assert "OKTA_ORG_URL" in result.error


@respx.mock
async def test_the_api_token_never_appears_in_an_apps_error():
    token = "s3cr3t-okta-token-value"
    config = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": token})
    _mock_user()
    respx.get(APPS_URL).mock(side_effect=httpx.HTTPError(f"failed using SSWS {token}"))

    result = await _plugin(config).fetch_apps("jdoe")

    assert token not in result.error


# --- Mock mode -------------------------------------------------------------------


async def test_mock_mode_returns_fixture_apps_without_network():
    plugin = OktaPlugin(PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"}))

    result = await plugin.fetch_apps("jdoe")

    assert result.ok
    assert result.data["count"] >= 1
    assert result.data["apps"][0]["label"]


# --- The plain lookup stays cheap --------------------------------------------------


@respx.mock
async def test_plain_fetch_does_not_call_the_apps_endpoint():
    """Stage 7 runs fetch() for every plugin on every lookup; it must not pay
    for a round trip nobody asked for."""
    _mock_user()
    apps_route = respx.get(APPS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch("jdoe")

    assert not apps_route.called
