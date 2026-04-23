"""Speed-class capacity allocation, shared by assessor and platform_compare.

The allocator maps source-device interface demand by speed class against a
target platform's native port supply, with greedy upward substitution
(1G → {10G,25G,40G,100G}; 10G → {25G,40G,100G}; etc.). Breakout slots are
advertised but not yet consumed.

Lives in its own module so the assessor can call it for finding generation
without creating a cycle with platform_compare.
"""


_UPWARD_SUBSTITUTES = {
    "1G": ["10G", "25G", "40G", "100G"],
    "10G": ["25G", "40G", "100G"],
    "25G": ["40G", "100G"],
    "40G": ["100G"],
    "100G": [],
}

_SPEED_ORDER = ("100M", "1G", "10G", "25G", "40G", "100G")


def allocate_speed_capacity(source_demand_by_speed, target_native_supply, target_breakout=None):
    """Allocate `source_demand_by_speed` against `target_native_supply` with
    greedy upward substitution. Returns a dict with `allocation_ok`,
    `unmet_demand`, `allocation_detail`, `breakout_used`,
    `remaining_supply_by_speed`.
    """
    demand = dict(source_demand_by_speed or {})
    supply = dict(target_native_supply or {})

    allocation_detail = {
        speed: {"matched_native": 0, "matched_breakout": 0, "unmet": 0}
        for speed in _SPEED_ORDER
    }

    for speed in ("1G", "10G", "25G", "40G", "100G"):
        need = demand.get(speed, 0)
        if need <= 0:
            continue

        available_native = supply.get(speed, 0)
        matched = min(need, available_native)
        if matched > 0:
            allocation_detail[speed]["matched_native"] = matched
            supply[speed] -= matched
            need -= matched

        for alt_speed in _UPWARD_SUBSTITUTES[speed]:
            if need <= 0:
                break
            available_alt = supply.get(alt_speed, 0)
            matched_alt = min(need, available_alt)
            if matched_alt > 0:
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
        "breakout_used": {},
        "remaining_supply_by_speed": supply,
    }
