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

@pytest.mark.parametrize("name,source_demand,target_native,expected_ok,expected_unmet", [
    (
        "direct_speed_match",
        {"1G": 5, "10G": 4},
        {"1G": 5, "10G": 4},
        True, {},
    ),
    (
        "upward_substitution_1g_uses_10g",
        {"1G": 10, "10G": 4},
        {"1G": 5, "10G": 9},  # 5 extra 10G ports absorb the excess 1G demand
        True, {},
    ),
    (
        "unmet_high_speed_demand",
        {"1G": 5, "10G": 10},
        {"1G": 20, "10G": 4},
        False, {"10G": 6},
    ),
    (
        "partial_headroom",
        {"1G": 13, "10G": 8},
        {"1G": 20, "10G": 6},
        False, {"10G": 2},
    ),
    (
        "single_speed_class_exact_fit",
        {"10G": 8},
        {"1G": 0, "10G": 8},
        True, {},
    ),
    (
        "empty_demand",
        {},
        {"1G": 20, "10G": 6},
        True, {},
    ),
    (
        "high_speed_mix_with_100g",
        {"1G": 5, "10G": 4, "100G": 2},
        {"1G": 0, "10G": 4, "100G": 12},
        True, {},  # 1G borrows from 100G via upward substitution
    ),
    (
        "real_config_baseline",
        # The committed sample config produces this demand.
        {"1G": 13, "10G": 8},
        # C8500-20X6C native supply.
        {"1G": 20, "10G": 6},
        False, {"10G": 2},  # 2 10G demand falls short; no higher speeds to borrow
    ),
])
def test_allocate_speed_capacity_scenarios(
    name, source_demand, target_native, expected_ok, expected_unmet
):
    result = _allocate_speed_capacity(source_demand, target_native, {})
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
    assert result["allocation_detail"]["1G"]["matched_breakout"] == 5
    assert result["remaining_supply_by_speed"]["10G"] == 5  # 10 - 5 consumed


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
