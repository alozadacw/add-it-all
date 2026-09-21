"""
Stage 2: authenticators enrolled by a user (`add-it-all okta <user> -u`).

Source: `GET /api/v1/users/{userId}/factors`. "Factor" is the API's word;
"authenticator" is what the Okta admin console calls the same thing, so the
CLI uses the console's word and this module keeps the API's word for
anything touching the wire.

Deliberately NOT shown: `profile.questionText`. Knowing a security question
is enrolled is the useful fact; printing the question itself hands over a
recovery-credential hint for no operational gain.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

import httpx
import pytest
import respx
from okta_plugin.plugin import OktaPlugin, factor_detail, factor_label

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
USERS_URL = f"{ORG_URL}/api/v1/users"
USER_ID = "00u1abcdefGHIJKLmno7"
FACTORS_URL = f"{USERS_URL}/{USER_ID}/factors"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _user_payload() -> dict:
    return {
        "id": USER_ID,
        "status": "ACTIVE",
        "profile": {"login": "jdoe", "email": "jdoe@example.com"},
    }


def _factor(
    factor_type: str = "push",
    provider: str = "OKTA",
    status: str = "ACTIVE",
    profile: dict | None = None,
    created: str = "2025-06-11T08:12:00.000Z",
) -> dict:
    return {
        "id": f"opf-{factor_type}",
        "factorType": factor_type,
        "provider": provider,
        "vendorName": provider,
        "status": status,
        "created": created,
        "lastUpdated": "2026-08-20T14:31:00.000Z",
        "profile": profile if profile is not None else {"name": "Jane's iPhone"},
    }


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


def _mock_user():
    return respx.get(f"{USERS_URL}/jdoe").mock(
        return_value=httpx.Response(200, json=_user_payload())
    )


# --- Labelling ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("factor_type", "expected"),
    [
        ("push", "Okta Verify push"),
        ("signed_nonce", "Okta FastPass"),
        ("sms", "SMS"),
        ("call", "Voice call"),
        ("email", "Email"),
        ("question", "Security question"),
        ("token:software:totp", "TOTP app"),
        ("token:hardware", "Hardware token"),
        ("password", "Password"),
    ],
)
def test_okta_factor_types_get_readable_labels(factor_type, expected):
    """`token:software:totp` is not a thing an operator should have to decode."""
    assert factor_label(factor_type, "OKTA") == expected


def test_an_unknown_factor_type_falls_back_to_the_raw_value():
    """Okta adds authenticator types; inventing a label for one we don't know
    would be worse than showing what the API said."""
    assert factor_label("some_future_factor", "OKTA") == "some_future_factor"


def test_a_non_okta_provider_is_named_in_the_label():
    """A Duo push and an Okta Verify push are different systems to go and
    revoke, so the label must not flatten them together."""
    assert factor_label("push", "DUO") == "Okta Verify push (DUO)"


def test_the_okta_provider_is_not_repeated_in_the_label():
    assert factor_label("push", "OKTA") == "Okta Verify push"


def test_a_missing_provider_does_not_produce_a_dangling_suffix():
    assert factor_label("sms", None) == "SMS"


# --- Detail extraction -------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"name": "Jane's iPhone"}, "Jane's iPhone"),
        ({"authenticatorName": "YubiKey 5C"}, "YubiKey 5C"),
        ({"phoneNumber": "+1 XXX-XXX-1234"}, "+1 XXX-XXX-1234"),
        ({"email": "j***e@example.com"}, "j***e@example.com"),
        ({"credentialId": "jdoe@example.com"}, "jdoe@example.com"),
        ({}, None),
        (None, None),
    ],
)
def test_detail_picks_the_most_identifying_field_available(profile, expected):
    assert factor_detail(profile) == expected


def test_a_named_device_wins_over_its_credential_id():
    """Both are present on a push factor; the device name is what an operator
    recognises."""
    assert factor_detail({"name": "Jane's iPhone", "credentialId": "jdoe@example.com"}) == (
        "Jane's iPhone"
    )


def test_the_security_question_text_is_never_surfaced():
    """That the question exists is the useful fact. The question itself is a
    recovery-credential hint with no operational value here."""
    detail = factor_detail({"question": "favorite_art_piece", "questionText": "Favourite art?"})

    assert detail is None
    assert "Favourite art?" != detail


# --- Happy path ---------------------------------------------------------------


@respx.mock
async def test_authenticators_are_returned_and_normalised():
    _mock_user()
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[_factor()]))

    result = await _plugin().fetch_authenticators("jdoe")

    assert result.ok
    assert result.data["count"] == 1
    factor = result.data["authenticators"][0]
    assert factor["label"] == "Okta Verify push"
    assert factor["factor_type"] == "push"
    assert factor["provider"] == "OKTA"
    assert factor["status"] == "ACTIVE"
    assert factor["detail"] == "Jane's iPhone"
    assert factor["created"] == "2025-06-11T08:12:00.000Z"


@respx.mock
async def test_every_enrolled_factor_is_returned():
    _mock_user()
    respx.get(FACTORS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _factor("push"),
                _factor("sms", profile={"phoneNumber": "+1 XXX-XXX-1234"}),
                _factor("webauthn", provider="FIDO", profile={"authenticatorName": "YubiKey 5C"}),
            ],
        )
    )

    result = await _plugin().fetch_authenticators("jdoe")

    assert result.data["count"] == 3


@respx.mock
async def test_status_is_reported_verbatim():
    """PENDING_ACTIVATION is a real, different state from ACTIVE -- an
    enrolment someone started and never finished."""
    _mock_user()
    respx.get(FACTORS_URL).mock(
        return_value=httpx.Response(
            200, json=[_factor("email", status="PENDING_ACTIVATION", profile={"email": "j***e@x.com"})]
        )
    )

    result = await _plugin().fetch_authenticators("jdoe")

    assert result.data["authenticators"][0]["status"] == "PENDING_ACTIVATION"


@respx.mock
async def test_inactive_factors_are_still_listed():
    """A disabled authenticator is still enrolled, and an offboarding check
    wants the full picture rather than only what currently works."""
    _mock_user()
    respx.get(FACTORS_URL).mock(
        return_value=httpx.Response(200, json=[_factor("sms", status="INACTIVE")])
    )

    result = await _plugin().fetch_authenticators("jdoe")

    assert result.data["count"] == 1
    assert result.data["authenticators"][0]["status"] == "INACTIVE"


@respx.mock
async def test_user_with_no_authenticators_is_a_success_with_an_empty_list():
    _mock_user()
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = await _plugin().fetch_authenticators("jdoe")

    assert result.ok
    assert result.data["count"] == 0
    assert "no-authenticators" in result.tags


@respx.mock
async def test_missing_factor_fields_do_not_crash():
    _mock_user()
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[{"id": "opf1"}]))

    result = await _plugin().fetch_authenticators("jdoe")

    assert result.ok
    assert result.data["authenticators"][0]["detail"] is None


# --- Avoiding a redundant lookup ------------------------------------------------


@respx.mock
async def test_supplying_a_known_okta_id_skips_the_user_lookup():
    user_route = _mock_user()
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_authenticators("jdoe", okta_id=USER_ID)

    assert not user_route.called


# --- Failure modes ---------------------------------------------------------------


@respx.mock
async def test_unknown_user_reports_not_found_rather_than_an_error():
    respx.get(f"{USERS_URL}/ghost").mock(return_value=httpx.Response(404))

    result = await _plugin().fetch_authenticators("ghost")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["authenticators"] == []
    assert "not-found" in result.tags


@respx.mock
async def test_forbidden_is_an_actionable_error():
    _mock_user()
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch_authenticators("jdoe")

    assert not result.ok
    assert "authenticator" in result.error.lower()


@respx.mock
async def test_rate_limited_is_an_error_result():
    _mock_user()
    respx.get(FACTORS_URL).mock(return_value=httpx.Response(429))

    result = await _plugin().fetch_authenticators("jdoe")

    assert not result.ok


@respx.mock
async def test_timeout_is_an_error_result():
    _mock_user()
    respx.get(FACTORS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch_authenticators("jdoe")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await OktaPlugin(PluginConfig({})).fetch_authenticators("jdoe")

    assert not result.ok
    assert "OKTA_ORG_URL" in result.error


@respx.mock
async def test_the_api_token_never_appears_in_an_authenticators_error():
    token = "s3cr3t-okta-token-value"
    config = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": token})
    _mock_user()
    respx.get(FACTORS_URL).mock(side_effect=httpx.HTTPError(f"failed using SSWS {token}"))

    result = await _plugin(config).fetch_authenticators("jdoe")

    assert token not in result.error


# --- Mock mode -------------------------------------------------------------------


async def test_mock_mode_returns_fixture_authenticators_without_network():
    plugin = OktaPlugin(PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"}))

    result = await plugin.fetch_authenticators("jdoe")

    assert result.ok
    assert result.data["count"] >= 1
    assert result.data["authenticators"][0]["label"]


# --- The plain lookup stays cheap --------------------------------------------------


@respx.mock
async def test_plain_fetch_does_not_call_the_factors_endpoint():
    _mock_user()
    factors_route = respx.get(FACTORS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch("jdoe")

    assert not factors_route.called
