"""
Stage 2: per-device last sign-in, from the Okta System Log.

Why this is a second data source: `/users/{id}/devices` has no last-login
field. Its `lastUpdated` is the obvious-looking trap -- it moves when the
device *record* changes (profile sync, management flip, OS bump), not when
someone signed in. Using it would produce plausible, wrong answers, which
in an offboarding tool is worse than showing nothing. So sign-in times come
from `/api/v1/logs`, correlated to devices on `device.id`.

All HTTP is mocked. Every credential here is obviously fake.

Run just this stage:  pytest -m okta
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
from okta_plugin.plugin import MAX_LOG_WINDOW, OktaPlugin, parse_since

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.okta

ORG_URL = "https://acme.okta.com"
LOGS_URL = f"{ORG_URL}/api/v1/logs"
USER_ID = "00u1abcdefGHIJKLmno7"
MBP = "guoMACBOOK00000000001"
PHONE = "guoIPHONE000000000002"

CONFIG = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": "not-a-real-token"})


def _event(device_id: str | None, published: str, result: str = "SUCCESS") -> dict:
    event = {
        "uuid": f"evt-{published}",
        "published": published,
        "eventType": "user.session.start",
        "actor": {"id": USER_ID, "type": "User", "alternateId": "jdoe@example.com"},
        "outcome": {"result": result},
        "client": {"ipAddress": "203.0.113.10"},
    }
    if device_id is not None:
        event["device"] = {"id": device_id, "name": "a device", "managed": True}
    return event


def _plugin(config: PluginConfig = CONFIG) -> OktaPlugin:
    return OktaPlugin(config)


# --- parse_since ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("90d", timedelta(days=90)), ("30d", timedelta(days=30)), ("1d", timedelta(days=1)),
     ("12h", timedelta(hours=12)), ("  7d  ", timedelta(days=7)), ("7D", timedelta(days=7))],
)
def test_parse_since_accepts_day_and_hour_windows(raw, expected):
    assert parse_since(raw) == expected


@pytest.mark.parametrize("raw", ["", "d", "90", "90w", "-7d", "abc", "0d"])
def test_parse_since_rejects_nonsense(raw):
    with pytest.raises(ValueError):
        parse_since(raw)


def test_parse_since_clamps_beyond_okta_retention():
    """Okta keeps System Log data ~90 days. Asking for 180 cannot return 180,
    and silently accepting it would let the column header claim a window the
    data does not cover."""
    assert parse_since("180d") == MAX_LOG_WINDOW


# --- Correlating events to devices --------------------------------------------------


@respx.mock
async def test_returns_the_most_recent_signin_per_device():
    respx.get(LOGS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[  # DESCENDING: newest first
                _event(MBP, "2026-08-28T14:31:00.000Z"),
                _event(PHONE, "2026-06-14T09:00:00.000Z"),
                _event(MBP, "2026-08-01T08:00:00.000Z"),  # older, must not win
            ],
        )
    )

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert result.ok
    assert result.data["signins"][MBP] == "2026-08-28T14:31:00.000Z"
    assert result.data["signins"][PHONE] == "2026-06-14T09:00:00.000Z"


@respx.mock
async def test_devices_with_no_events_are_simply_absent():
    respx.get(LOGS_URL).mock(return_value=httpx.Response(200, json=[_event(MBP, "2026-08-28T14:31:00.000Z")]))

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert PHONE not in result.data["signins"]


@respx.mock
async def test_events_without_device_identity_are_skipped_not_guessed():
    """Not every auth event stamps a device. Attributing one to a device by
    user agent could not tell two MacBooks apart, so we drop it instead."""
    respx.get(LOGS_URL).mock(
        return_value=httpx.Response(200, json=[_event(None, "2026-08-29T10:00:00.000Z")])
    )

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert result.ok
    assert result.data["signins"] == {}


@respx.mock
async def test_failed_signins_do_not_count_as_a_last_signin():
    respx.get(LOGS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _event(MBP, "2026-08-30T10:00:00.000Z", result="FAILURE"),
                _event(MBP, "2026-08-28T14:31:00.000Z", result="SUCCESS"),
            ],
        )
    )

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert result.data["signins"][MBP] == "2026-08-28T14:31:00.000Z"


@respx.mock
async def test_empty_log_is_a_success_not_an_error():
    respx.get(LOGS_URL).mock(return_value=httpx.Response(200, json=[]))

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert result.ok
    assert result.data["signins"] == {}


# --- Request shape -------------------------------------------------------------------


@respx.mock
async def test_scopes_the_query_to_this_user_and_sorts_newest_first():
    route = respx.get(LOGS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    request = route.calls.last.request
    assert USER_ID in request.url.params["filter"]
    assert request.url.params["sortOrder"] == "DESCENDING"
    assert "since" in request.url.params


@respx.mock
async def test_one_request_covers_every_device_not_one_per_device():
    """/api/v1/logs is Okta's most rate-limited endpoint. N+1 querying would
    fall over on anyone with a handful of devices."""
    route = respx.get(LOGS_URL).mock(
        return_value=httpx.Response(
            200, json=[_event(MBP, "2026-08-28T00:00:00.000Z"), _event(PHONE, "2026-08-27T00:00:00.000Z")]
        )
    )

    await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90), device_ids={MBP, PHONE})

    assert route.call_count == 1


@respx.mock
async def test_stops_paging_once_every_known_device_has_been_seen():
    """Descending order means the first hit for a device is its latest, so
    once all devices are accounted for the remaining pages cannot change the
    answer -- and each extra page costs rate-limit budget."""
    page_two = f"{LOGS_URL}?after=cursor2"
    respx.get(LOGS_URL, params={"after": "cursor2"}).mock(
        return_value=httpx.Response(200, json=[_event(MBP, "2020-01-01T00:00:00.000Z")])
    )
    first = respx.get(LOGS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[_event(MBP, "2026-08-28T00:00:00.000Z"), _event(PHONE, "2026-08-27T00:00:00.000Z")],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )
    )

    result = await _plugin().fetch_device_signins(
        USER_ID, since=timedelta(days=90), device_ids={MBP, PHONE}
    )

    assert first.call_count == 1
    assert result.data["signins"][MBP] == "2026-08-28T00:00:00.000Z"


@respx.mock
async def test_follows_pagination_when_devices_are_still_unaccounted_for():
    page_two = f"{LOGS_URL}?after=cursor2"
    respx.get(LOGS_URL, params={"after": "cursor2"}).mock(
        return_value=httpx.Response(200, json=[_event(PHONE, "2026-06-14T09:00:00.000Z")])
    )
    respx.get(LOGS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[_event(MBP, "2026-08-28T00:00:00.000Z")],
            headers={"Link": f'<{page_two}>; rel="next"'},
        )
    )

    result = await _plugin().fetch_device_signins(
        USER_ID, since=timedelta(days=90), device_ids={MBP, PHONE}
    )

    assert result.data["signins"][PHONE] == "2026-06-14T09:00:00.000Z"


# --- Failure modes ---------------------------------------------------------------------


@respx.mock
async def test_missing_log_scope_is_an_actionable_error_not_a_crash():
    """A read-only admin token can lack System Log access; say so."""
    respx.get(LOGS_URL).mock(return_value=httpx.Response(403))

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert not result.ok
    assert "log" in result.error.lower()


@respx.mock
async def test_rate_limited_is_an_error_result():
    respx.get(LOGS_URL).mock(return_value=httpx.Response(429))

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert not result.ok


@respx.mock
async def test_timeout_is_an_error_result():
    respx.get(LOGS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert not result.ok


@respx.mock
async def test_the_api_token_never_appears_in_a_log_error():
    token = "s3cr3t-okta-token-value"
    config = PluginConfig({"OKTA_ORG_URL": ORG_URL, "OKTA_API_TOKEN": token})
    respx.get(LOGS_URL).mock(side_effect=httpx.HTTPError(f"failed using SSWS {token}"))

    result = await _plugin(config).fetch_device_signins(USER_ID, since=timedelta(days=90))

    assert token not in result.error


async def test_mock_mode_returns_signins_without_network():
    plugin = OktaPlugin(PluginConfig({"ADD_IT_ALL_MOCK_OKTA": "1"}))

    result = await plugin.fetch_device_signins("00uMOCK", since=timedelta(days=90))

    assert result.ok
    assert result.data["signins"]
