"""
`add-it-all cairo <name>` -- the CAIRO CLI surface.

Follows the shape every connector here uses: `<service> <identifier>
[flags]`, no noun subcommands. The identifier is a vendor or application
name rather than a person, which is the only thing that differs.

Driven through the real core app via `build_app`, so these also prove the
plugin's sub-app is mounted the way a user would actually reach it.

Run just this connector:  pytest -m cairo
"""

from __future__ import annotations

import re

import httpx
import pytest
import respx
from cairo_plugin.plugin import CairoPlugin
from typer.testing import CliRunner

from add_it_all.cli import build_app
from add_it_all.plugins.config import PluginConfig

pytestmark = pytest.mark.cairo

BASE = "https://tprm.example.internal"
VENDORS_URL = f"{BASE}/api/vendors"
VENDOR_ID = "e4df561c-0000-0000-0000-b792214ea7b2"

CONFIG = PluginConfig({"CAIRO_BASE_URL": BASE, "CAIRO_API_KEY": "not-a-real-key"})
MOCK_CONFIG = PluginConfig({"ADD_IT_ALL_MOCK_CAIRO": "1"})

runner = CliRunner(env={"COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb"})
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    return _ANSI.sub("", result.stdout)


def _app(config: PluginConfig = CONFIG):
    return build_app({"cairo": CairoPlugin(config)})


def _vendor(name="AcmeSec Corporation", status="approved", vendor_id=VENDOR_ID, risk="low"):
    return {
        "id": vendor_id, "name": name, "domain": "acmesec.example", "status": status,
        "risk_level": risk, "category": "Cloud SaaS", "data_classification": "Confidential",
        "last_approved_at": "2025-11-02 12:00:00+00", "next_review_at": "2026-11-02 12:00:00+00",
        "primary_contact": "support@acmesec.example",
        "owner": "Morgan Vendorowner",
        "assigned_to": "reviewer@corp.example", "assigned_to_name": "Alex Reviewer",
    }


def _detail(vendor=None, assessments=None):
    payload = dict(vendor or _vendor())
    payload["assessments"] = assessments if assessments is not None else [{
        "id": "a1", "status": "post_complete", "workflow_status": "review_complete",
        "engagement_context": {
            "description": "Threat-intel IP enrichment for the security team.",
            "engagement_type": "Cloud SaaS", "data_classification": "Confidential",
            "production_access": "yes", "inherent_risk_level": "medium",
            "business_owner": "Dana Requester",
        },
    }]
    return payload


def _mock_ok(vendors=None, detail=None):
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=vendors or [_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(
        return_value=httpx.Response(200, json=detail or _detail())
    )


# --- Happy path -------------------------------------------------------------


@respx.mock
def test_name_alone_shows_the_vendor_and_its_status():
    _mock_ok()

    result = runner.invoke(_app(), ["cairo", "acmesec"])

    assert result.exit_code == 0
    out = _out(result)
    assert "AcmeSec Corporation" in out
    assert "approved" in out.lower()


@respx.mock
def test_the_applications_table_is_shown():
    _mock_ok()

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "Threat-intel IP enrichment" in out
    assert "Cloud SaaS" in out


@respx.mock
def test_a_denied_vendor_is_called_out_clearly():
    denied = _vendor(status="denied", risk="critical")
    _mock_ok(vendors=[denied], detail=_detail(denied))

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "denied" in out.lower()


@respx.mock
def test_no_identifier_is_a_usage_error():
    assert runner.invoke(_app(), ["cairo"]).exit_code == 2


# --- Ambiguity --------------------------------------------------------------


@respx.mock
def test_multiple_matches_print_a_chooser_not_a_guess():
    respx.get(VENDORS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _vendor("Databright Inc", vendor_id="id-1"),
                _vendor("Databright Plugin", status="denied", vendor_id="id-2"),
            ],
        )
    )

    result = runner.invoke(_app(), ["cairo", "databright"])

    assert result.exit_code == 0
    out = _out(result)
    assert "Databright Inc" in out
    assert "Databright Plugin" in out
    assert "match" in out.lower()


@respx.mock
def test_the_chooser_shows_each_candidates_status():
    """Often enough to answer the question without a second command."""
    respx.get(VENDORS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _vendor("Databright Inc", vendor_id="id-1"),
                _vendor("Databright Plugin", status="denied", vendor_id="id-2"),
            ],
        )
    )

    out = _out(runner.invoke(_app(), ["cairo", "databright"]))

    assert "approved" in out.lower()
    assert "denied" in out.lower()


@respx.mock
def test_no_match_says_so_and_exits_zero():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))

    result = runner.invoke(_app(), ["cairo", "nothing-like-this"])

    assert result.exit_code == 0
    assert "no vendor" in _out(result).lower()


# --- The unassessed gap -------------------------------------------------------


@respx.mock
def test_a_vendor_with_no_assessment_says_so_explicitly():
    _mock_ok(detail=_detail(assessments=[]))

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "no assessment" in out.lower()
    # Must not read as "this vendor has no applications".
    assert "no applications" not in out.lower()


@respx.mock
def test_a_paragraph_description_is_summarised_not_dumped_into_the_cell():
    """Live descriptions run to several hundred characters. Rendering one
    raw turns a one-row table into a fifteen-line block and pushes every
    other column off the screen."""
    long_desc = (
        "AcmeSec is a SaaS threat-intelligence data vendor. Their product "
        "identifies anonymization infrastructure behind IP addresses, including "
        "VPN services, residential proxies, and the operators running them, and "
        "this request covers a substantial upgrade to the existing tier."
    )
    detail = _detail()
    detail["assessments"][0]["engagement_context"]["description"] = long_desc
    _mock_ok(detail=detail)

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "AcmeSec is a SaaS threat-intelligence data vendor." in out
    assert "residential proxies" not in out, "full paragraph must not be rendered"


# --- Failure modes -------------------------------------------------------------


@respx.mock
def test_an_api_failure_exits_non_zero():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(503))

    assert runner.invoke(_app(), ["cairo", "acmesec"]).exit_code == 1


@respx.mock
def test_a_detail_failure_still_prints_the_vendor_status():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(503))

    result = runner.invoke(_app(), ["cairo", "acmesec"])

    assert result.exit_code == 0
    out = _out(result)
    assert "approved" in out.lower()
    assert "unavailable" in out.lower()


def test_missing_credentials_exit_non_zero_with_an_actionable_message():
    result = runner.invoke(build_app({"cairo": CairoPlugin(PluginConfig({}))}), ["cairo", "x"])

    assert result.exit_code == 1
    assert "CAIRO_BASE_URL" in _out(result)


# --- Help and mock mode ----------------------------------------------------------


def test_help_describes_the_identifier_as_a_vendor_or_application():
    out = _out(runner.invoke(_app(), ["cairo", "--help"]))

    assert "vendor" in out.lower() or "application" in out.lower()


def test_mock_mode_end_to_end():
    result = runner.invoke(_app(MOCK_CONFIG), ["cairo", "acmesec"])

    assert result.exit_code == 0


@respx.mock
def test_output_stays_readable_at_80_columns():
    narrow = CliRunner(env={"COLUMNS": "80", "NO_COLOR": "1", "TERM": "dumb"})
    _mock_ok()

    out = _ANSI.sub("", narrow.invoke(_app(), ["cairo", "acmesec"]).stdout)

    assert "approved" in out.lower()
    assert "Cloud SaaS" in out


@respx.mock
def test_the_application_owner_is_shown():
    _mock_ok()

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "Dana Requester" in out


@respx.mock
def test_the_vendor_owner_is_shown_under_its_own_label():
    """Labelled 'vendor owner' rather than 'owner', because the applications
    table has its own owner column meaning a different person."""
    _mock_ok()

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "vendor owner" in out
    assert "Morgan Vendorowner" in out


@respx.mock
def test_the_reviewer_is_never_shown():
    """Asked for explicitly 2026-09-04: the person who ran the TPRM review is
    not the owner, and showing them where an owner is expected points at the
    wrong person."""
    _mock_ok()

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "Alex Reviewer" not in out
    assert "reviewer@corp.example" not in out


@respx.mock
def test_vendor_side_contact_details_are_not_printed():
    """Not an owner, and not needed to answer 'is this allowed'."""
    _mock_ok()

    out = _out(runner.invoke(_app(), ["cairo", "acmesec"]))

    assert "support@acmesec.example" not in out
