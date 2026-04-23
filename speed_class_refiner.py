"""Refine each interface's effective_speed_class using runtime data.

The analyzer (`analyzer.py::_normalize_speed_class`) derives speed-class
demand purely from interface type names (TenGigabitEthernet → 10G, etc.) —
the *port-class capability*, not the actually-driven speed. A 10G slot
running a 1G SFP is real 1G demand, not 10G. Sizing decisions made on
inflated 10G demand pick the wrong platform.

This refiner runs after the analyzer + runtime merge in main.py. It walks
each active physical interface's detail record, looks up runtime data for
that interface (transceiver-derived first, then operational/bandwidth-
derived), and overwrites `effective_speed_class` when a higher-confidence
source is available. The original analyzer-derived value is preserved as
`effective_speed_class_original` for audit, and a new
`effective_speed_class_source` field records which inference was used.

The rolled-up `interfaces.active_physical_by_speed_class` counter is
recomputed from the refined values — this is what the assessor and
allocator actually consume, so it must reflect the corrected demand.

A summary block `summary.speed_class_inference` records counts by source
so the renderers can disclose to the operator which inputs the verdict
relied on. This is the verdict-stability guard: when runtime data is
present and reclassifies demand, the report must say so explicitly.

The refiner is a no-op when:
  - `report['runtime']` is absent or carries no transceiver/operational data
  - the analyzer report has no interface details to walk

Idempotent — calling it twice produces the same result as calling once.

Lives outside `analyzer.py` to preserve DEC-001 (only analyzer.py parses
config dialect; the refiner consumes parsed runtime + parsed details).
"""

_SPEED_PRIORITY = ("transceiver", "operational", "interface_type")


def _build_lookup_by_interface(runtime_section, key):
    """Pull `runtime['interfaces'][key]` defensively. Returns {} when
    absent so callers can blindly index by interface name."""
    if not isinstance(runtime_section, dict):
        return {}
    section = runtime_section.get(key)
    if not isinstance(section, dict):
        return {}
    return section


def refine_speed_classes(report):
    """Refine each active physical interface's effective_speed_class using
    runtime data. Mutates report in place. Idempotent.

    Priority: transceiver > operational > interface_type (the analyzer's
    name-based default). When a higher-priority source produces a non-None
    speed class, it wins; otherwise the existing value is preserved.

    After per-interface refinement, recomputes
    `interfaces.active_physical_by_speed_class` from the refined values
    (active + physical only) and writes
    `summary.speed_class_inference: {by_<source>: count}`.
    """
    interfaces = report.get("interfaces") or {}
    details = interfaces.get("details") or []
    runtime_intf = _build_lookup_by_interface(report.get("runtime"), "interfaces")
    transceivers = _build_lookup_by_interface(runtime_intf, "transceivers_by_interface")
    operational = _build_lookup_by_interface(runtime_intf, "operational_by_interface")

    counts = {f"by_{source}": 0 for source in _SPEED_PRIORITY}

    for detail in details:
        if not detail.get("is_physical"):
            continue

        original = detail.get("effective_speed_class")
        # Stash the analyzer's name-based call once. Idempotency: if the
        # refiner already ran (re-merging runtime data), don't overwrite
        # the original with a previously-refined value.
        if "effective_speed_class_original" not in detail:
            detail["effective_speed_class_original"] = original

        intf_name = detail.get("name")
        refined = original
        source = "interface_type"

        tx = transceivers.get(intf_name) or {}
        tx_speed = tx.get("speed_inferred")
        op = operational.get(intf_name) or {}
        op_speed = op.get("speed_inferred")

        if tx_speed:
            refined = tx_speed
            source = "transceiver"
        elif op_speed:
            refined = op_speed
            source = "operational"

        detail["effective_speed_class"] = refined
        detail["effective_speed_class_source"] = source

        # Only count active physical interfaces — the rolled-up counter
        # below applies the same filter, so the inference summary should
        # match what the verdict actually consumed.
        if detail.get("is_active"):
            counts[f"by_{source}"] += 1

    # Recompute active_physical_by_speed_class from the refined per-interface
    # values. Match analyzer.py's filter at lines 376-388 (active + physical).
    by_speed = {}
    for detail in details:
        if not (detail.get("is_physical") and detail.get("is_active")):
            continue
        speed = detail.get("effective_speed_class")
        if not speed:
            continue
        by_speed[speed] = by_speed.get(speed, 0) + 1
    interfaces["active_physical_by_speed_class"] = by_speed

    summary = report.setdefault("summary", {})
    summary["speed_class_inference"] = counts
