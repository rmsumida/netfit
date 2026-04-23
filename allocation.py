"""Speed-class capacity allocation, shared by assessor and platform_compare.

Maps source-device interface demand by speed class against a target platform's
port supply. Two phases:

Phase 1 — all-native exact matches across every speed class. These don't
compete with each other, so doing them up front prevents lower-speed demand
from greedily upward-subbing into ports that higher-speed demand will need.

Phase 2 — per-speed-class, low-to-high, choose between upward substitution
and breakout fanout based on feasibility:
  - If the sum of available higher-speed native ports is enough to satisfy
    the remaining demand at this speed, do upward only (cheap, no port
    waste).
  - Otherwise, do fanout first (consuming a parent port + a breakout slot
    yields N child ports — N× more efficient than upward at the same parent
    cost), then upward to mop up.

Within fanout, breakout entries are processed lowest-source-speed first
(e.g. `40G_to_4x10G` before `100G_to_4x10G`) to preserve highest-speed
ports for highest-speed demand. Leftover child ports from a partially-used
fanout are discarded — a breakout slot is committed atomically; banking
surplus children would misrepresent physical reality and create ambiguity
in `breakout_used` accounting.

Fanout entries with `dest >= demand_speed` are eligible: a 1G demand can
consume a child of a `40G_to_4x10G` slot via the upward-substitution chain
(1G ≤ 10G).

Lives in its own module so the assessor can call it without creating a cycle
with platform_compare.
"""
import math
import re


_UPWARD_SUBSTITUTES = {
    "1G": ["10G", "25G", "40G", "100G"],
    "10G": ["25G", "40G", "100G"],
    "25G": ["40G", "100G"],
    "40G": ["100G"],
    "100G": [],
}

_SPEED_ORDER = ("100M", "1G", "10G", "25G", "40G", "100G")

_SPEED_RANK = {speed: idx for idx, speed in enumerate(_SPEED_ORDER)}

# Breakout key shape: "<src>_to_<count>x<dest>" e.g. "40G_to_4x10G".
_BREAKOUT_KEY_RE = re.compile(r"^(\d+G|\d+M)_to_(\d+)x(\d+G|\d+M)$")


def _parse_breakout_key(key):
    """Return `(src_speed, count, dest_speed)` or None for unrecognized keys."""
    m = _BREAKOUT_KEY_RE.match(key)
    if not m:
        return None
    src, count, dest = m.group(1), int(m.group(2)), m.group(3)
    if src not in _SPEED_RANK or dest not in _SPEED_RANK:
        return None
    return src, count, dest


def allocate_speed_capacity(source_demand_by_speed, target_native_supply, target_breakout=None):
    """Allocate `source_demand_by_speed` against `target_native_supply`,
    optionally consuming `target_breakout` slots when native + upward cannot
    satisfy demand.

    Returns a dict with:
      allocation_ok (bool),
      unmet_demand (dict[speed, count]),
      allocation_detail (dict[speed, {matched_native, matched_native_upward,
                                      matched_breakout_fanout, matched_breakout,
                                      unmet}]),
      breakout_used (dict[breakout_key, parent_slots_consumed]),
      remaining_supply_by_speed (dict[speed, count]).

    `matched_breakout` is preserved as a legacy alias equal to
    `matched_native_upward + matched_breakout_fanout` so existing renderers
    that display "Matched upward" continue to work without code changes.
    """
    demand = dict(source_demand_by_speed or {})
    supply = dict(target_native_supply or {})
    breakout_supply = dict(target_breakout or {})

    allocation_detail = {
        speed: {
            "matched_native": 0,
            "matched_native_upward": 0,
            "matched_breakout_fanout": 0,
            "matched_breakout": 0,
            "unmet": 0,
        }
        for speed in _SPEED_ORDER
    }
    breakout_used = {}

    # Pre-index breakout entries by destination speed for cheap fanout lookup.
    # Sort by source speed ascending so we consume lower-speed parents first
    # (preserving 100G ports for 100G demand).
    breakout_by_dest = {}
    for key in breakout_supply:
        parsed = _parse_breakout_key(key)
        if parsed is None:
            continue
        src, count, dest = parsed
        breakout_by_dest.setdefault(dest, []).append((_SPEED_RANK[src], src, count, key))
    for dest in breakout_by_dest:
        breakout_by_dest[dest].sort()

    remaining = {speed: demand.get(speed, 0) for speed in ("1G", "10G", "25G", "40G", "100G")}

    # Phase 1: all-native exact matches across every speed class. Doing
    # these up front ensures low-speed upward substitution can't poach
    # ports that high-speed native demand needs.
    for speed in ("1G", "10G", "25G", "40G", "100G"):
        need = remaining[speed]
        if need <= 0:
            continue
        available_native = supply.get(speed, 0)
        matched = min(need, available_native)
        if matched > 0:
            allocation_detail[speed]["matched_native"] = matched
            supply[speed] -= matched
            remaining[speed] = need - matched

    # Phase 2: per-speed (low → high) decide between upward and fanout.
    for speed in ("1G", "10G", "25G", "40G", "100G"):
        need = remaining[speed]
        if need <= 0:
            continue

        upward_supply = sum(supply.get(alt, 0) for alt in _UPWARD_SUBSTITUTES[speed])

        if upward_supply >= need:
            # Upward alone can satisfy the remaining demand; skip fanout.
            for alt_speed in _UPWARD_SUBSTITUTES[speed]:
                if need <= 0:
                    break
                available_alt = supply.get(alt_speed, 0)
                matched_alt = min(need, available_alt)
                if matched_alt > 0:
                    allocation_detail[speed]["matched_native_upward"] += matched_alt
                    allocation_detail[speed]["matched_breakout"] += matched_alt
                    supply[alt_speed] -= matched_alt
                    need -= matched_alt
        else:
            # Upward alone is insufficient — try fanout first (N children per
            # parent is more efficient than upward's 1:1), then mop up with
            # whatever upward natives remain.
            for _, src, count, key in breakout_by_dest.get(speed, []) + sum(
                (breakout_by_dest.get(d, []) for d in _UPWARD_SUBSTITUTES[speed]), []
            ):
                if need <= 0:
                    break
                slots_available = breakout_supply.get(key, 0)
                parent_native = supply.get(src, 0)
                usable_parents = min(slots_available, parent_native)
                if usable_parents <= 0:
                    continue
                parents_to_consume = min(usable_parents, math.ceil(need / count))
                children_yielded = parents_to_consume * count
                matched_from_fanout = min(need, children_yielded)
                allocation_detail[speed]["matched_breakout_fanout"] += matched_from_fanout
                allocation_detail[speed]["matched_breakout"] += matched_from_fanout
                breakout_used[key] = breakout_used.get(key, 0) + parents_to_consume
                breakout_supply[key] -= parents_to_consume
                supply[src] -= parents_to_consume
                need -= matched_from_fanout

            for alt_speed in _UPWARD_SUBSTITUTES[speed]:
                if need <= 0:
                    break
                available_alt = supply.get(alt_speed, 0)
                matched_alt = min(need, available_alt)
                if matched_alt > 0:
                    allocation_detail[speed]["matched_native_upward"] += matched_alt
                    allocation_detail[speed]["matched_breakout"] += matched_alt
                    supply[alt_speed] -= matched_alt
                    need -= matched_alt

        if need > 0:
            allocation_detail[speed]["unmet"] = need

    unmet_demand = {
        speed: detail["unmet"]
        for speed, detail in allocation_detail.items()
        if detail["unmet"] > 0
    }

    return {
        "allocation_ok": not unmet_demand,
        "unmet_demand": unmet_demand,
        "allocation_detail": allocation_detail,
        "breakout_used": breakout_used,
        "remaining_supply_by_speed": supply,
    }
