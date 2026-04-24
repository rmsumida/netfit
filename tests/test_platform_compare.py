"""Unit tests for platform_compare — speed allocation, ranking, backward
compatibility, and platform YAML structure validation.

These tests replace the hand-rolled `validate_phase*.py` scripts that
previously exercised the same logic via print-statements and manual
inspection."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from platform_compare import (
    _allocate_speed_capacity,
    _get,
    compare_platforms,
    load_target_profiles,
    rank_assessment,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLATFORMS_DIR = PROJECT_ROOT / "platforms"


# ---------------------------------------------------------------------------
# _allocate_speed_capacity scenarios
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,source_demand,target_native,target_breakout,expected_ok,expected_unmet",
    [
        (
            "direct_speed_match",
            {"1G": 5, "10G": 4},
            {"1G": 5, "10G": 4},
            {},
            True, {},
        ),
        (
            "upward_substitution_1g_uses_10g",
            {"1G": 10, "10G": 4},
            {"1G": 5, "10G": 9},  # 5 extra 10G ports absorb the excess 1G demand
            {},
            True, {},
        ),
        (
            "unmet_high_speed_demand",
            {"1G": 5, "10G": 10},
            {"1G": 20, "10G": 4},
            {},
            False, {"10G": 6},
        ),
        (
            "partial_headroom",
            {"1G": 13, "10G": 8},
            {"1G": 20, "10G": 6},
            {},
            False, {"10G": 2},
        ),
        (
            "single_speed_class_exact_fit",
            {"10G": 8},
            {"1G": 0, "10G": 8},
            {},
            True, {},
        ),
        (
            "empty_demand",
            {},
            {"1G": 20, "10G": 6},
            {},
            True, {},
        ),
        (
            "high_speed_mix_with_100g",
            {"1G": 5, "10G": 4, "100G": 2},
            {"1G": 0, "10G": 4, "100G": 12},
            {},
            True, {},  # 1G borrows from 100G via upward substitution
        ),
        (
            "real_config_baseline",
            # Sample config demand. C8500-20X6C corrected native supply.
            {"1G": 13, "10G": 8},
            {"10G": 20, "100G": 6},
            {"100G_to_4x25G": 6},
            True, {},  # 1G upward-subs into 10G, 10G all-native, all satisfied
        ),
        # --- breakout fanout cases (issue #16) ---
        (
            "breakout_satisfies_unmet_10g",
            # 6 native + 4 from one 40G → 4×10G fanout = 10 total
            {"10G": 10}, {"10G": 6, "40G": 1}, {"40G_to_4x10G": 1},
            True, {},
        ),
        (
            "breakout_partial_consumption_wastes_children",
            # Demand 5 → ceil(5/4)=2 parents consumed, yields 8 children;
            # 5 satisfied, 3 children discarded. Wastage is by design.
            {"10G": 5}, {"10G": 0, "40G": 2}, {"40G_to_4x10G": 2},
            True, {},
        ),
        (
            "breakout_falls_back_to_upward_when_no_breakout",
            # 2 native 1G + 4 upward from 10G = 6 total
            {"1G": 6}, {"1G": 2, "10G": 4}, {},
            True, {},
        ),
        (
            "breakout_requires_native_parent_supply",
            # Breakout slots advertised but no native parent ports → fanout
            # consumes nothing; falls through to upward (none available) →
            # remains unmet.
            {"10G": 4}, {"10G": 0, "40G": 0}, {"40G_to_4x10G": 1},
            False, {"10G": 4},
        ),
        (
            "real_c8500_12x4qc_sample_workload",
            # Acceptance test: corrected 12X4QC YAML against the sample
            # workload (13×1G + 8×10G). Without breakout this reports unmet
            # 10G; with breakout fanout it should allocate cleanly.
            {"1G": 13, "10G": 8},
            {"10G": 12, "40G": 2, "100G": 2},
            {"40G_to_4x10G": 2, "100G_to_4x25G": 2},
            True, {},
        ),
    ]
)
def test_allocate_speed_capacity_scenarios(
    name, source_demand, target_native, target_breakout,
    expected_ok, expected_unmet,
):
    result = _allocate_speed_capacity(source_demand, target_native, target_breakout)
    assert result["allocation_ok"] == expected_ok, (
        f"[{name}] allocation_ok mismatch. "
        f"Got {result['allocation_ok']}, expected {expected_ok}. "
        f"Detail: {result['allocation_detail']}"
    )
    assert result["unmet_demand"] == expected_unmet, (
        f"[{name}] unmet_demand mismatch. "
        f"Got {result['unmet_demand']}, expected {expected_unmet}"
    )


def test_allocate_speed_capacity_preserves_supply_accounting():
    """Supply consumed for upward-substitution must be debited from the
    correct higher-speed pool, not ghost-consumed."""
    result = _allocate_speed_capacity(
        source_demand_by_speed={"1G": 8},
        target_native_supply={"1G": 3, "10G": 10},
        target_breakout={},
    )
    # 3 matched native 1G + 5 matched upward from 10G = 8 total, no unmet.
    assert result["allocation_ok"] is True
    assert result["allocation_detail"]["1G"]["matched_native"] == 3
    # New disaggregated field name (#16):
    assert result["allocation_detail"]["1G"]["matched_native_upward"] == 5
    # Legacy alias (matched_breakout = upward + fanout) preserved:
    assert result["allocation_detail"]["1G"]["matched_breakout"] == 5
    assert result["allocation_detail"]["1G"]["matched_breakout_fanout"] == 0
    assert result["remaining_supply_by_speed"]["10G"] == 5  # 10 - 5 consumed


def test_allocate_speed_capacity_breakout_used_field():
    """The `breakout_used` dict counts parent slots consumed per breakout key,
    and the `matched_breakout_fanout` per-speed field counts child ports
    actually used. Legacy `matched_breakout` = upward + fanout."""
    result = _allocate_speed_capacity(
        source_demand_by_speed={"10G": 10},
        target_native_supply={"10G": 6, "40G": 1},
        target_breakout={"40G_to_4x10G": 1},
    )
    assert result["allocation_ok"] is True
    assert result["breakout_used"] == {"40G_to_4x10G": 1}
    assert result["allocation_detail"]["10G"]["matched_native"] == 6
    assert result["allocation_detail"]["10G"]["matched_breakout_fanout"] == 4
    assert result["allocation_detail"]["10G"]["matched_native_upward"] == 0
    # Legacy alias should equal the sum of upward + fanout.
    assert result["allocation_detail"]["10G"]["matched_breakout"] == 4
    # The parent 40G port was consumed by the breakout.
    assert result["remaining_supply_by_speed"]["40G"] == 0


def test_allocate_speed_capacity_breakout_consumes_parent_slot_atomically():
    """Each breakout consumes one parent slot regardless of how many child
    ports the demand actually uses. Surplus children are discarded — banking
    them would misrepresent the physical breakout commitment."""
    result = _allocate_speed_capacity(
        source_demand_by_speed={"10G": 5},
        target_native_supply={"10G": 0, "40G": 2},
        target_breakout={"40G_to_4x10G": 2},
    )
    assert result["allocation_ok"] is True
    # 2 parents consumed (ceil(5/4) = 2), yielding 8 children, 5 used, 3 discarded.
    assert result["breakout_used"] == {"40G_to_4x10G": 2}
    assert result["allocation_detail"]["10G"]["matched_breakout_fanout"] == 5
    assert result["remaining_supply_by_speed"]["40G"] == 0


# ---------------------------------------------------------------------------
# rank_assessment ordering
# ---------------------------------------------------------------------------

def _make_assessment(overall, crit=0, high=0, med=0, low=0, risk=0):
    return {
        "assessment_summary": {
            "overall_recommendation": overall,
            "finding_counts": {"critical": crit, "high": high, "medium": med, "low": low},
            "total_risk_score": risk,
        }
    }


def test_rank_assessment_orders_by_recommendation_first():
    """LIKELY_FIT ranks before CONDITIONAL_FIT ranks before HIGH_RISK, regardless
    of finding counts."""
    likely = rank_assessment(_make_assessment("LIKELY_FIT", crit=5))
    conditional = rank_assessment(_make_assessment("CONDITIONAL_FIT"))
    high_risk = rank_assessment(_make_assessment("HIGH_RISK"))
    assert likely < conditional < high_risk


def test_rank_assessment_breaks_ties_by_severity_counts():
    a = rank_assessment(_make_assessment("CONDITIONAL_FIT", crit=1))
    b = rank_assessment(_make_assessment("CONDITIONAL_FIT", crit=0, high=2))
    # Fewer critical trumps more high findings.
    assert b < a


# ---------------------------------------------------------------------------
# Backward-compat: old analysis JSON (pre-iteration-1 fields) must still score
# ---------------------------------------------------------------------------

def _analysis_without_fields(analysis, field_names, section="interfaces"):
    stripped = copy.deepcopy(analysis)
    for name in field_names:
        stripped.get(section, {}).pop(name, None)
    return stripped


def test_compare_platforms_handles_legacy_analysis_shape():
    """If analyzer output lacks the iteration-1 `active_*` fields (because an
    older report is being scored), compare_platforms must still produce
    results using the _get() default fallbacks — not crash with KeyError."""
    analysis_path = PROJECT_ROOT / "output" / "analysis_report.json"
    if not analysis_path.exists():
        pytest.skip("Run `python3 main.py` first to generate analysis_report.json")
    analysis = json.loads(analysis_path.read_text())

    legacy = _analysis_without_fields(analysis, [
        "active_total", "active_physical_count", "active_physical_by_type",
        "active_subinterfaces", "active_tunnels", "active_loopbacks",
        "active_svis", "active_port_channels", "active_physical_by_speed_class",
        "active_physical_by_role",
    ])
    profiles = load_target_profiles(str(PLATFORMS_DIR))
    result = compare_platforms(legacy, profiles)
    assert result["platform_count"] == len(profiles)
    for r in result["results"]:
        assert isinstance(r["fitness_score"], (int, float))


def test_get_returns_default_on_missing_path():
    data = {"a": {"b": 1}}
    assert _get(data, ["a", "b"]) == 1
    assert _get(data, ["a", "x"], "fallback") == "fallback"
    assert _get(data, ["missing"], 0) == 0
    # _get must not error when it traverses through a non-dict.
    assert _get({"a": [1, 2, 3]}, ["a", "b"], None) is None


# ---------------------------------------------------------------------------
# Platform YAML schema expectations
# ---------------------------------------------------------------------------

EXPECTED_CAPABILITY_KEYS = {
    "supports_subinterfaces", "supports_trunking", "supports_etherchannel",
    "supports_vrf", "supports_ospf", "supports_bgp", "supports_hsrp",
    "supports_aaa", "supports_tacacs", "supports_radius", "supports_snmp",
    "supports_ntp", "supports_nat", "supports_qos", "supports_crypto",
    "supports_ipsec", "supports_tunnel_interfaces",
    "supported_interface_types",
}

EXPECTED_SCALE_KEYS = {
    "max_interfaces", "max_physical_interfaces", "max_l3_interfaces",
    "max_subinterfaces", "max_vrfs", "max_bgp_neighbors",
    "max_static_routes", "max_tunnels", "ports",
}


@pytest.mark.parametrize("yaml_file", sorted(PLATFORMS_DIR.glob("*.yaml")))
def test_platform_yaml_has_required_structure(yaml_file):
    profile = yaml.safe_load(yaml_file.read_text())
    assert isinstance(profile, dict), f"{yaml_file} must be a mapping"
    assert "platform_name" in profile
    assert "capabilities" in profile
    assert "scale" in profile
    assert "fit_preferences" in profile

    # Scale block must carry explicit port inventory by speed class.
    scale = profile["scale"]
    missing_scale = EXPECTED_SCALE_KEYS - set(scale)
    assert not missing_scale, (
        f"{yaml_file.name} scale block missing: {missing_scale}"
    )
    ports = scale["ports"]
    assert "native" in ports, f"{yaml_file.name} missing scale.ports.native"
    assert "breakout" in ports, f"{yaml_file.name} missing scale.ports.breakout"

    # Capabilities must cover the feature set the assessor checks.
    missing_caps = EXPECTED_CAPABILITY_KEYS - set(profile["capabilities"])
    assert not missing_caps, (
        f"{yaml_file.name} capabilities missing: {missing_caps}"
    )


def test_max_physical_interfaces_falls_back_to_max_interfaces():
    """If a platform YAML lacks max_physical_interfaces, fitness scoring
    must fall back to max_interfaces (see compute_platform_fitness)."""
    # Synthesize a profile missing max_physical_interfaces.
    scale = {"max_interfaces": 28}
    effective = scale.get("max_physical_interfaces") or scale.get("max_interfaces", 0)
    assert effective == 28


# ---------------------------------------------------------------------------
# NAT + IPsec SA ceiling findings (PR-B).
# ---------------------------------------------------------------------------

def _nat_ipsec_analysis(active_nat=None, peak_nat=None, active_sas=None):
    """Minimal analysis skeleton that exercises the NAT + IPsec SA assessor
    paths. `routing` stays empty; `services.nat_present` and
    `crypto_vpn.ipsec_present` are flagged on so the assessor enters both
    code paths."""
    return {
        "summary": {"hostname": "test", "routing_protocols_enabled": []},
        "interfaces": {
            "total": 0, "active_total": 0, "active_physical_count": 0,
            "active_subinterfaces": 0, "active_tunnels": 0,
            "active_physical_by_type": {}, "active_physical_by_speed_class": {},
            "active_physical_by_role": {}, "active_management_interfaces": 0,
            "active_wan_physical_count": 0, "active_lan_physical_count": 0,
            "active_uplink_physical_count": 0, "active_port_channel_member_count": 0,
            "active_port_channels": 0, "by_type": {},
            "layer2_access_count": 0, "layer2_trunk_count": 0, "layer3_count": 0,
        },
        "switching": {"spanning_tree": {"present": False}},
        "routing": {"vrfs": [], "static_route_count": 0, "protocols": {}, "bgp": {}},
        "high_availability": {},
        "security": {},
        "services": {"nat_present": True, "nat_line_count": 10},
        "policy": {},
        "management_plane": {},
        "crypto_vpn": {
            "crypto_present": True, "ipsec_present": True,
            "crypto_line_count": 10,
        },
        "runtime": {
            "nat": {
                "active_translations": active_nat,
                "peak_translations": peak_nat,
            },
            "crypto": {"active_sas": active_sas},
        },
    }


def _profile_with_ceilings(
    max_nat_translations=None, max_ipsec_sas=None,
):
    """Synthetic target profile exercising only the assessor scale paths
    relevant to this test group. Capabilities are fully permissive so the
    critical-unsupported paths don't fire and mask the headroom logic."""
    return {
        "platform_name": "Test_Target",
        "capabilities": {
            "supported_interface_types": [],
            "supports_nat": True, "supports_crypto": True,
            "supports_ipsec": True, "supports_ikev2": True,
            "supports_isakmp": True, "supports_tunnel_interfaces": True,
            "supports_bgp": True, "supports_vrf": True, "supports_ospf": True,
            "supports_hsrp": True, "supports_aaa": True,
            "supports_subinterfaces": True,
        },
        "scale": {
            "max_interfaces": 10000, "max_physical_interfaces": 10000,
            "max_subinterfaces": 10000, "max_vrfs": 10000,
            "max_bgp_neighbors": 10000, "max_static_routes": 100000,
            "max_tunnels": 10000, "ports": {"native": {}, "breakout": {}},
            "max_nat_translations": max_nat_translations,
            "max_ipsec_sas": max_ipsec_sas,
        },
        "fit_preferences": {},
        "constraints": {},
        "notes": [],
    }


def test_nat_translation_ceiling_exceeded_flags_finding():
    from assessor import assess_refresh
    analysis = _nat_ipsec_analysis(peak_nat=150_000)
    profile = _profile_with_ceilings(max_nat_translations=100_000)
    result = assess_refresh(analysis, profile)
    titles = [f["title"] for f in result["findings"]]
    assert "NAT translation scale exceeds target profile" in titles


def test_nat_translation_headroom_approaching_flags_advisory():
    """Peak translations at ~80% of ceiling should produce an advisory
    headroom finding (low severity), not a hard exceeds finding."""
    from assessor import assess_refresh
    analysis = _nat_ipsec_analysis(peak_nat=80_000)
    profile = _profile_with_ceilings(max_nat_translations=100_000)
    result = assess_refresh(analysis, profile)
    nat_findings = [
        f for f in result["findings"] if "NAT translation" in f["title"]
    ]
    assert any("approaching" in f["title"].lower() for f in nat_findings)
    assert not any("exceeds" in f["title"].lower() for f in nat_findings)


def test_nat_uses_peak_translations_when_present_else_active():
    """When runtime harvest carries peak_translations, the assessor should
    compare against peak (the high-water-mark demand), not the instantaneous
    active count."""
    from assessor import assess_refresh
    # Active below ceiling, peak above — finding should fire off peak.
    analysis = _nat_ipsec_analysis(active_nat=50_000, peak_nat=150_000)
    profile = _profile_with_ceilings(max_nat_translations=100_000)
    result = assess_refresh(analysis, profile)
    titles = [f["title"] for f in result["findings"]]
    assert "NAT translation scale exceeds target profile" in titles


def test_nat_finding_skipped_without_ceiling():
    """If the target profile has no max_nat_translations, the assessor must
    not emit any NAT-scale finding — no false positives from missing data."""
    from assessor import assess_refresh
    analysis = _nat_ipsec_analysis(peak_nat=500_000)
    profile = _profile_with_ceilings(max_nat_translations=None)
    result = assess_refresh(analysis, profile)
    nat_scale_findings = [
        f for f in result["findings"]
        if "NAT translation" in f["title"]
    ]
    assert nat_scale_findings == []


def test_ipsec_sa_ceiling_exceeded_flags_finding():
    from assessor import assess_refresh
    analysis = _nat_ipsec_analysis(active_sas=12_000)
    profile = _profile_with_ceilings(max_ipsec_sas=8_000)
    result = assess_refresh(analysis, profile)
    titles = [f["title"] for f in result["findings"]]
    assert "IPsec SA scale exceeds target profile" in titles


def test_ipsec_sa_headroom_approaching_flags_advisory():
    from assessor import assess_refresh
    analysis = _nat_ipsec_analysis(active_sas=7_000)
    profile = _profile_with_ceilings(max_ipsec_sas=8_000)
    result = assess_refresh(analysis, profile)
    crypto_findings = [
        f for f in result["findings"] if "IPsec SA" in f["title"]
    ]
    assert any("approaching" in f["title"].lower() for f in crypto_findings)
    assert not any("exceeds" in f["title"].lower() for f in crypto_findings)


def test_ipsec_sa_finding_skipped_without_ceiling_or_runtime():
    from assessor import assess_refresh
    # Ceiling declared, runtime absent -> no finding.
    analysis = _nat_ipsec_analysis(active_sas=None)
    profile = _profile_with_ceilings(max_ipsec_sas=8_000)
    result = assess_refresh(analysis, profile)
    assert not any("IPsec SA" in f["title"] for f in result["findings"])
    # Runtime present, ceiling absent -> no finding.
    analysis = _nat_ipsec_analysis(active_sas=99_999)
    profile = _profile_with_ceilings(max_ipsec_sas=None)
    result = assess_refresh(analysis, profile)
    assert not any("IPsec SA" in f["title"] for f in result["findings"])


# ---------------------------------------------------------------------------
# Scale-comparison table renderer (PR-B).
# ---------------------------------------------------------------------------

def test_scale_comparison_rows_include_populated_and_skip_fully_unknown():
    """Rows where both current and ceiling are None should be dropped; rows
    where ceiling is missing but current is known should stay in so the
    reader can see which dimensions have no published ceiling yet."""
    from platform_compare import _scale_comparison_rows
    analysis = _nat_ipsec_analysis(peak_nat=100, active_sas=None)
    result = {
        "target_scale": {
            "max_physical_interfaces": 26, "max_subinterfaces": 2000,
            "max_vrfs": 128, "max_bgp_neighbors": 1000,
            "max_static_routes": 20000, "max_tunnels": 2000,
            # No max_nat_translations or max_ipsec_sas — audit gap.
        },
    }
    rows = _scale_comparison_rows(result, analysis)
    dims = [r[0] for r in rows]
    # NAT row must still appear (current known, ceiling absent).
    assert any("NAT translations" in d for d in dims)
    # IPsec SA row must be dropped (both current and ceiling absent).
    assert not any("IPsec SAs" in d for d in dims)
    # Verdict for the NAT row should flag the missing ceiling explicitly.
    nat_row = next(r for r in rows if "NAT translations" in r[0])
    assert "No ceiling declared" in nat_row[4]


def test_scale_comparison_rows_classify_headroom_bands():
    """Verdict glyph should match the assessor's thresholds (≥90% close,
    ≥75% tight, >100% exceeded) so the two signals agree."""
    from platform_compare import _scale_verdict_glyph
    assert "Exceeded" in _scale_verdict_glyph(150, 100)[1]
    assert "Close" in _scale_verdict_glyph(95, 100)[1]
    assert "Tight" in _scale_verdict_glyph(80, 100)[1]
    assert "Within" in _scale_verdict_glyph(50, 100)[1]
    # Missing ceiling -> "No ceiling declared"
    assert "No ceiling declared" in _scale_verdict_glyph(100, None)[1]
    # Missing current -> "Current workload unknown"
    assert "unknown" in _scale_verdict_glyph(None, 100)[1].lower()
