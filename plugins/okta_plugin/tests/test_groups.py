"""
Stage 2: groups a user belongs to (`fetch_groups`).

Source: `GET /api/v1/users/{userId}/groups` -- the group memberships Okta
holds for one person. This is the building block the team comparison
(`--team`) fans out over: one groups call per team member.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import httpx
import pytest
import respx
from okta_plugin.plugin import OktaPlugin

from lookup_cli.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"
USER_ID = "00u1abcdefGHIJKLmno7"
GROUPS_URL = f"{USERS_URL}/{USER_ID}/groups"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _user_payload() -> dict:
    return {
        "id": USER_ID,
        "status": "ACTIVE",
        "profile": {"login": "jdoe", "email": "jdoe@example.com"},
    }


def _group(
    name: str = "Everyone",
    group_id: str = "00g1everyone000000000",
    group_type: str = "BUILT_IN",
) -> dict:
    return {
        "id": group_id,
        "type": group_type,
        "profile": {"name": name, "description": f"{name} group"},
    }


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


def _mock_user():
    return respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_payload())
    )


# --- Happy path ---------------------------------------------------------------


@respx.mock
async def test_groups_are_returned_and_normalised():
    _mock_user()
    respx.get(GROUPS_URL).mock(
        return_value=httpx.Response(200, json=[_group("Eng-GitHub", "00g2", "OKTA_GROUP")])
    )

    result = await _plugin().fetch_groups("jdoe")

    assert result.ok
    assert result.data["count"] == 1
    group = result.data["groups"][0]
    assert group["name"] == "Eng-GitHub"
    assert group["group_id"] == "00g2"
    assert group["type"] == "OKTA_GROUP"


@respx.mock
async def test_groups_are_sorted_by_name_case_insensitively():
    """Okta returns groups in an arbitrary order; alphabetical means two
    people's memberships can actually be lined up and compared."""
    _mock_user()
    respx.get(GROUPS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _group("VPN-Users", "00g3"),
                _group("aws-prod", "00g4"),
                _group("Everyone", "00g1"),
            ],
        )
    )

    result = await _plugin().fetch_groups("jdoe")

    assert [g["name"] for g in result.data["groups"]] == ["aws-prod", "Everyone", "VPN-Users"]


@respx.mock
async def test_user_with_no_groups_is_a_success_with_an_empty_list():
    _mock_user()
    respx.get(GROUPS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = await _plugin().fetch_groups("jdoe")

    assert result.ok
    assert result.data["count"] == 0
    assert result.data["groups"] == []
    assert "no-groups" in result.tags


@respx.mock
async def test_missing_group_fields_do_not_crash():
    _mock_user()
    respx.get(GROUPS_URL).mock(return_value=httpx.Response(200, json=[{"id": "00g9"}]))

    result = await _plugin().fetch_groups("jdoe")

    assert result.ok
    assert result.data["groups"][0]["name"] is None
    assert result.data["groups"][0]["group_id"] == "00g9"


# --- Pagination ---------------------------------------------------------------


@respx.mock
async def test_paginated_group_lists_are_fully_assembled():
    """Under-reporting a member's groups would make the comparison lie about
    who has access to what, so page one is not the answer."""
    page_two = f"{GROUPS_URL}?after=abc"
    _mock_user()
    respx.get(GROUPS_URL, params={"after": "abc"}).mock(
        return_value=httpx.Response(200, json=[_group("VPN-Users", "00g3")])
    )
    respx.get(GROUPS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[_group()],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )
    )

    result = await _plugin().fetch_groups("jdoe")

    assert result.data["count"] == 2


# --- Avoiding a redundant lookup ------------------------------------------------


@respx.mock
async def test_supplying_a_known_okta_id_skips_the_user_lookup():
    user_route = _mock_user()
    respx.get(GROUPS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_groups("jdoe", okta_id=USER_ID)

    assert not user_route.called


# --- Failure modes ---------------------------------------------------------------


@respx.mock
async def test_unknown_user_reports_not_found_rather_than_an_error():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_groups("ghost")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["groups"] == []
    assert "not-found" in result.tags


@respx.mock
async def test_forbidden_is_an_actionable_error_naming_the_scope():
    _mock_user()
    respx.get(GROUPS_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch_groups("jdoe")

    assert not result.ok
    assert "group" in result.error.lower()


@respx.mock
async def test_timeout_is_an_error_result():
    _mock_user()
    respx.get(GROUPS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch_groups("jdoe")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await OktaPlugin(PluginConfig({})).fetch_groups("jdoe")

    assert not result.ok
    assert "OKTA_ORG_URL" in result.error


@respx.mock
async def test_the_api_token_never_appears_in_a_groups_error():
    token = "s3cr3t-okta-token-value"
    config = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": token})
    _mock_user()
    respx.get(GROUPS_URL).mock(side_effect=httpx.HTTPError(f"failed using SSWS {token}"))

    result = await _plugin(config).fetch_groups("jdoe")

    assert token not in result.error


# --- Mock mode -------------------------------------------------------------------


async def test_mock_mode_returns_fixture_groups_without_network():
    plugin = OktaPlugin(PluginConfig({"LOOKUP_CLI_MOCK_OKTA": "1"}))

    result = await plugin.fetch_groups("jdoe")

    assert result.ok
    assert result.data["count"] >= 1
    assert result.data["groups"][0]["name"]


# --- The plain lookup stays cheap --------------------------------------------------


@respx.mock
async def test_plain_fetch_does_not_call_the_groups_endpoint():
    """Stage 7 runs fetch() for every plugin on every lookup; it must not pay
    for a round trip nobody asked for."""
    _mock_user()
    groups_route = respx.get(GROUPS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch("jdoe")

    assert not groups_route.called
