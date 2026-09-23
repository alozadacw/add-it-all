"""
Jamf hardware detail -- `add-it-all jamf <id> -w`.

**Apple Silicon reports zeros.** On an M-series Mac, Jamf sends
`coreCount: 0`, `processorCount: 0`, `processorSpeedMhz: 0`,
`busSpeedMhz: 0`, `cacheSizeKilobytes: 0` and `openRamSlots: 0` -- not
because the machine has none, but because those Intel-era fields are never
populated. Verified on a live M3 MacBook Air 2026-09-23. Rendering them as
"0 cores" would be confidently wrong, so a zero in those fields is shown as
absent rather than as a number.

Run just this stage:  pytest -m jamf
"""

from __future__ import annotations

import httpx
import pytest
import respx
from jamf_plugin.plugin import JamfPlugin, format_ram, optional_number

from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.jamf

BASE = "https://example.jamfcloud.com"
TOKEN_URL = f"{BASE}/api/oauth/token"
COMPUTERS_URL = f"{BASE}/api/v1/computers-inventory"

CONFIG = PluginConfig({
    "JAMF_BASE_URL": BASE, "JAMF_CLIENT_ID": "id", "JAMF_CLIENT_SECRET": "secret"})


def _apple_silicon_hardware():
    """Exactly what a live M-series Mac returns, zeros included."""
    return {
        "make": "Apple", "model": "MacBook Air (15-inch, M3, 2024)",
        "modelIdentifier": "MacExample15,1", "serialNumber": "EXAMPLE002",
        "processorType": "Apple M3", "processorArchitecture": "arm64",
        "appleSilicon": True, "totalRamMegabytes": 16384,
        "batteryCapacityPercent": 97, "batteryHealth": "NORMAL",
        "macAddress": "00:00:5E:00:53:01", "bootRom": "18000.161.10",
        "coreCount": 0, "processorCount": 0, "processorSpeedMhz": 0,
        "busSpeedMhz": 0, "cacheSizeKilobytes": 0, "openRamSlots": 0,
    }


def _record(hardware=None):
    return {"id": "1",
            "general": {"name": "CW-EXAMPLE001-L", "remoteManagement": {"managed": True}},
            "hardware": hardware if hardware is not None else _apple_silicon_hardware(),
            "operatingSystem": {"name": "macOS", "version": "26.7"}}


def _mock(records=None):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(
        200, json={"access_token": "t", "expires_in": 1200}))
    return respx.get(COMPUTERS_URL).mock(return_value=httpx.Response(
        200, json={"totalCount": 1, "results": records if records is not None else [_record()]}))


# --- Helpers ------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, None, ""])
def test_a_zero_or_missing_number_reads_as_absent(value):
    """Zero here means "Jamf did not populate this", not "none of them"."""
    assert optional_number(value) is None


@pytest.mark.parametrize(("value", "expected"), [(8, 8), (97, 97), (16384, 16384)])
def test_a_real_number_passes_through(value, expected):
    assert optional_number(value) == expected


def test_ram_is_shown_in_gb_not_megabytes():
    """16384 MB is a number nobody thinks in."""
    assert format_ram(16384) == "16 GB"


def test_ram_keeps_a_fractional_size_honest():
    assert format_ram(24576) == "24 GB"


def test_missing_ram_is_none():
    assert format_ram(None) is None
    assert format_ram(0) is None


# --- Fetching -------------------------------------------------------------------


@respx.mock
async def test_hardware_is_returned_normalised():
    _mock()

    result = await JamfPlugin(CONFIG).fetch_hardware("jdoe")

    assert result.ok
    hw = result.data["devices"][0]
    assert hw["model"] == "MacBook Air (15-inch, M3, 2024)"
    assert hw["model_identifier"] == "MacExample15,1"
    assert hw["chip"] == "Apple M3"
    assert hw["architecture"] == "arm64"
    assert hw["apple_silicon"] is True
    assert hw["memory"] == "16 GB"
    assert hw["battery_health"] == "NORMAL"
    assert hw["battery_capacity_percent"] == 97


@respx.mock
async def test_apple_silicon_zeros_are_not_reported_as_counts():
    """The whole reason this module has a helper. `0 cores` would be wrong."""
    _mock()

    hw = (await JamfPlugin(CONFIG).fetch_hardware("jdoe")).data["devices"][0]

    assert hw["core_count"] is None
    assert hw["processor_speed_mhz"] is None
    assert hw["open_ram_slots"] is None


@respx.mock
async def test_a_real_core_count_is_kept():
    """An Intel Mac does populate these, and then they matter."""
    hardware = dict(_apple_silicon_hardware(),
                    appleSilicon=False, processorType="Quad-Core Intel Core i7",
                    coreCount=4, processorSpeedMhz=2600)
    _mock([_record(hardware)])

    hw = (await JamfPlugin(CONFIG).fetch_hardware("jdoe")).data["devices"][0]

    assert hw["core_count"] == 4
    assert hw["processor_speed_mhz"] == 2600


@respx.mock
async def test_the_hardware_section_is_requested():
    route = _mock()

    await JamfPlugin(CONFIG).fetch_hardware("jdoe")

    sections = {v for k, v in route.calls.last.request.url.params.multi_items() if k == "section"}
    assert "HARDWARE" in sections


@respx.mock
async def test_hardware_accepts_the_same_identifier_kinds():
    """A device name or serial must work here too, not just a username."""
    route = _mock()

    await JamfPlugin(CONFIG).fetch_hardware("CW-EXAMPLE001-L")

    f = route.calls.last.request.url.params["filter"]
    assert 'general.name=="CW-EXAMPLE001-L"' in f
    assert 'hardware.serialNumber=="CW-EXAMPLE001-L"' in f


@respx.mock
async def test_a_missing_hardware_section_does_not_crash():
    _mock([{"id": "1", "general": {"name": "Bare"}}])

    result = await JamfPlugin(CONFIG).fetch_hardware("jdoe")

    assert result.ok
    assert result.data["devices"][0]["model"] is None


async def test_mock_mode_populates_hardware():
    plugin = JamfPlugin(PluginConfig({"ADD_IT_ALL_MOCK_JAMF": "1"}))

    hw = (await plugin.fetch_hardware("jdoe")).data["devices"][0]

    assert hw["model"] and hw["chip"] and hw["memory"]
