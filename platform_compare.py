"""Platform fitness scoring and comparison reporting.

Given an analyzed source device and a set of candidate target-platform profiles,
this module ranks the platforms by fit for a hardware refresh and renders the
result in JSON, Markdown, and HTML.

Pipeline entry point: `build_platform_comparison_reports` (bottom of file).
"""
import datetime
import html
import json
import os
from pathlib import Path

import yaml

from allocation import _parse_breakout_key, allocate_speed_capacity
from assessor import assess_refresh

NETFIT_VERSION = "0.6.0"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_target_profiles(folder_path):
    folder = Path(folder_path)
    if not folder.exists():
        raise FileNotFoundError(f"Target profiles folder not found: {folder_path}")

    profiles = []
    seen_files = set()
    for pattern in ("*.yaml", "*.yml"):
        for file in sorted(folder.glob(pattern)):
            resolved = str(file.resolve())
            if resolved in seen_files:
                continue
            profile = load_yaml(file)
            if not isinstance(profile, dict):
                raise ValueError(
                    f"Invalid YAML profile format in {file}; expected a mapping."
                )
            profile["_source_file"] = str(file)
            profiles.append(profile)
            seen_files.add(resolved)

    if not profiles:
        raise ValueError(f"No YAML target profiles found in folder: {folder_path}")
    return profiles


def save_json(data, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def save_text(content, output_path):
    Path(output_path).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

SPEED_SORT_ORDER = ["100M", "1G", "10G", "25G", "40G", "100G"]

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

RECOMMENDATION_RANK = {
    "LIKELY_FIT": 0,
    "CONDITIONAL_FIT": 1,
    "HIGH_RISK": 2,
    "NOT_RECOMMENDED": 3,
    "UNKNOWN": 4,
}


def _get(dct, path, default=None):
    current = dct
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _compute_headroom_ratio(current, maximum):
    if not maximum:
        return None
    try:
        return current / maximum
    except ZeroDivisionError:
        return None


def _sort_speeds(keys):
    seen = set()
    ordered = [k for k in SPEED_SORT_ORDER if k in keys]
    seen.update(ordered)
    rest = sorted(k for k in keys if k not in seen)
    return ordered + rest


def _sev_icon(level):
    return {"critical": "🔴", "high": "🟠", "medium": "🟡",
            "low": "🔵", "info": "⚪"}.get(level, "⚪")


def _sev_badge_md(severity):
    return f"{_sev_icon(severity)} {severity.capitalize()}"


def _sev_badge_html(severity):
    label = html.escape(severity.capitalize())
    css_class = f"sev-{severity}" if severity in SEVERITY_RANK else "sev-info"
    return f'<span class="badge {css_class}">{label}</span>'


def _recommendation_badge_html(rec):
    css_map = {
        "LIKELY_FIT": "rec-fit",
        "CONDITIONAL_FIT": "rec-conditional",
        "HIGH_RISK": "rec-risk",
        "NOT_RECOMMENDED": "rec-nope",
        "UNKNOWN": "rec-unknown",
    }
    css = css_map.get(rec, "rec-unknown")
    return f'<span class="rec {css}">{html.escape(rec)}</span>'


def _bool_str(value, yes="Yes", no="No"):
    return yes if value else no


# ---------------------------------------------------------------------------
# Speed-capacity allocation (kept as module-level alias so existing imports
# `from platform_compare import _allocate_speed_capacity` continue to work).
# ---------------------------------------------------------------------------

def _allocate_speed_capacity(source_demand_by_speed, target_native_supply, target_breakout):
    return allocate_speed_capacity(source_demand_by_speed, target_native_supply, target_breakout)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def _generation_timestamp():
    """ISO-8601 UTC timestamp for output provenance.

    Honors SOURCE_DATE_EPOCH (reproducible-builds standard) so tests/CI can
    pin a fixed timestamp for byte-equality checks. Falls back to current UTC.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            ts = datetime.datetime.fromtimestamp(int(epoch), tz=datetime.timezone.utc)
        except (TypeError, ValueError):
            ts = datetime.datetime.now(datetime.timezone.utc)
    else:
        ts = datetime.datetime.now(datetime.timezone.utc)
    return ts.replace(microsecond=0).isoformat()


def _build_metadata(analysis):
    return {
        "generated_at": _generation_timestamp(),
        "netfit_version": NETFIT_VERSION,
        "source_hostname": analysis.get("summary", {}).get("hostname", "UNKNOWN"),
    }


# ---------------------------------------------------------------------------
# Score-breakdown grouping (compact rendering for MD/HTML)
# ---------------------------------------------------------------------------

# Each group is (label, list-of-keyword-substrings). The first group whose
# keyword matches a row's factor string claims it. Order matters — verdict
# checks live before workload/feature checks so general substrings don't
# poach verdict rows.
_BREAKDOWN_GROUPS = (
    ("Verdict & risk", (
        "Overall recommendation:", "Critical findings:", "High findings:",
        "Medium findings:", "Low findings:", "Total risk score:",
    )),
    ("Capacity & allocation", (
        "Physical interface capacity", "Physical interface usage",
        "Subinterface capacity", "Subinterface usage", "Subinterfaces",
        "Unsupported interface types", "Unsupported interface type findings",
        "Speed matching", "Supply ratio", "Unmet high-speed", "Unmet mid/low-speed",
    )),
    ("Scale headroom", ("headroom",)),
    ("Role alignment", (
        "Intended role is WAN edge", "Role alignment", "Branch bias",
        "High scale WAN edge", "WAN edge role and WAN interfaces",
    )),
    ("Workload weights", (
        "Throughput weight", "Routing scale weight",
        "Services weight", "Crypto weight",
    )),
    ("Workload signals", (
        "BGP present", "NAT present", "Crypto present", "QoS present",
        "L3 > access", "BGP and tunnels", "BGP and VRF", "NAT and crypto",
        "Access count",
    )),
    ("Hardware-fit features", (
        "port-channel support", "management port", "LAN deployment",
    )),
)


def _classify_breakdown_row(factor):
    for label, keywords in _BREAKDOWN_GROUPS:
        for kw in keywords:
            if kw in factor:
                return label
    return "Other"


def _group_breakdown(breakdown):
    """Bin each (factor, impact) row into a category, summing impacts and
    keeping a couple of contributing examples for the table cell.

    Returns a list of dicts in display order: each `{label, impact, members}`.
    """
    bins = {label: {"impact": 0.0, "members": []} for label, _ in _BREAKDOWN_GROUPS}
    bins["Other"] = {"impact": 0.0, "members": []}

    for factor, impact in breakdown:
        label = _classify_breakdown_row(factor)
        bins[label]["impact"] += impact
        bins[label]["members"].append((factor, impact))

    out = []
    order = [label for label, _ in _BREAKDOWN_GROUPS] + ["Other"]
    for label in order:
        b = bins[label]
        if not b["members"]:
            continue
        # Show the two largest-magnitude contributors.
        sorted_members = sorted(b["members"], key=lambda x: -abs(x[1]))[:2]
        out.append({
            "label": label,
            "impact": round(b["impact"], 2),
            "examples": sorted_members,
        })
    return out


# ---------------------------------------------------------------------------
# Ranking + fitness scoring
# ---------------------------------------------------------------------------

def rank_assessment(assessment):
    summary = assessment.get("assessment_summary", {})
    counts = summary.get("finding_counts", {})
    return (
        RECOMMENDATION_RANK.get(summary.get("overall_recommendation", "UNKNOWN"), 4),
        counts.get("critical", 0),
        counts.get("high", 0),
        counts.get("medium", 0),
        counts.get("low", 0),
        summary.get("total_risk_score", 999999),
    )


def compute_platform_fitness(analysis, profile, assessment):
    """Score a platform's fit for the source device's workload.

    Starts at 1000, then applies additive penalties (severe capacity misses,
    unsupported features, scale headroom exceeded) and bonuses (role alignment,
    workload match, adequate headroom). Returns `(score, breakdown)` where
    breakdown is a list of `(reason, delta)` tuples suitable for display.
    """
    summary = assessment.get("assessment_summary", {})
    counts = summary.get("finding_counts", {})
    findings = assessment.get("findings", [])
    fit = profile.get("fit_preferences", {})
    scale = profile.get("scale", {})
    constraints = profile.get("constraints", {})
    capabilities = profile.get("capabilities", {})

    fitness = 1000.0
    breakdown = []

    def adjust(reason, delta):
        nonlocal fitness
        fitness += delta
        breakdown.append((reason, delta))

    # Base penalty from the overall recommendation.
    overall = summary.get("overall_recommendation", "UNKNOWN")
    adjust(f"Overall recommendation: {overall}", -{
        "LIKELY_FIT": 0,
        "CONDITIONAL_FIT": 80,
        "HIGH_RISK": 180,
        "NOT_RECOMMENDED": 1000,
        "UNKNOWN": 300,
    }.get(overall, 300))

    # Severity-weighted finding counts.
    for severity, weight in (("critical", 250), ("high", 80),
                             ("medium", 25), ("low", 5)):
        n = counts.get(severity, 0)
        if n:
            adjust(f"{severity.capitalize()} findings: {n}", -n * weight)

    adjust(
        f"Total risk score: {summary.get('total_risk_score', 0)}",
        -summary.get("total_risk_score", 0) * 1.5,
    )

    # Role / preference bonuses.
    intended_role = str(constraints.get("intended_role", "")).lower()
    if intended_role == "wan_edge":
        adjust("Intended role is WAN edge", 80)

    role_alignment = fit.get("role_alignment", "")
    role_weight = fit.get("role_weight", 0)
    wan_edge_roles = {
        "compact_wan_edge", "mid_scale_wan_edge",
        "performance_wan_edge", "high_scale_wan_edge",
    }
    if role_alignment in wan_edge_roles:
        adjust(f"Role alignment ({role_alignment})", role_weight * 10)

    adjust("Throughput weight", fit.get("throughput_weight", 0) * 8)
    adjust("Routing scale weight", fit.get("routing_scale_weight", 0) * 10)
    adjust("Services weight", fit.get("services_weight", 0) * 8)
    adjust("Crypto weight", fit.get("crypto_weight", 0) * 8)

    # Observed workload extraction.
    bgp_neighbors = _get(analysis, ["routing", "bgp", "neighbor_count"], 0) or 0
    static_routes = _get(analysis, ["routing", "static_route_count"], 0) or 0
    vrf_count = len(_get(analysis, ["routing", "vrfs"], []) or [])
    tunnel_count = _get(analysis, ["interfaces", "tunnels"], 0) or 0
    l3_count = _get(analysis, ["interfaces", "layer3_count"], 0) or 0
    trunk_count = _get(analysis, ["interfaces", "layer2_trunk_count"], 0) or 0
    access_count = _get(analysis, ["interfaces", "layer2_access_count"], 0) or 0
    qos_present = bool(_get(analysis, ["policy", "qos_present"], False))
    nat_present = bool(_get(analysis, ["services", "nat_present"], False))
    crypto_present = bool(_get(analysis, ["crypto_vpn", "crypto_present"], False))
    bgp_present = bool(_get(analysis, ["routing", "protocols", "bgp"], False))

    active_physical_count = _get(analysis, ["interfaces", "active_physical_count"], 0) or 0
    active_physical_by_type = _get(analysis, ["interfaces", "active_physical_by_type"], {}) or {}
    active_subinterfaces = _get(analysis, ["interfaces", "active_subinterfaces"], 0) or 0
    active_physical_by_speed_class = _get(analysis, ["interfaces", "active_physical_by_speed_class"], {}) or {}
    active_physical_by_role = _get(analysis, ["interfaces", "active_physical_by_role"], {}) or {}
    active_management_count = _get(analysis, ["interfaces", "active_management_interfaces"], 0) or 0
    active_wan_count = _get(analysis, ["interfaces", "active_wan_physical_count"], 0) or 0
    active_lan_count = _get(analysis, ["interfaces", "active_lan_physical_count"], 0) or 0
    active_uplink_count = _get(analysis, ["interfaces", "active_uplink_physical_count"], 0) or 0
    active_port_channel_member_count = _get(analysis, ["interfaces", "active_port_channel_member_count"], 0) or 0

    max_physical_interfaces = scale.get("max_physical_interfaces") or scale.get("max_interfaces", 0)
    max_subinterfaces = scale.get("max_subinterfaces", 0)
    supports_subinterfaces = bool(capabilities.get("supports_subinterfaces", False))
    supported_interface_types = set(capabilities.get("supported_interface_types", []))

    ports_config = scale.get("ports", {})
    target_native_supply = ports_config.get("native", {})
    target_breakout_config = ports_config.get("breakout", {})
    target_reserved = ports_config.get("reserved_or_dedicated", {})

    # Physical-interface capacity check.
    if active_physical_count > 0:
        if active_physical_count > max_physical_interfaces:
            adjust("Physical interface capacity exceeded", -300)
        elif max_physical_interfaces and (active_physical_count / max_physical_interfaces) > 0.90:
            adjust("Physical interface usage > 90%", -100)

    # Subinterface capacity check.
    if active_subinterfaces > 0:
        if not supports_subinterfaces:
            adjust("Target does not support subinterfaces", -400)
        elif active_subinterfaces > max_subinterfaces:
            adjust("Subinterface capacity exceeded", -250)
        elif max_subinterfaces and (active_subinterfaces / max_subinterfaces) > 0.80:
            adjust("Subinterface usage > 80%", -80)

    # Interface-type compatibility check.
    unsupported_types = [t for t in active_physical_by_type if t not in supported_interface_types]
    if unsupported_types:
        adjust(f"Unsupported interface types: {unsupported_types}", -len(unsupported_types) * 200)

    # Scale headroom for BGP, static routes, VRFs, tunnels, L3/trunks.
    headroom_checks = [
        (bgp_neighbors, scale.get("max_bgp_neighbors"), 120, "BGP neighbors"),
        (static_routes, scale.get("max_static_routes"), 80, "Static routes"),
        (vrf_count, scale.get("max_vrfs"), 100, "VRFs"),
        (tunnel_count, scale.get("max_tunnels"), 120, "Tunnels"),
        (l3_count, scale.get("max_l3_interfaces"), 80, "L3 interfaces"),
        (trunk_count, scale.get("max_trunk_ports"), 40, "Trunk ports"),
    ]
    for current, maximum, weight, label in headroom_checks:
        ratio = _compute_headroom_ratio(current, maximum)
        if ratio is None:
            continue
        if ratio > 1.0:
            adjust(f"{label} headroom exceeded ({current}/{maximum})", -weight * 3)
        elif ratio > 0.85:
            adjust(f"{label} headroom >85% ({current}/{maximum})", -weight * 1.5)
        elif ratio > 0.70:
            adjust(f"{label} headroom >70% ({current}/{maximum})", -weight * 0.75)
        elif ratio < 0.25:
            adjust(f"{label} headroom <25% ({current}/{maximum})", weight * 0.10)
        else:
            adjust(f"{label} headroom normal ({current}/{maximum})", weight * 0.30)

    # Workload-oriented bonuses.
    if bgp_present:
        adjust("BGP present", fit.get("routing_scale_weight", 0) * 6)
    if nat_present:
        adjust("NAT present", fit.get("services_weight", 0) * 5)
    if crypto_present:
        adjust("Crypto present", fit.get("crypto_weight", 0) * 5)
    if qos_present:
        adjust("QoS present", fit.get("services_weight", 0) * 3)

    # Branch-bias calibration: small platforms penalized for heavy workloads,
    # rewarded for genuinely compact ones.
    if fit.get("branch_bias") == "high":
        if bgp_neighbors > 100:
            adjust("Branch bias: bgp_neighbors > 100", -80)
        if tunnel_count > 100:
            adjust("Branch bias: tunnel_count > 100", -80)
        if vrf_count > 16:
            adjust("Branch bias: vrf_count > 16", -60)
        if static_routes > 2000:
            adjust("Branch bias: static_routes > 2000", -60)
        if (bgp_neighbors < 50 and tunnel_count < 50
                and vrf_count < 8 and static_routes < 1000):
            adjust("Branch bias: compact workload match", 60)

    # Large-platform overkill penalty for very small deployments.
    if role_alignment == "high_scale_wan_edge":
        if (bgp_neighbors < 20 and tunnel_count < 20
                and vrf_count < 4 and static_routes < 500 and l3_count < 12):
            adjust("High scale WAN edge but small workload", -30)

    # Switching-heavy penalty for WAN-edge candidates.
    if access_count > 12:
        adjust("Access count > 12", -50)
    if access_count > 24:
        adjust("Access count > 24", -70)
    if access_count > 48:
        adjust("Access count > 48", -100)

    # Routed-workload bonuses.
    if l3_count > access_count:
        adjust("L3 > access count", 40)
    if bgp_present and tunnel_count > 0:
        adjust("BGP and tunnels present", 40)
    if bgp_present and vrf_count > 0:
        adjust("BGP and VRF present", 50)
    if nat_present and crypto_present:
        adjust("NAT and crypto present", 30)

    # Assessor-flagged unsupported-type findings (duplicate channel to reinforce).
    unsupported_findings = [
        f for f in findings
        if "unsupported interface types" in f.get("title", "").lower()
    ]
    if unsupported_findings:
        adjust("Unsupported interface type findings", -len(unsupported_findings) * 200)

    # Speed-class allocation against native supply.
    alloc_result = {
        "allocation_ok": True,
        "unmet_demand": {},
        "allocation_detail": {},
        "breakout_used": {},
        "remaining_supply_by_speed": {},
    }
    if active_physical_by_speed_class:
        alloc_result = _allocate_speed_capacity(
            source_demand_by_speed=active_physical_by_speed_class,
            target_native_supply=target_native_supply,
            target_breakout=target_breakout_config,
        )
        if not alloc_result["allocation_ok"]:
            unmet_high = sum(v for k, v in alloc_result["unmet_demand"].items()
                             if k in ("40G", "100G"))
            unmet_low = sum(alloc_result["unmet_demand"].values()) - unmet_high
            if unmet_high:
                adjust("Unmet high-speed demand", -unmet_high * 150)
            if unmet_low:
                adjust("Unmet mid/low-speed demand", -unmet_low * 60)
        else:
            adjust("Speed matching successful", 30)

        total_native = sum(target_native_supply.values()) or 0
        total_demand = sum(active_physical_by_speed_class.values()) or 0
        if total_native and total_demand:
            supply_ratio = total_native / total_demand
            if supply_ratio > 1.5:
                adjust("Supply ratio > 1.5", 25)
            elif supply_ratio > 1.2:
                adjust("Supply ratio > 1.2", 10)

    # Port-role checks.
    management_port_capacity = target_reserved.get("management", 0)
    if active_management_count > 0 and management_port_capacity == 0:
        adjust("No dedicated management port", -60)
    elif active_management_count > 0 and management_port_capacity > 0:
        adjust("Dedicated management port available", 25)

    if "wan_edge" in role_alignment.lower() and active_wan_count > 0:
        adjust("WAN edge role and WAN interfaces present", 15)

    if (active_uplink_count > 0 or active_port_channel_member_count > 0) \
            and not bool(capabilities.get("supports_port_channel", False)):
        adjust("No port-channel support for uplinks", -80)

    if active_lan_count > 0 and fit.get("branch_bias") == "low":
        adjust("Large LAN deployment on non-branch platform", -10)

    # Structured interface-comparison metadata attached to the result.
    interface_comparison = {
        "physical_capacity_ok": active_physical_count <= max_physical_interfaces,
        "subinterface_capacity_ok": (
            (not active_subinterfaces)
            or (supports_subinterfaces and active_subinterfaces <= max_subinterfaces)
        ),
        "speed_match_ok": alloc_result["allocation_ok"],
        "management_fit": management_port_capacity > 0 or active_management_count == 0,
        "role_fit_summary": {
            "management_supported": management_port_capacity > 0,
            "wan_capable": "wan_edge" in role_alignment.lower(),
            "port_channel_supported": bool(capabilities.get("supports_port_channel", False)),
            "source_active_by_role": active_physical_by_role,
        },
        "unmet_speed_demand": alloc_result.get("unmet_demand", {}),
        "source_active_physical_by_speed_class": active_physical_by_speed_class,
        "target_native_supply_by_speed_class": target_native_supply,
        "target_breakout_config": target_breakout_config,
        "allocation_summary": alloc_result.get("allocation_detail", {}),
    }

    return round(fitness, 2), breakdown, interface_comparison


# ---------------------------------------------------------------------------
# Comparison orchestration
# ---------------------------------------------------------------------------

def compare_platforms(analysis, target_profiles):
    results = []

    for profile in target_profiles:
        assessment = assess_refresh(analysis, profile)
        fitness_score, breakdown, interface_comparison = compute_platform_fitness(
            analysis, profile, assessment
        )

        source_demand = _get(analysis, ["interfaces", "active_physical_by_speed_class"], {}) or {}
        scale = profile.get("scale", {})
        native_supply = scale.get("ports", {}).get("native", {}) or {}
        breakout = scale.get("ports", {}).get("breakout", {}) or {}
        allocation = _allocate_speed_capacity(source_demand, native_supply, breakout)

        results.append({
            "platform_name": profile.get("platform_name", "UNKNOWN_PLATFORM"),
            "source_file": profile.get("_source_file", "UNKNOWN_FILE"),
            "fitness_score": fitness_score,
            "score_breakdown": breakdown,
            "assessment": assessment,
            "source_demand": source_demand,
            "native_supply": native_supply,
            "breakout": breakout,
            "allocation_detail": allocation.get("allocation_detail", {}),
            "unmet_demand": allocation.get("unmet_demand", {}),
            "breakout_used": allocation.get("breakout_used", {}),
            "allocation_ok": allocation.get("allocation_ok", False),
            "interface_comparison": interface_comparison,
        })

    results.sort(key=lambda r: (-r["fitness_score"], rank_assessment(r["assessment"])))

    recommended = next(
        (
            r["platform_name"] for r in results
            if r["assessment"].get("assessment_summary", {}).get("overall_recommendation")
            in ("LIKELY_FIT", "CONDITIONAL_FIT")
        ),
        None,
    )

    return {
        "metadata": _build_metadata(analysis),
        "device_hostname": analysis.get("summary", {}).get("hostname", "UNKNOWN"),
        "platform_count": len(results),
        "top_ranked_platform": results[0]["platform_name"] if results else None,
        "recommended_platform": recommended,
        "best_fit_platform": recommended or (results[0]["platform_name"] if results else None),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _device_context_lines_md(analysis):
    """Render the one-shot source-device context that's shared across all
    platform candidates.

    Routing / NAT / IPsec scale numbers land here so the reader sees the
    current workload on the source device before any target comparison. When
    a NetBrain runtime harvest is merged (`analysis['runtime']`), live
    translation counts and active-SA counts are surfaced alongside the
    config-side presence flags.
    """
    summary = analysis.get("summary", {})
    interfaces = analysis.get("interfaces", {})
    routing = analysis.get("routing", {})
    security = analysis.get("security", {})
    services = analysis.get("services", {})
    policy = analysis.get("policy", {})
    crypto = analysis.get("crypto_vpn", {})
    ha = analysis.get("high_availability", {})
    runtime = analysis.get("runtime", {}) or {}
    runtime_nat = runtime.get("nat", {}) or {}
    runtime_crypto = runtime.get("crypto", {}) or {}

    lines = ["## Source Device Context", ""]
    lines.append(f"- **Hostname:** {summary.get('hostname', 'UNKNOWN')}")
    lines.append(f"- **Total interfaces:** {interfaces.get('total', 0)}")
    lines.append(f"- **Active physical:** {interfaces.get('active_physical_count', 0)}")
    by_type = interfaces.get("active_physical_by_type", {})
    if by_type:
        lines.append(
            "- **Active physical by type:** "
            + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
        )
    by_speed = interfaces.get("active_physical_by_speed_class", {})
    if by_speed:
        lines.append(
            "- **Active physical by speed:** "
            + ", ".join(f"{k}={by_speed[k]}" for k in _sort_speeds(by_speed.keys()))
        )
    inference = summary.get("speed_class_inference") or {}
    if inference:
        parts_inf = []
        for src, count in (
            ("by_interface_type", inference.get("by_interface_type", 0)),
            ("by_transceiver", inference.get("by_transceiver", 0)),
            ("by_operational", inference.get("by_operational", 0)),
        ):
            if count:
                parts_inf.append(f"{count} from {src.removeprefix('by_')}")
        if parts_inf:
            lines.append("- **Speed-class inference:** " + ", ".join(parts_inf))
            refined = inference.get("by_transceiver", 0) + inference.get("by_operational", 0)
            if refined > 0:
                lines.append(
                    f"    - _{refined} interface(s) had their speed class refined "
                    f"from runtime data (e.g., a 10G slot running a 1G optic). "
                    f"See per-interface `effective_speed_class_source` in "
                    f"`analysis_report.json`._"
                )
            elif inference.get("by_interface_type", 0) > 0:
                lines.append(
                    "    - _All speed classes inferred from interface type names. "
                    "Provide `show interfaces` or `show interfaces transceiver` "
                    "harvest output to refine demand using actual transceiver / "
                    "operational speed and avoid over- or under-stating port-mix needs._"
                )
    lines.append(f"- **Active subinterfaces:** {interfaces.get('active_subinterfaces', 0)}")
    lines.append(f"- **Active tunnels:** {interfaces.get('active_tunnels', 0)}")
    lines.append(f"- **Active port-channels:** {interfaces.get('active_port_channels', 0)}")

    protocols = summary.get("routing_protocols_enabled", []) or []
    lines.append("")
    lines.append("### Routing scale")
    lines.append(f"- **Routing protocols:** {', '.join(protocols) if protocols else 'None'}")
    lines.append(f"- **BGP neighbors:** {_get(routing, ['bgp', 'neighbor_count'], 0)}")
    lines.append(f"- **VRFs:** {len(routing.get('vrfs', []) or [])}")
    lines.append(f"- **Static routes:** {routing.get('static_route_count', 0)}")
    ospf_procs = _get(routing, ["ospf", "processes"], []) or []
    eigrp_procs = _get(routing, ["eigrp", "processes"], []) or []
    if ospf_procs or eigrp_procs:
        lines.append(f"- **OSPF processes:** {len(ospf_procs)}")
        lines.append(f"- **EIGRP processes:** {len(eigrp_procs)}")
        lines.append(
            "    - _OSPF / EIGRP neighbor counts depend on runtime harvest "
            "(see GH #22). Without `show ip ospf neighbor` / `show ip eigrp "
            "neighbors` output, only process counts are visible here._"
        )
    runtime_routes = runtime.get("route_table", {}) or {}
    if runtime_routes:
        total_routes = runtime_routes.get("ipv4_total")
        if total_routes is not None:
            lines.append(f"- **Live IPv4 routes (runtime):** {total_routes}")
        by_proto = runtime_routes.get("ipv4_by_protocol", {}) or {}
        if by_proto:
            lines.append(
                "    - _By protocol:_ "
                + ", ".join(f"{k}={v}" for k, v in sorted(by_proto.items()))
            )

    lines.append("")
    lines.append("### NAT scale")
    if services.get("nat_present"):
        lines.append(f"- **NAT configured:** Yes ({services.get('nat_line_count', 0)} config lines)")
    else:
        lines.append("- **NAT configured:** No")
    if runtime_nat:
        active = runtime_nat.get("active_translations")
        peak = runtime_nat.get("peak_translations")
        if active is not None or peak is not None:
            active_s = str(active) if active is not None else "unknown"
            peak_s = str(peak) if peak is not None else "unknown"
            lines.append(
                f"- **Translations (runtime):** active={active_s}, peak={peak_s}"
            )
        hits = runtime_nat.get("hits")
        misses = runtime_nat.get("misses")
        if hits is not None or misses is not None:
            hits_s = str(hits) if hits is not None else "unknown"
            misses_s = str(misses) if misses is not None else "unknown"
            lines.append(f"- **NAT hits/misses (runtime):** {hits_s} / {misses_s}")
    elif services.get("nat_present"):
        lines.append(
            "    - _Runtime translation counts not available. Provide a "
            "`show ip nat statistics` harvest to surface active / peak "
            "translation counts for ceiling comparison._"
        )

    lines.append("")
    lines.append("### IPsec / VPN scale")
    if crypto.get("crypto_present"):
        parts_c = []
        if crypto.get("isakmp_present"):
            parts_c.append("ISAKMP")
        if crypto.get("ikev2_present"):
            parts_c.append("IKEv2")
        if crypto.get("ipsec_present"):
            parts_c.append("IPsec")
        if crypto.get("tunnel_interfaces_present"):
            parts_c.append("tunnel-if")
        mode = "/".join(parts_c) if parts_c else "crypto (unspecified)"
        lines.append(
            f"- **Crypto configured:** Yes — {mode} "
            f"({crypto.get('crypto_line_count', 0)} config lines)"
        )
    else:
        lines.append("- **Crypto configured:** No")
    lines.append(f"- **Tunnel interfaces (config):** {interfaces.get('active_tunnels', 0)}")
    if runtime_crypto:
        active_sas = runtime_crypto.get("active_sas")
        total_sas = runtime_crypto.get("total_sas")
        if active_sas is not None or total_sas is not None:
            active_s = str(active_sas) if active_sas is not None else "unknown"
            total_s = str(total_sas) if total_sas is not None else "unknown"
            lines.append(
                f"- **IPsec SAs (runtime):** active={active_s}, total={total_s}"
            )
    elif crypto.get("crypto_present"):
        lines.append(
            "    - _Runtime IPsec SA counts not available. Provide a "
            "`show crypto ipsec sa count` harvest to surface active / total "
            "SA counts for ceiling comparison._"
        )
    lines.append("")

    feature_flags = [
        ("VRF", summary.get("vrf_present")),
        ("IPv6", summary.get("ipv6_present")),
        ("FHRP", summary.get("fhrp_present")),
        ("QoS", policy.get("qos_present")),
        ("NAT", services.get("nat_present")),
        ("Crypto/VPN", crypto.get("crypto_present")),
        ("AAA", security.get("aaa_present")),
    ]
    lines.append("- **Features present:** "
                 + ", ".join(f"{label}={_bool_str(flag)}" for label, flag in feature_flags))

    risks = analysis.get("refresh_risks", []) or []
    if risks:
        lines.append("")
        lines.append("### Analyzer-identified Refresh Risks")
        for r in risks:
            lines.append(f"- {r}")

    considerations = analysis.get("migration_considerations", []) or []
    if considerations:
        lines.append("")
        lines.append("### Analyzer-identified Migration Considerations")
        for c in considerations:
            lines.append(f"- {c}")

    return lines


def _ranked_table_md(results):
    """Ranked candidates table. Verdict and finding counts — no fitness
    column; scoring is relegated to the bottom appendix."""
    lines = [
        "| Rank | Platform | Verdict | Risk | Critical | High | Medium | Low |",
        "|------|----------|---------|------|----------|------|--------|-----|",
    ]
    for idx, r in enumerate(results, start=1):
        summary = r["assessment"].get("assessment_summary", {})
        counts = summary.get("finding_counts", {})
        lines.append(
            f"| {idx} | {r['platform_name']} | "
            f"{summary.get('overall_recommendation', 'UNKNOWN')} | "
            f"{summary.get('total_risk_score', 0)} | "
            f"{counts.get('critical', 0)} | {counts.get('high', 0)} | "
            f"{counts.get('medium', 0)} | {counts.get('low', 0)} |"
        )
    return lines


def _allocation_block_md(result):
    """Demand-vs-capacity table + allocation outcome bullets. Reused for
    best-fit detail. Returns a list of lines (possibly empty)."""
    src_demand = result.get("source_demand", {}) or {}
    native_supply = result.get("native_supply", {}) or {}
    alloc_detail = result.get("allocation_detail", {}) or {}
    all_speeds = set(src_demand) | set(native_supply) | set(alloc_detail)

    lines = []
    if all_speeds:
        lines.append("### Demand vs capacity (by speed class)")
        lines.append("")
        lines.append(
            "| Speed | Source demand | Native supply | Matched native | "
            "Matched upward | Matched via breakout | Unmet |"
        )
        lines.append(
            "|-------|---------------|---------------|----------------|"
            "----------------|----------------------|-------|"
        )
        for speed in _sort_speeds(all_speeds):
            ad = alloc_detail.get(speed, {})
            lines.append(
                f"| {speed} | {src_demand.get(speed, 0)} | "
                f"{native_supply.get(speed, 0)} | "
                f"{ad.get('matched_native', 0)} | "
                f"{ad.get('matched_native_upward', 0)} | "
                f"{ad.get('matched_breakout_fanout', 0)} | "
                f"{ad.get('unmet', 0)} |"
            )
        lines.append("")
        lines.append(
            "_**Matched native** = demand absorbed by same-speed-class ports. "
            "**Matched upward** = a higher-speed native port absorbs lower-speed "
            "demand at 1:1 (e.g. a 10G port serves a 1G demand). "
            "**Matched via breakout** = a higher-speed port is fanned out into N "
            "child ports of the dest speed (e.g. one 40G port → 4× 10G via a "
            "`40G_to_4x10G` slot). **Unmet** = remaining demand with no native, "
            "upward, or breakout capacity._"
        )
        lines.append("")

    lines.append("### Allocation outcome")
    lines.append("")
    lines.append(f"- **Status:** {'PASS' if result.get('allocation_ok') else 'FAIL'}")
    unmet = result.get("unmet_demand", {}) or {}
    lines.append(
        "- **Unmet demand:** "
        + (", ".join(f"{k}={v}" for k, v in unmet.items()) if unmet else "None")
    )
    breakout = result.get("breakout", {}) or {}
    lines.append(
        "- **Breakout available:** "
        + (", ".join(f"{k}={v}" for k, v in breakout.items()) if breakout else "None")
    )
    breakout_used = result.get("breakout_used", {}) or {}
    if breakout_used:
        consumed_summaries = []
        for key, parents in breakout_used.items():
            parsed = _parse_breakout_key(key)
            if parsed is None:
                consumed_summaries.append(f"{key}={parents}")
                continue
            _, count, dest = parsed
            consumed_summaries.append(
                f"{key}={parents} → {parents * count}×{dest} ports yielded"
            )
        lines.append("- **Breakout consumed:** " + ", ".join(consumed_summaries))
    else:
        lines.append("- **Breakout consumed:** None")
    return lines


def _best_fit_detail_md(result, analysis):
    """Best-fit narrative: platform notes, allocation, migration path, and
    pre-cutover checklist. Scoring lives in the methodology appendix — this
    section is for the person deciding and planning the refresh."""
    lines = [f"## Best-Fit Detail: {result['platform_name']}", ""]

    platform_notes = result["assessment"].get("platform_notes", []) or []
    if platform_notes:
        lines.append("### About this platform")
        lines.append("")
        for note in platform_notes:
            lines.append(f"- {note}")
        lines.append("")

    lines.extend(_allocation_block_md(result))
    lines.append("")

    actionable = _actionable_findings(
        result["assessment"].get("findings", []) or []
    )
    if actionable:
        lines.append("### Migration path")
        lines.append("")
        lines.append("Findings that need to be addressed before or during cutover:")
        lines.append("")
        for f in sorted(
            actionable,
            key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}
                .get(x.get("severity"), 4),
        ):
            lines.append(
                f"- {_sev_badge_md(f.get('severity', 'info'))} "
                f"**{f.get('title', 'Untitled')}**"
            )
            if f.get("detail"):
                lines.append(f"    - _Detail:_ {f['detail']}")
            if f.get("recommendation"):
                lines.append(f"    - _Action:_ {f['recommendation']}")
        lines.append("")

    checklist = _validation_checklist_items(result, analysis)
    if checklist:
        lines.append("### Pre-cutover validation checklist")
        lines.append("")
        for item in checklist:
            lines.append(f"- [ ] {item}")
        lines.append("")

    return lines


def _other_candidate_md(idx, result):
    """Compact entry for non-best-fit candidates. Verdict + headline findings
    only; detailed breakdown is in the scoring methodology appendix."""
    assessment = result.get("assessment", {})
    summary = assessment.get("assessment_summary", {})
    rec = summary.get("overall_recommendation", "UNKNOWN")

    lines = [f"### {idx}. {result['platform_name']} — {rec}", ""]

    findings = assessment.get("findings", []) or []
    high_sev = [f for f in findings if f.get("severity") in ("critical", "high")]
    med_sev = [f for f in findings if f.get("severity") == "medium"]

    if high_sev:
        lines.append("**Blocking findings:**")
        lines.append("")
        for f in high_sev:
            lines.append(
                f"- {_sev_badge_md(f.get('severity', 'info'))} "
                f"{f.get('title', 'Untitled')}"
            )
        lines.append("")
    if med_sev:
        lines.append("**Material findings:**")
        lines.append("")
        for f in med_sev:
            lines.append(
                f"- {_sev_badge_md(f.get('severity', 'info'))} "
                f"{f.get('title', 'Untitled')}"
            )
        lines.append("")
    if not (high_sev or med_sev):
        lines.append("_No blocking or material findings._")
        lines.append("")

    unmet = result.get("unmet_demand", {}) or {}
    if unmet:
        lines.append(
            "**Unmet port demand:** "
            + ", ".join(f"{v}× {k}" for k, v in unmet.items())
        )
        lines.append("")

    return lines


def _scoring_appendix_md(results):
    """Scoring methodology appendix — per-platform fitness + grouped
    breakdown. Pushed to the bottom so the verdict-first narrative leads;
    developers / auditors still get the full signal."""
    lines = [
        "## Scoring Methodology (Appendix)",
        "",
        "Fitness starts at 1000 and is adjusted by overall recommendation, "
        "severity-weighted finding counts, total risk score, interface capacity "
        "/ speed allocation checks, scale-headroom utilization, and role-alignment "
        "preferences. The **Recommended platform** is the highest-ranked candidate "
        "whose verdict is `LIKELY_FIT` or `CONDITIONAL_FIT`.",
        "",
        "**Users should read the verdict and findings above — this appendix exists "
        "for audit, tie-breaker, and calibration purposes only.**",
        "",
        "| Rank | Platform | Fitness |",
        "|------|----------|---------|",
    ]
    for idx, r in enumerate(results, start=1):
        lines.append(f"| {idx} | {r['platform_name']} | {r['fitness_score']} |")
    lines.append("")

    for idx, result in enumerate(results, start=1):
        breakdown = result.get("score_breakdown", []) or []
        if not breakdown:
            continue
        lines.append(f"### {idx}. {result['platform_name']}")
        lines.append("")
        penalties = sorted(
            (s for s in breakdown if s[1] < 0), key=lambda x: x[1]
        )[:3]
        bonuses = sorted(
            (s for s in breakdown if s[1] > 0), key=lambda x: -x[1]
        )[:3]
        if penalties:
            lines.append(
                "- **Top penalties:** "
                + ", ".join(f"{p[0]} ({p[1]:+g})" for p in penalties)
            )
        if bonuses:
            lines.append(
                "- **Top bonuses:** "
                + ", ".join(f"{b[0]} ({b[1]:+g})" for b in bonuses)
            )
        groups = _group_breakdown(breakdown)
        if groups:
            lines.append("")
            lines.append("| Driver | Net impact | Largest contributors |")
            lines.append("|--------|-----------:|----------------------|")
            for g in groups:
                examples = "; ".join(
                    f"{name} ({val:+g})" for name, val in g["examples"]
                )
                lines.append(
                    f"| {g['label']} | {g['impact']:+g} | {examples} |"
                )
        lines.append("")

    lines.append(
        "_Full per-row breakdown for every platform is available in "
        "`platform_comparison.json` under each result's `score_breakdown` field._"
    )
    return lines


def build_report_markdown(comparison, analysis):
    """Produce the unified hardware-refresh report as Markdown.

    Layout:
      1. Verdict (best-fit, recommendation, allocation status)
      2. Source device context (inventory, routing / NAT / IPsec scale)
      3. Ranked candidates (verdict + finding counts, no fitness column)
      4. Best-fit detail (platform notes, allocation, migration path, checklist)
      5. Other candidates (verdict + headline findings, compact)
      6. Scoring methodology appendix (fitness + grouped breakdown, audit-only)
    """
    hostname = comparison.get("device_hostname", "UNKNOWN")
    results = comparison.get("results", [])
    metadata = comparison.get("metadata", {}) or _build_metadata(analysis)
    best_fit_name = comparison.get("best_fit_platform")
    recommended = comparison.get("recommended_platform")
    best_fit_result = _find_best_fit_result(comparison)

    lines = [f"# Hardware Refresh Report — `{hostname}`", ""]
    lines.append(
        f"_Generated {metadata.get('generated_at', 'UNKNOWN')} by netfit "
        f"{metadata.get('netfit_version', 'UNKNOWN')}._"
    )
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if best_fit_result:
        summary = best_fit_result["assessment"].get("assessment_summary", {})
        rec = summary.get("overall_recommendation", "UNKNOWN")
        alloc_ok = best_fit_result.get("allocation_ok", True)
        is_recommended = recommended == best_fit_name
        lines.append(f"- **Best-fit platform:** **{best_fit_name}**")
        lines.append(f"- **Overall recommendation:** {rec}")
        lines.append(
            "- **Port-allocation status:** "
            + ("PASS" if alloc_ok else "FAIL — see Migration Path")
        )
        lines.append(
            f"- **Source profile:** `{best_fit_result.get('source_file', '')}`"
        )
        lines.append(
            f"- **Platforms compared:** {comparison.get('platform_count', 0)}"
        )
        if not is_recommended:
            lines.append("")
            lines.append(
                "> **Caveat:** this platform is the top-ranked candidate but has "
                "**not** earned `LIKELY_FIT` or `CONDITIONAL_FIT`. Treat as the "
                "best available option, not an endorsement. See Migration Path "
                "and Ranked Candidates below for the disposition."
            )
    else:
        lines.append("- _No candidate platform was scored._")
    lines.append("")

    lines.extend(_device_context_lines_md(analysis))
    lines.append("")

    if results:
        lines.append("## Ranked Candidates")
        lines.append("")
        lines.extend(_ranked_table_md(results))
        lines.append("")

    if best_fit_result:
        lines.extend(_best_fit_detail_md(best_fit_result, analysis))

    other_results = [
        r for r in results if r["platform_name"] != best_fit_name
    ]
    if other_results:
        lines.append("## Other Candidates")
        lines.append("")
        for idx, r in enumerate(other_results, start=2):
            lines.extend(_other_candidate_md(idx, r))

    if results:
        lines.extend(_scoring_appendix_md(results))

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_HTML_CSS = """
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
       margin: 32px; color: #1f2328; background: #ffffff; line-height: 1.5; }
h1, h2, h3 { color: #0b3d91; }
h1 { border-bottom: 2px solid #0b3d91; padding-bottom: 6px; }
h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 4px; margin-top: 32px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 18px; font-size: 14px; }
th, td { border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; }
tr.best-fit td { background: #e7f3ff; font-weight: 600; }
code { background: #f6f8fa; padding: 1px 4px; border-radius: 3px; font-size: 13px; }
.summary-box { background: #f6f8fa; border-left: 4px solid #0b3d91;
               padding: 12px 16px; margin-bottom: 24px; }
.warning-box { background: #fff8c5; border-left: 4px solid #bf8700;
               padding: 12px 16px; margin-bottom: 24px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
         color: #ffffff; font-size: 12px; font-weight: 600; }
.sev-critical { background: #b31412; }
.sev-high     { background: #cf6b00; }
.sev-medium   { background: #e6b800; color: #1f2328; }
.sev-low      { background: #1a6bce; }
.sev-info     { background: #6e7781; }
.rec { display: inline-block; padding: 2px 8px; border-radius: 10px;
       font-size: 12px; font-weight: 600; }
.rec-fit         { background: #1f883d; color: #ffffff; }
.rec-conditional { background: #bf8700; color: #ffffff; }
.rec-risk        { background: #cf222e; color: #ffffff; }
.rec-nope        { background: #6e7781; color: #ffffff; }
.rec-unknown     { background: #d0d7de; color: #1f2328; }
.finding { border: 1px solid #d0d7de; border-left: 4px solid #0b3d91;
           padding: 10px 14px; margin-bottom: 10px; background: #fafbfc; }
.finding-title { font-weight: 600; margin-bottom: 4px; }
.finding-meta { font-size: 13px; color: #57606a; margin-bottom: 6px; }
details.platform { border: 1px solid #d0d7de; border-radius: 6px;
                   padding: 12px 16px; margin-bottom: 12px; background: #ffffff; }
details.platform[open] { background: #fafbfc; }
details.platform > summary { font-weight: 600; font-size: 18px; cursor: pointer;
                             list-style: none; }
details.platform > summary::-webkit-details-marker { display: none; }
"""


def _esc(v):
    return html.escape(str(v))


def _device_context_html(analysis):
    summary = analysis.get("summary", {})
    interfaces = analysis.get("interfaces", {})
    routing = analysis.get("routing", {})
    security = analysis.get("security", {})
    services = analysis.get("services", {})
    policy = analysis.get("policy", {})
    crypto = analysis.get("crypto_vpn", {})
    runtime = analysis.get("runtime", {}) or {}
    runtime_nat = runtime.get("nat", {}) or {}
    runtime_crypto = runtime.get("crypto", {}) or {}
    runtime_routes = runtime.get("route_table", {}) or {}

    by_type = interfaces.get("active_physical_by_type", {})
    by_speed = interfaces.get("active_physical_by_speed_class", {})

    rows = [
        ("Hostname", _esc(summary.get("hostname", "UNKNOWN"))),
        ("Total interfaces", _esc(interfaces.get("total", 0))),
        ("Active physical", _esc(interfaces.get("active_physical_count", 0))),
        ("Active physical by type",
         _esc(", ".join(f"{k}={v}" for k, v in sorted(by_type.items())) or "—")),
        ("Active physical by speed",
         _esc(", ".join(f"{k}={by_speed[k]}" for k in _sort_speeds(by_speed.keys())) or "—")),
    ]
    inference = summary.get("speed_class_inference") or {}
    if inference:
        parts_inf = []
        for src, count in (
            ("interface_type", inference.get("by_interface_type", 0)),
            ("transceiver", inference.get("by_transceiver", 0)),
            ("operational", inference.get("by_operational", 0)),
        ):
            if count:
                parts_inf.append(f"{count} from {src}")
        if parts_inf:
            inference_value = ", ".join(parts_inf)
            refined = inference.get("by_transceiver", 0) + inference.get("by_operational", 0)
            if refined > 0:
                inference_value += (
                    f"<br><span style='font-size: 12px; color: #57606a;'>"
                    f"<em>{refined} interface(s) had their speed class refined "
                    f"from runtime data (e.g., a 10G slot running a 1G optic). "
                    f"See per-interface <code>effective_speed_class_source</code> "
                    f"in <code>analysis_report.json</code>.</em></span>"
                )
            elif inference.get("by_interface_type", 0) > 0:
                inference_value += (
                    "<br><span style='font-size: 12px; color: #57606a;'>"
                    "<em>All speed classes inferred from interface type names. "
                    "Provide <code>show interfaces</code> or "
                    "<code>show interfaces transceiver</code> harvest output to "
                    "refine demand and avoid over- or under-stating port-mix needs."
                    "</em></span>"
                )
            rows.append(("Speed-class inference", inference_value))
    rows += [
        ("Active subinterfaces", _esc(interfaces.get("active_subinterfaces", 0))),
        ("Active tunnels (config)", _esc(interfaces.get("active_tunnels", 0))),
        ("Active port-channels", _esc(interfaces.get("active_port_channels", 0))),
    ]

    # Routing scale block.
    routing_rows = [
        ("Routing protocols",
         _esc(", ".join(summary.get("routing_protocols_enabled", []) or ["None"]))),
        ("BGP neighbors", _esc(_get(routing, ["bgp", "neighbor_count"], 0))),
        ("VRFs", _esc(len(routing.get("vrfs", []) or []))),
        ("Static routes", _esc(routing.get("static_route_count", 0))),
    ]
    ospf_procs = _get(routing, ["ospf", "processes"], []) or []
    eigrp_procs = _get(routing, ["eigrp", "processes"], []) or []
    if ospf_procs or eigrp_procs:
        routing_rows.append(("OSPF processes", _esc(len(ospf_procs))))
        routing_rows.append(("EIGRP processes", _esc(len(eigrp_procs))))
        routing_rows.append((
            "Neighbor counts",
            "<em>OSPF / EIGRP neighbor counts depend on runtime harvest (see "
            "<a href='https://github.com/rmsumida/netfit/issues/22'>GH #22</a>). "
            "Without <code>show ip ospf neighbor</code> / <code>show ip eigrp "
            "neighbors</code> output, only process counts are visible here.</em>",
        ))
    if runtime_routes:
        total_routes = runtime_routes.get("ipv4_total")
        if total_routes is not None:
            routing_rows.append(("Live IPv4 routes (runtime)", _esc(total_routes)))
        by_proto = runtime_routes.get("ipv4_by_protocol", {}) or {}
        if by_proto:
            routing_rows.append((
                "Live routes by protocol",
                _esc(", ".join(f"{k}={v}" for k, v in sorted(by_proto.items()))),
            ))

    # NAT scale block.
    nat_rows = []
    if services.get("nat_present"):
        nat_rows.append((
            "NAT configured",
            f"Yes ({_esc(services.get('nat_line_count', 0))} config lines)",
        ))
    else:
        nat_rows.append(("NAT configured", "No"))
    if runtime_nat:
        active = runtime_nat.get("active_translations")
        peak = runtime_nat.get("peak_translations")
        if active is not None or peak is not None:
            nat_rows.append((
                "Translations (runtime)",
                f"active={_esc(active if active is not None else 'unknown')}, "
                f"peak={_esc(peak if peak is not None else 'unknown')}",
            ))
        hits = runtime_nat.get("hits")
        misses = runtime_nat.get("misses")
        if hits is not None or misses is not None:
            nat_rows.append((
                "NAT hits/misses (runtime)",
                f"{_esc(hits if hits is not None else 'unknown')} / "
                f"{_esc(misses if misses is not None else 'unknown')}",
            ))
    elif services.get("nat_present"):
        nat_rows.append((
            "Translations (runtime)",
            "<em>Not available. Provide <code>show ip nat statistics</code> "
            "harvest to surface active / peak translation counts.</em>",
        ))

    # IPsec / VPN scale block.
    crypto_rows = []
    if crypto.get("crypto_present"):
        parts_c = []
        if crypto.get("isakmp_present"):
            parts_c.append("ISAKMP")
        if crypto.get("ikev2_present"):
            parts_c.append("IKEv2")
        if crypto.get("ipsec_present"):
            parts_c.append("IPsec")
        if crypto.get("tunnel_interfaces_present"):
            parts_c.append("tunnel-if")
        mode = "/".join(parts_c) if parts_c else "crypto (unspecified)"
        crypto_rows.append((
            "Crypto configured",
            f"Yes — {_esc(mode)} ({_esc(crypto.get('crypto_line_count', 0))} "
            "config lines)",
        ))
    else:
        crypto_rows.append(("Crypto configured", "No"))
    crypto_rows.append((
        "Tunnel interfaces (config)",
        _esc(interfaces.get("active_tunnels", 0)),
    ))
    if runtime_crypto:
        active_sas = runtime_crypto.get("active_sas")
        total_sas = runtime_crypto.get("total_sas")
        if active_sas is not None or total_sas is not None:
            crypto_rows.append((
                "IPsec SAs (runtime)",
                f"active={_esc(active_sas if active_sas is not None else 'unknown')}, "
                f"total={_esc(total_sas if total_sas is not None else 'unknown')}",
            ))
    elif crypto.get("crypto_present"):
        crypto_rows.append((
            "IPsec SAs (runtime)",
            "<em>Not available. Provide <code>show crypto ipsec sa count</code> "
            "harvest to surface active / total SA counts.</em>",
        ))

    feature_flags = [
        ("VRF", summary.get("vrf_present")),
        ("IPv6", summary.get("ipv6_present")),
        ("FHRP", summary.get("fhrp_present")),
        ("QoS", policy.get("qos_present")),
        ("NAT", services.get("nat_present")),
        ("Crypto/VPN", crypto.get("crypto_present")),
        ("AAA", security.get("aaa_present")),
    ]
    feature_row = (
        "Features present",
        _esc(", ".join(f"{label}={_bool_str(flag)}" for label, flag in feature_flags)),
    )

    def _render_subtable(heading, subrows):
        if not subrows:
            return ""
        parts = [
            f"<h3>{_esc(heading)}</h3>",
            "<table>",
            '<tr><th style="width: 220px;">Field</th><th>Value</th></tr>',
        ]
        for label, value in subrows:
            parts.append(f"<tr><td>{_esc(label)}</td><td>{value}</td></tr>")
        parts.append("</table>")
        return "\n".join(parts)

    out = ['<h2>Source Device Context</h2>']
    out.append('<h3>Inventory &amp; interfaces</h3>')
    out.append('<table>')
    out.append('<tr><th style="width: 220px;">Field</th><th>Value</th></tr>')
    for label, value in rows:
        out.append(f"<tr><td>{_esc(label)}</td><td>{value}</td></tr>")
    out.append("</table>")
    out.append(_render_subtable("Routing scale", routing_rows))
    out.append(_render_subtable("NAT scale", nat_rows))
    out.append(_render_subtable("IPsec / VPN scale", crypto_rows))
    out.append(_render_subtable("Features", [feature_row]))

    risks = analysis.get("refresh_risks", []) or []
    considerations = analysis.get("migration_considerations", []) or []
    if risks:
        out.append("<h3>Analyzer-identified Refresh Risks</h3><ul>")
        out.extend(f"<li>{_esc(r)}</li>" for r in risks)
        out.append("</ul>")
    if considerations:
        out.append("<h3>Analyzer-identified Migration Considerations</h3><ul>")
        out.extend(f"<li>{_esc(c)}</li>" for c in considerations)
        out.append("</ul>")
    return "\n".join(out)


def _ranked_table_html(results, best_fit_name):
    """Ranked candidates table (HTML). No fitness column — scoring is in
    the methodology appendix."""
    body = [
        "<h2>Ranked Candidates</h2>",
        "<table>",
        "<tr><th>Rank</th><th>Platform</th><th>Verdict</th>"
        "<th>Risk score</th><th>Critical</th><th>High</th>"
        "<th>Medium</th><th>Low</th></tr>",
    ]
    for idx, r in enumerate(results, start=1):
        summary = r["assessment"].get("assessment_summary", {})
        counts = summary.get("finding_counts", {})
        rec_cell = _recommendation_badge_html(
            summary.get("overall_recommendation", "UNKNOWN")
        )
        is_best = r["platform_name"] == best_fit_name
        row_class = ' class="best-fit"' if is_best else ""
        body.append(
            f"<tr{row_class}>"
            f"<td>{idx}</td>"
            f"<td>{_esc(r['platform_name'])}</td>"
            f"<td>{rec_cell}</td>"
            f"<td>{_esc(summary.get('total_risk_score', 0))}</td>"
            f"<td>{_esc(counts.get('critical', 0))}</td>"
            f"<td>{_esc(counts.get('high', 0))}</td>"
            f"<td>{_esc(counts.get('medium', 0))}</td>"
            f"<td>{_esc(counts.get('low', 0))}</td>"
            "</tr>"
        )
    body.append("</table>")
    return body


def _allocation_block_html(result):
    """Demand-vs-capacity table + allocation outcome (HTML). Returns a list
    of parts (possibly empty for the table if no speed data)."""
    src_demand = result.get("source_demand", {}) or {}
    native_supply = result.get("native_supply", {}) or {}
    alloc_detail = result.get("allocation_detail", {}) or {}
    all_speeds = set(src_demand) | set(native_supply) | set(alloc_detail)

    parts = []
    if all_speeds:
        parts.append("<h3>Demand vs capacity (by speed class)</h3><table>")
        parts.append(
            "<tr><th>Speed</th><th>Source demand</th><th>Native supply</th>"
            "<th>Matched native</th><th>Matched upward</th>"
            "<th>Matched via breakout</th><th>Unmet</th></tr>"
        )
        for speed in _sort_speeds(all_speeds):
            ad = alloc_detail.get(speed, {})
            parts.append(
                f"<tr><td>{_esc(speed)}</td>"
                f"<td>{_esc(src_demand.get(speed, 0))}</td>"
                f"<td>{_esc(native_supply.get(speed, 0))}</td>"
                f"<td>{_esc(ad.get('matched_native', 0))}</td>"
                f"<td>{_esc(ad.get('matched_native_upward', 0))}</td>"
                f"<td>{_esc(ad.get('matched_breakout_fanout', 0))}</td>"
                f"<td>{_esc(ad.get('unmet', 0))}</td></tr>"
            )
        parts.append("</table>")
        parts.append(
            "<p style='font-size: 12px; color: #57606a;'>"
            "<strong>Matched native</strong> = demand absorbed by same-speed-class "
            "ports. <strong>Matched upward</strong> = higher-speed native port "
            "absorbs lower-speed demand at 1:1. <strong>Matched via breakout</strong> "
            "= higher-speed port fanned out into N child ports of the dest speed "
            "(e.g. one 40G → 4× 10G via a <code>40G_to_4x10G</code> slot). "
            "<strong>Unmet</strong> = remaining demand with no native, upward, or "
            "breakout capacity.</p>"
        )

    alloc_ok = result.get("allocation_ok")
    unmet = result.get("unmet_demand", {}) or {}
    breakout = result.get("breakout", {}) or {}
    parts.append("<h3>Allocation outcome</h3>")
    parts.append(
        f"<p><strong>Status:</strong> {'PASS' if alloc_ok else 'FAIL'}</p>"
    )
    if unmet:
        parts.append(
            "<p><strong>Unmet demand:</strong> "
            + _esc(", ".join(f"{k}={v}" for k, v in unmet.items())) + "</p>"
        )
    if breakout:
        parts.append(
            "<p><strong>Breakout available:</strong> "
            + _esc(", ".join(f"{k}={v}" for k, v in breakout.items())) + "</p>"
        )
    breakout_used = result.get("breakout_used", {}) or {}
    if breakout_used:
        consumed_summaries = []
        for key, parents in breakout_used.items():
            parsed = _parse_breakout_key(key)
            if parsed is None:
                consumed_summaries.append(f"{key}={parents}")
                continue
            _, count, dest = parsed
            consumed_summaries.append(
                f"{key}={parents} → {parents * count}×{dest} ports yielded"
            )
        parts.append(
            "<p><strong>Breakout consumed:</strong> "
            + _esc(", ".join(consumed_summaries)) + "</p>"
        )
    return parts


def _best_fit_detail_html(result, analysis):
    parts = [f"<h2>Best-Fit Detail: {_esc(result['platform_name'])}</h2>"]

    platform_notes = result["assessment"].get("platform_notes", []) or []
    if platform_notes:
        parts.append("<h3>About this platform</h3><ul>")
        parts.extend(f"<li>{_esc(note)}</li>" for note in platform_notes)
        parts.append("</ul>")

    parts.extend(_allocation_block_html(result))

    actionable = _actionable_findings(
        result["assessment"].get("findings", []) or []
    )
    if actionable:
        parts.append("<h3>Migration path</h3>")
        parts.append(
            "<p>Findings that need to be addressed before or during cutover:</p>"
        )
        for f in sorted(
            actionable,
            key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3}
                .get(x.get("severity"), 4),
        ):
            parts.append(
                '<div class="finding">'
                f'<div class="finding-title">{_esc(f.get("title", "Untitled"))}</div>'
                f'<div class="finding-meta">'
                f'{_sev_badge_html(f.get("severity", "info"))} '
                f'· Category: {_esc(f.get("category", "unknown"))}</div>'
            )
            if f.get("detail"):
                parts.append(
                    f'<div><strong>Detail:</strong> {_esc(f["detail"])}</div>'
                )
            if f.get("recommendation"):
                parts.append(
                    f'<div><strong>Action:</strong> {_esc(f["recommendation"])}</div>'
                )
            parts.append("</div>")

    checklist = _validation_checklist_items(result, analysis)
    if checklist:
        parts.append("<h3>Pre-cutover validation checklist</h3><ul>")
        for item in checklist:
            parts.append(
                f'<li><input type="checkbox" disabled> {_esc(item)}</li>'
            )
        parts.append("</ul>")

    return parts


def _other_candidate_html(idx, result):
    assessment = result.get("assessment", {})
    summary = assessment.get("assessment_summary", {})
    rec = summary.get("overall_recommendation", "UNKNOWN")

    parts = [
        '<details class="platform">',
        f'<summary>{idx}. {_esc(result["platform_name"])} — '
        f'{_recommendation_badge_html(rec)}</summary>',
    ]

    findings = assessment.get("findings", []) or []
    high_sev = [f for f in findings if f.get("severity") in ("critical", "high")]
    med_sev = [f for f in findings if f.get("severity") == "medium"]

    if high_sev:
        parts.append("<p><strong>Blocking findings:</strong></p><ul>")
        for f in high_sev:
            parts.append(
                f'<li>{_sev_badge_html(f.get("severity", "info"))} '
                f'{_esc(f.get("title", "Untitled"))}</li>'
            )
        parts.append("</ul>")
    if med_sev:
        parts.append("<p><strong>Material findings:</strong></p><ul>")
        for f in med_sev:
            parts.append(
                f'<li>{_sev_badge_html(f.get("severity", "info"))} '
                f'{_esc(f.get("title", "Untitled"))}</li>'
            )
        parts.append("</ul>")
    if not (high_sev or med_sev):
        parts.append("<p><em>No blocking or material findings.</em></p>")

    unmet = result.get("unmet_demand", {}) or {}
    if unmet:
        parts.append(
            "<p><strong>Unmet port demand:</strong> "
            + _esc(", ".join(f"{v}× {k}" for k, v in unmet.items())) + "</p>"
        )

    parts.append("</details>")
    return parts


def _scoring_appendix_html(results):
    parts = [
        "<h2>Scoring Methodology (Appendix)</h2>",
        "<p>Fitness starts at 1000 and is adjusted by overall recommendation, "
        "severity-weighted finding counts, total risk score, interface capacity / "
        "speed allocation checks, scale-headroom utilization, and role-alignment "
        "preferences. The <strong>Recommended platform</strong> is the highest-"
        "ranked candidate whose verdict is <code>LIKELY_FIT</code> or "
        "<code>CONDITIONAL_FIT</code>.</p>",
        "<p><strong>Users should read the verdict and findings above — this "
        "appendix exists for audit, tie-breaker, and calibration purposes "
        "only.</strong></p>",
        "<table>",
        "<tr><th>Rank</th><th>Platform</th><th>Fitness</th></tr>",
    ]
    for idx, r in enumerate(results, start=1):
        parts.append(
            f"<tr><td>{idx}</td>"
            f"<td>{_esc(r['platform_name'])}</td>"
            f"<td>{_esc(r['fitness_score'])}</td></tr>"
        )
    parts.append("</table>")

    for idx, result in enumerate(results, start=1):
        breakdown = result.get("score_breakdown", []) or []
        if not breakdown:
            continue
        parts.append(f"<h3>{idx}. {_esc(result['platform_name'])}</h3>")
        penalties = sorted(
            (s for s in breakdown if s[1] < 0), key=lambda x: x[1]
        )[:3]
        bonuses = sorted(
            (s for s in breakdown if s[1] > 0), key=lambda x: -x[1]
        )[:3]
        parts.append("<ul>")
        if penalties:
            parts.append(
                "<li><strong>Top penalties:</strong> "
                + ", ".join(f"{_esc(p[0])} ({p[1]:+g})" for p in penalties)
                + "</li>"
            )
        if bonuses:
            parts.append(
                "<li><strong>Top bonuses:</strong> "
                + ", ".join(f"{_esc(b[0])} ({b[1]:+g})" for b in bonuses)
                + "</li>"
            )
        parts.append("</ul>")
        groups = _group_breakdown(breakdown)
        if groups:
            parts.append("<table>")
            parts.append(
                "<tr><th>Driver</th>"
                "<th style='width: 110px; text-align: right;'>Net impact</th>"
                "<th>Largest contributors</th></tr>"
            )
            for g in groups:
                examples = "; ".join(
                    f"{_esc(name)} ({val:+g})" for name, val in g["examples"]
                )
                parts.append(
                    f"<tr><td>{_esc(g['label'])}</td>"
                    f"<td style='text-align: right;'>{g['impact']:+g}</td>"
                    f"<td>{examples}</td></tr>"
                )
            parts.append("</table>")

    parts.append(
        "<p style='font-size: 12px; color: #57606a;'>"
        "Full per-row breakdown for every platform is available in "
        "<code>platform_comparison.json</code> under each result's "
        "<code>score_breakdown</code> field.</p>"
    )
    return parts


def build_report_html(comparison, analysis):
    """Unified refresh report as HTML. Same section sequence as the
    Markdown counterpart; scoring lives in the bottom appendix."""
    hostname = comparison.get("device_hostname", "UNKNOWN")
    results = comparison.get("results", [])
    metadata = comparison.get("metadata", {}) or _build_metadata(analysis)
    best_fit_name = comparison.get("best_fit_platform")
    recommended = comparison.get("recommended_platform")
    best_fit_result = _find_best_fit_result(comparison)

    body_parts = [
        f"<h1>Hardware Refresh Report — <code>{_esc(hostname)}</code></h1>",
        f'<p style="font-size: 12px; color: #57606a; margin-top: -8px;">'
        f"Generated {_esc(metadata.get('generated_at', 'UNKNOWN'))} by netfit "
        f"{_esc(metadata.get('netfit_version', 'UNKNOWN'))}.</p>",
    ]

    body_parts.append("<h2>Verdict</h2>")
    if best_fit_result:
        summary = best_fit_result["assessment"].get("assessment_summary", {})
        rec = summary.get("overall_recommendation", "UNKNOWN")
        alloc_ok = best_fit_result.get("allocation_ok", True)
        is_recommended = recommended == best_fit_name
        body_parts.append('<div class="summary-box">')
        body_parts.append(
            "<p><strong>Best-fit platform:</strong> "
            f"<strong>{_esc(best_fit_name)}</strong></p>"
        )
        body_parts.append(
            "<p><strong>Overall recommendation:</strong> "
            f"{_recommendation_badge_html(rec)}</p>"
        )
        alloc_badge = (
            '<span class="rec rec-fit">PASS</span>' if alloc_ok
            else '<span class="rec rec-conditional">FAIL — see Migration Path</span>'
        )
        body_parts.append(
            f"<p><strong>Port-allocation status:</strong> {alloc_badge}</p>"
        )
        body_parts.append(
            "<p><strong>Source profile:</strong> "
            f"<code>{_esc(best_fit_result.get('source_file', ''))}</code></p>"
        )
        body_parts.append(
            "<p><strong>Platforms compared:</strong> "
            f"{_esc(comparison.get('platform_count', 0))}</p>"
        )
        body_parts.append("</div>")
        if not is_recommended:
            body_parts.append(
                '<div class="warning-box"><strong>Caveat:</strong> '
                "this platform is the top-ranked candidate but has "
                "<strong>not</strong> earned <code>LIKELY_FIT</code> or "
                "<code>CONDITIONAL_FIT</code>. Treat as the best available "
                "option, not an endorsement. See Migration Path and Ranked "
                "Candidates below for the disposition.</div>"
            )
    else:
        body_parts.append("<p><em>No candidate platform was scored.</em></p>")

    body_parts.append(_device_context_html(analysis))

    if results:
        body_parts.extend(_ranked_table_html(results, best_fit_name))

    if best_fit_result:
        body_parts.extend(_best_fit_detail_html(best_fit_result, analysis))

    other_results = [
        r for r in results if r["platform_name"] != best_fit_name
    ]
    if other_results:
        body_parts.append("<h2>Other Candidates</h2>")
        for idx, r in enumerate(other_results, start=2):
            body_parts.extend(_other_candidate_html(idx, r))

    if results:
        body_parts.extend(_scoring_appendix_html(results))

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        '<head>\n  <meta charset="utf-8">\n'
        f"  <title>Hardware Refresh — {_esc(hostname)}</title>\n"
        f"  <style>{_HTML_CSS}</style>\n"
        "</head>\n<body>\n"
        + "\n".join(body_parts)
        + "\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# Best-fit helpers (shared by unified Markdown and HTML renderers)
# ---------------------------------------------------------------------------

def _find_best_fit_result(comparison):
    """Return the per-platform result dict for the best-fit platform, or None
    if no results exist."""
    best_fit_name = comparison.get("best_fit_platform")
    if not best_fit_name:
        return None
    for r in comparison.get("results", []):
        if r["platform_name"] == best_fit_name:
            return r
    return None


def _actionable_findings(findings):
    """Return findings that drive migration decisions — drops info notes and
    headroom advisories so the migration-path section reads as a punch list,
    not a status dump."""
    return [
        f for f in findings
        if f.get("severity") in ("critical", "high", "medium")
        or (f.get("severity") == "low" and "approaching" not in f.get("title", "").lower())
    ]


def _validation_checklist_items(result, analysis):
    """Compose a short pre-cutover checklist from assessor findings and
    analyzer-identified refresh risks. Deduplicated; ordered for action."""
    items = []
    seen = set()

    def _add(text):
        normalized = text.strip()
        if normalized and normalized not in seen:
            items.append(normalized)
            seen.add(normalized)

    findings = result.get("assessment", {}).get("findings", []) or []
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for f in sorted(findings, key=lambda x: severity_order.get(x.get("severity"), 4)):
        if f.get("severity") in ("critical", "high", "medium"):
            rec = f.get("recommendation")
            if rec:
                _add(rec)

    unmet = result.get("unmet_demand", {}) or {}
    if unmet:
        unmet_str = ", ".join(f"{v}× {k}" for k, v in unmet.items())
        _add(
            f"Resolve unmet port demand ({unmet_str}) via breakout cabling, "
            f"transceiver mix change, or supplemental line cards."
        )

    for risk in analysis.get("refresh_risks", []) or []:
        _add(risk)

    return items


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def build_platform_comparison_reports(
    analysis_json_path,
    target_profiles_folder,
    comparison_json_output=None,
    report_md_output=None,
    report_html_output=None,
):
    """Load analysis + profiles, compare, and emit outputs.

    Emits at most three files per device: the machine-readable
    `platform_comparison.json` (schema unchanged) and the unified
    human-readable `report.md` / `report.html`. Any output parameter left
    as `None` is skipped.
    """
    analysis = load_json(analysis_json_path)
    profiles = load_target_profiles(target_profiles_folder)
    comparison = compare_platforms(analysis, profiles)

    if comparison_json_output:
        save_json(comparison, comparison_json_output)
    if report_md_output:
        save_text(build_report_markdown(comparison, analysis), report_md_output)
    if report_html_output:
        save_text(build_report_html(comparison, analysis), report_html_output)

    return comparison
