"""
CAIRO connector: vendors and the applications assessed against them.

CAIRO is the internal TPRM (third-party risk) register. Unlike the other
connectors here it is NOT person-scoped -- the identifier is a vendor or
application name, never a username. It is deliberately excluded from the
Stage 7 person aggregate for that reason.

Two facts about the API drive most of this module's shape:

1. **There is no server-side filtering.** `?search=`, `?name=`, `?status=`,
   `?limit=` are all ignored -- `/api/vendors` always returns the full list
   (713 records / ~730KB at time of writing). So search is client-side, and
   the cache is load-bearing rather than a nicety.

2. **The application detail is nested inside the vendor detail.** There is
   no `/api/applications`. `GET /api/vendors/{id}` embeds `assessments[]`,
   each carrying an `engagement_context` that describes one application and
   how it is used.

All HTTP is mocked. Every credential here is obviously fake, and no fixture
uses a real vendor name.

Run just this connector:  pytest -m cairo
"""

from __future__ import annotations

import httpx
import pytest
import respx
from cairo_plugin.plugin import CairoPlugin

from lookup_cli.plugins.config import PluginConfig

pytestmark = pytest.mark.cairo

BASE = "https://tprm.example.internal"
VENDORS_URL = f"{BASE}/api/vendors"
VENDOR_ID = "e4df561c-0000-0000-0000-b792214ea7b2"

CONFIG = PluginConfig({"CAIRO_BASE_URL": BASE, "CAIRO_API_KEY": "not-a-real-key"})


def _vendor(
    name: str = "AcmeSec Corporation",
    status: str = "approved",
    vendor_id: str = VENDOR_ID,
    risk_level: str = "low",
    category: str = "Cloud SaaS",
    domain: str = "acmesec.example",
) -> dict:
    return {
        "id": vendor_id,
        "name": name,
        "domain": domain,
        "status": status,
        "risk_level": risk_level,
        "category": category,
        "primary_contact": "support@acmesec.example",
        "owner": "Morgan Vendorowner",
        "assigned_to": "reviewer@corp.example",
        "assigned_to_name": "Alex Reviewer",
        "data_classification": "Confidential",
        "last_approved_at": "2025-11-02 12:00:00+00",
        "next_review_at": "2026-11-02 12:00:00+00",
        "created_at": "2024-12-05 12:00:00+00",
        "parent_vendor_id": None,
    }


def _engagement(
    description: str = "Threat-intel IP enrichment for the security team.",
    engagement_type: str = "Cloud SaaS",
) -> dict:
    return {
        "description": description,
        "engagement_type": engagement_type,
        "data_classification": "Confidential",
        "production_access": "yes",
        "hosted_data_storage": "vendor cloud",
        "inherent_risk_level": "medium",
        "business_owner": "Dana Requester",
        "business_unit": "Security",
        "vendor_name": "AcmeSec Corporation",
        "jira_ticket": "SECSD-1000",
    }


def _detail(vendor: dict | None = None, assessments: list[dict] | None = None) -> dict:
    payload = dict(vendor or _vendor())
    payload["assessments"] = assessments if assessments is not None else [
        {
            "id": "191db0aa-0000-0000-0000-a2ce65958882",
            "vendor_id": payload["id"],
            "jira_ticket": "SECSD-1000",
            "quarter": "Q3-2026",
            "status": "post_complete",
            "workflow_status": "review_complete",
            "engagement_context": _engagement(),
        }
    ]
    payload["child_vendors"] = []
    return payload


def _plugin(config: PluginConfig = CONFIG) -> CairoPlugin:
    return CairoPlugin(config)


# --- Contract --------------------------------------------------------------
#
# CAIRO is deliberately NOT part of the Stage 7 person aggregate. Per the
# decision of 2026-09-04 that exclusion lives in the aggregate command's
# explicit plugin list (okta, jira, jamf, allwhere), NOT as a flag on
# the plugin -- so adding this connector required no change to core. The
# reason it matters: `lookup-cli lookup dluo` fanning out here would search
# for a *vendor named dluo* and report nothing found, which is a silent
# wrong answer in the view people trust most.


def test_required_credentials_are_declared():
    assert set(CairoPlugin.required_credentials) == {"CAIRO_BASE_URL", "CAIRO_API_KEY"}


# --- Long prose descriptions --------------------------------------------------


def test_summarise_keeps_a_short_description_intact():
    from cairo_plugin.plugin import summarise
    assert summarise("Short and sweet.", 80) == "Short and sweet."


def test_summarise_prefers_cutting_at_the_first_sentence():
    """Live descriptions are prose paragraphs. The first sentence is almost
    always the 'what is this' summary, so it beats a blind character cut."""
    from cairo_plugin.plugin import summarise
    text = "Acme is a SaaS threat-intel vendor. Their product does other things too."
    # Limit sits between the first sentence (35 chars) and the whole string (72),
    # so the sentence boundary is the interesting cut.
    assert summarise(text, 50) == "Acme is a SaaS threat-intel vendor."


def test_summarise_falls_back_to_a_character_cut_with_an_ellipsis():
    from cairo_plugin.plugin import summarise
    out = summarise("word " * 40, 40)
    assert len(out) <= 41
    assert out.endswith("\u2026")


def test_summarise_does_not_cut_mid_word():
    from cairo_plugin.plugin import summarise
    out = summarise("alpha beta gamma delta epsilon zeta eta theta", 20)
    assert not out.replace("\u2026", "").rstrip().endswith(("alph", "bet", "gamm"))


def test_summarise_handles_none():
    from cairo_plugin.plugin import summarise
    assert summarise(None, 80) is None


def test_a_first_sentence_that_is_still_too_long_gets_cut():
    from cairo_plugin.plugin import summarise
    out = summarise("a" * 200 + ". Second.", 60)
    assert len(out) <= 61


# --- Search ----------------------------------------------------------------


@respx.mock
async def test_exact_name_match_returns_one_vendor():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("AcmeSec Corporation")

    assert result.ok
    assert result.data["found"] is True
    assert result.data["vendor"]["name"] == "AcmeSec Corporation"
    assert result.data["vendor"]["status"] == "approved"


@respx.mock
async def test_search_is_case_insensitive_substring():
    """The API offers no filtering at all, so matching is ours to define.
    Substring beats exact: nobody types 'AcmeSec Corporation' from memory."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("acmesec")

    assert result.data["found"] is True


@respx.mock
async def test_multiple_matches_are_returned_without_picking_one():
    """Auto-selecting the 'best' match would be a guess. An approval answer
    for the wrong vendor is worse than making someone choose."""
    respx.get(VENDORS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _vendor("Databright Inc", vendor_id="id-1"),
                _vendor("Databright Plugin", vendor_id="id-2", status="denied"),
            ],
        )
    )

    result = await _plugin().fetch("databright")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["ambiguous"] is True
    assert len(result.data["matches"]) == 2
    assert {m["name"] for m in result.data["matches"]} == {"Databright Inc", "Databright Plugin"}


@respx.mock
async def test_an_exact_name_wins_over_other_substring_matches():
    """'Slack' should resolve, not be drowned by 'Slack Connector Plugin'."""
    respx.get(VENDORS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _vendor("Databright Plugin", vendor_id="id-2"),
                _vendor("Databright", vendor_id="id-1"),
            ],
        )
    )
    respx.get(f"{VENDORS_URL}/id-1").mock(
        return_value=httpx.Response(200, json=_detail(_vendor("Databright", vendor_id="id-1")))
    )

    result = await _plugin().fetch("Databright")

    assert result.data["found"] is True
    assert result.data["vendor"]["name"] == "Databright"


@respx.mock
async def test_no_match_is_a_success_not_an_error():
    """Consistent with the Okta connector: 'no such vendor' is a real answer,
    not a failure to reach CAIRO."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))

    result = await _plugin().fetch("nothing-like-this")

    assert result.ok
    assert result.data["found"] is False
    assert result.data["matches"] == []
    assert "not-found" in result.tags


@respx.mock
async def test_matches_are_sorted_by_name():
    respx.get(VENDORS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _vendor("zeta data", vendor_id="id-3"),
                _vendor("Alpha data", vendor_id="id-1"),
                _vendor("middle data", vendor_id="id-2"),
            ],
        )
    )

    result = await _plugin().fetch("data")

    assert [m["name"] for m in result.data["matches"]] == ["Alpha data", "middle data", "zeta data"]


# --- Applications nested in the vendor detail --------------------------------


@respx.mock
async def test_applications_come_from_the_nested_assessments():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("acmesec")

    apps = result.data["applications"]
    assert len(apps) == 1
    assert apps[0]["description"].startswith("Threat-intel")
    assert apps[0]["engagement_type"] == "Cloud SaaS"
    assert apps[0]["data_classification"] == "Confidential"
    assert apps[0]["production_access"] == "yes"
    assert apps[0]["workflow_status"] == "review_complete"


@respx.mock
async def test_a_vendor_can_have_several_applications():
    detail = _detail(
        assessments=[
            {
                "id": "a1", "status": "post_complete", "workflow_status": "review_complete",
                "engagement_context": _engagement("First use case", "Cloud SaaS"),
            },
            {
                "id": "a2", "status": "pending", "workflow_status": "draft",
                "engagement_context": _engagement("Second use case", "Plugin/Extension/Integration"),
            },
        ]
    )
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=detail))

    result = await _plugin().fetch("acmesec")

    assert len(result.data["applications"]) == 2


@respx.mock
async def test_a_vendor_with_no_assessment_is_not_a_vendor_with_no_applications():
    """174 of 713 vendors are in this state. Rendering it as 'no applications'
    would assert something the register does not actually say."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(
        return_value=httpx.Response(200, json=_detail(assessments=[]))
    )

    result = await _plugin().fetch("acmesec")

    assert result.ok
    assert result.data["applications"] == []
    assert result.data["assessed"] is False


@respx.mock
async def test_an_assessed_vendor_is_marked_assessed():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("acmesec")

    assert result.data["assessed"] is True


@respx.mock
async def test_missing_engagement_context_does_not_crash():
    detail = _detail(assessments=[{"id": "a1", "status": "pending"}])
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=detail))

    result = await _plugin().fetch("acmesec")

    assert result.ok
    assert result.data["applications"][0]["description"] is None


# --- The three status fields must not be conflated ---------------------------


@respx.mock
async def test_vendor_status_and_assessment_status_are_kept_separate():
    """They disagree on all 713 live records because they measure different
    things: vendor.status is approval, assessment.status is workflow state.
    Merging them would produce a confident wrong answer about whether
    something is allowed."""
    detail = _detail(
        _vendor(status="approved"),
        assessments=[
            {
                "id": "a1", "status": "exempt", "workflow_status": "draft",
                "engagement_context": _engagement(),
            }
        ],
    )
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=detail))

    result = await _plugin().fetch("acmesec")

    assert result.data["vendor"]["status"] == "approved"
    assert result.data["applications"][0]["assessment_status"] == "exempt"
    assert result.data["applications"][0]["workflow_status"] == "draft"


@pytest.mark.parametrize(
    "status", ["approved", "denied", "pending_approval", "review_required", "under_review"]
)
@respx.mock
async def test_every_approval_status_is_reported_verbatim(status):
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor(status=status)]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(
        return_value=httpx.Response(200, json=_detail(_vendor(status=status)))
    )

    result = await _plugin().fetch("acmesec")

    assert result.data["vendor"]["status"] == status
    assert result.data["approved"] is (status == "approved")


# --- Personal data is dropped before it can reach the cache -------------------


@respx.mock
async def test_the_application_owner_is_carried():
    """Requested 2026-09-04. `engagement_context.business_owner` is the person
    who owns *this application* -- 223 distinct people on live data."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("acmesec")

    assert result.data["applications"][0]["business_owner"] == "Dana Requester"


@respx.mock
async def test_the_vendor_owner_is_carried_separately_from_the_application_owner():
    """They are different concepts and disagree on 312 of 568 live records:
    `owner` owns the vendor relationship (67 people), `business_owner` owns
    one application (223 people). Merging them would misattribute ownership."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("acmesec")

    assert result.data["vendor"]["owner"] == "Morgan Vendorowner"
    assert result.data["applications"][0]["business_owner"] == "Dana Requester"


@respx.mock
async def test_the_reviewer_is_never_carried():
    """`assigned_to` / `assigned_to_name` is the TPRM analyst who ran the
    review -- only 5 distinct people across the whole register. Explicitly
    not wanted, and showing it where an owner is expected would point at the
    wrong person entirely."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("acmesec")

    blob = repr(result.data)
    assert "Alex Reviewer" not in blob
    assert "reviewer@corp.example" not in blob


@respx.mock
async def test_third_party_contact_details_are_not_carried():
    """The vendor's own contact details are not an owner and are not needed
    to answer 'is this allowed'. Everything in `data` lands in the plaintext
    cache, so unused personal data is not collected."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=_detail()))

    result = await _plugin().fetch("acmesec")

    assert "support@acmesec.example" not in repr(result.data)


@respx.mock
async def test_a_missing_business_owner_does_not_crash():
    detail = _detail()
    detail["assessments"][0]["engagement_context"].pop("business_owner")
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(200, json=detail))

    result = await _plugin().fetch("acmesec")

    assert result.ok
    assert result.data["applications"][0]["business_owner"] is None


# --- Failure modes ------------------------------------------------------------


@respx.mock
async def test_unauthorized_is_an_actionable_error():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(401))

    result = await _plugin().fetch("acmesec")

    assert not result.ok
    assert "cairo" in result.error.lower() or "key" in result.error.lower()


@respx.mock
async def test_timeout_is_an_error_result():
    respx.get(VENDORS_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await _plugin().fetch("acmesec")

    assert not result.ok


@respx.mock
async def test_server_error_is_an_error_result():
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(503))

    result = await _plugin().fetch("acmesec")

    assert not result.ok


async def test_missing_credentials_become_an_error_result():
    result = await CairoPlugin(PluginConfig({})).fetch("acmesec")

    assert not result.ok
    assert "CAIRO_BASE_URL" in result.error


@respx.mock
async def test_the_api_key_never_appears_in_an_error():
    key = "s3cr3t-cairo-key-value-0000"
    config = PluginConfig({"CAIRO_BASE_URL": BASE, "CAIRO_API_KEY": key})
    respx.get(VENDORS_URL).mock(side_effect=httpx.HTTPError(f"failed using Bearer {key}"))

    result = await _plugin(config).fetch("acmesec")

    assert key not in result.error


@respx.mock
async def test_a_detail_failure_still_reports_the_vendor_it_matched():
    """The list call already answered 'does this vendor exist and is it
    approved'. Losing the nested applications degrades that section only."""
    respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[_vendor()]))
    respx.get(f"{VENDORS_URL}/{VENDOR_ID}").mock(return_value=httpx.Response(503))

    result = await _plugin().fetch("acmesec")

    assert result.ok
    assert result.data["vendor"]["status"] == "approved"
    assert result.data["applications"] == []
    assert result.data["detail_error"] is not None


# --- Auth wiring ----------------------------------------------------------------


@respx.mock
async def test_the_key_is_sent_as_a_bearer_token():
    route = respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin().fetch("anything")

    assert route.calls.last.request.headers["Authorization"] == "Bearer not-a-real-key"


@respx.mock
async def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    config = PluginConfig({"CAIRO_BASE_URL": f"{BASE}/", "CAIRO_API_KEY": "k"})
    route = respx.get(VENDORS_URL).mock(return_value=httpx.Response(200, json=[]))

    await _plugin(config).fetch("anything")

    assert route.called


# --- Mock mode --------------------------------------------------------------------


async def test_mock_mode_works_with_no_credentials():
    plugin = CairoPlugin(PluginConfig({"LOOKUP_CLI_MOCK_CAIRO": "1"}))

    result = await plugin.fetch("acmesec")

    assert result.ok
    assert result.data["vendor"]["name"]
    assert result.data["applications"]
