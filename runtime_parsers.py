"""Per-intent parsers for NetBrain harvest output (Cisco IOS / IOS-XE).

Each parser is a pure function that accepts the raw text of one show-command
output and the source command string, and returns a dict shaped to fit a slot
under the analysis report's `runtime` section. The loader composes the slot
output into the full `runtime.*` shape; see runtime_loader.INTENT_TARGETS.

Adding a new alias for an existing intent is a runtime_loader.ALIAS_MAP edit
only — no parser change needed unless the alias's output dialect differs, in
which case the parser branches on `source_command`.
"""
import re


_PROTOCOL_NAMES = {
    "connected", "static", "ospf", "bgp", "eigrp", "isis",
    "rip", "mobile", "odr", "nhrp", "lisp", "application",
}


def parse_inventory(raw_text, source_command=None):
    """Parse `show inventory`. Returns inventory section: chassis + modules + transceivers.

    Stanza format:
        NAME: "<slot>", DESCR: "<descr>"
        PID: <pid>, VID: <vid>, SN: <sn>
    """
    chassis_pid = None
    chassis_serial = None
    modules = []
    transceivers = []

    name_re = re.compile(r'^NAME:\s*"([^"]*)",\s*DESCR:\s*"([^"]*)"\s*$')
    pid_re = re.compile(
        r'^PID:\s*(\S*?)\s*,\s*VID:\s*(\S*?)\s*,\s*SN:\s*(\S*)\s*$'
    )

    lines = raw_text.splitlines()
    i = 0
    while i < len(lines):
        m_name = name_re.match(lines[i].strip())
        if not m_name:
            i += 1
            continue
        slot, descr = m_name.group(1), m_name.group(2)
        if i + 1 >= len(lines):
            break
        m_pid = pid_re.match(lines[i + 1].strip())
        if not m_pid:
            i += 1
            continue
        pid, vid, serial = m_pid.group(1), m_pid.group(2), m_pid.group(3)

        entry = {"slot": slot, "descr": descr, "pid": pid, "vid": vid, "serial": serial}
        if re.search(r"chassis", slot, re.IGNORECASE) and chassis_pid is None:
            chassis_pid = pid
            chassis_serial = serial
        elif re.search(r"transceiver", slot, re.IGNORECASE):
            transceivers.append(entry)
        else:
            modules.append(entry)
        i += 2

    return {
        "chassis_pid": chassis_pid,
        "chassis_serial": chassis_serial,
        "modules": modules,
        "transceivers": transceivers,
    }


_UPTIME_UNITS = {
    "year": 365 * 86400, "years": 365 * 86400,
    "week": 7 * 86400, "weeks": 7 * 86400,
    "day": 86400, "days": 86400,
    "hour": 3600, "hours": 3600,
    "minute": 60, "minutes": 60,
}


def _uptime_to_seconds(uptime_text):
    total = 0
    for num, unit in re.findall(r"(\d+)\s+(years?|weeks?|days?|hours?|minutes?)", uptime_text):
        total += int(num) * _UPTIME_UNITS[unit]
    return total or None


def parse_version(raw_text, source_command=None):
    """Parse `show version`. Returns platform-section fields."""
    out = {
        "software_version": None,
        "image_name": None,
        "uptime_seconds": None,
        "rommon_version": None,
        "license_level": None,
    }

    m = re.search(r"Cisco IOS XE Software,\s*Version\s+(\S+)", raw_text)
    if m:
        out["software_version"] = m.group(1)
    else:
        m = re.search(r"Cisco IOS Software.*?,?\s*Version\s+(\S+?),", raw_text)
        if m:
            out["software_version"] = m.group(1)

    m = re.search(r'System image file is\s+"([^"]+)"', raw_text)
    if m:
        out["image_name"] = m.group(1)

    m = re.search(r"^\s*\S+\s+uptime is\s+(.+?)\s*$", raw_text, re.MULTILINE)
    if m:
        out["uptime_seconds"] = _uptime_to_seconds(m.group(1))

    m = re.search(r"^\s*ROM:\s*(.+?)\s*$", raw_text, re.MULTILINE)
    if m:
        out["rommon_version"] = m.group(1)

    m = re.search(r"^\s*License Level:\s*(\S+)", raw_text, re.MULTILINE)
    if m:
        out["license_level"] = m.group(1)

    return out


def parse_route_table_ipv4_summary(raw_text, source_command=None):
    """Parse `show ip route summary`. Returns route_table-section fields.

    Tolerates missing header row (some sanitization passes strip it).
    Skips indented continuation lines (BGP External/Internal, OSPF Intra/Inter).
    """
    out = {
        "ipv4_total": None,
        "ipv4_by_protocol": {},
        "ipv4_memory_bytes": None,
    }

    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if raw_line.startswith((" ", "\t")):
            # indented sub-totals (Intra-area, External, Internal, Local)
            continue
        tokens = line.split()
        if not tokens:
            continue
        head = tokens[0]
        if head == "Total":
            nums = [int(t) for t in tokens[1:] if t.isdigit()]
            if len(nums) >= 2:
                out["ipv4_total"] = nums[0] + nums[1]
            if nums:
                out["ipv4_memory_bytes"] = nums[-1]
            continue
        if head not in _PROTOCOL_NAMES:
            continue
        nums = [int(t) for t in tokens[1:] if t.isdigit()]
        if len(nums) < 5:
            continue
        cols = nums[-5:]  # Networks, Subnets, Replicates, Overhead, Memory
        out["ipv4_by_protocol"][head] = cols[0] + cols[1]

    return out


def parse_nat_statistics(raw_text, source_command=None):
    """Parse `show ip nat statistics`. Returns nat-section fields."""
    out = {
        "active_translations": None,
        "peak_translations": None,
        "hits": None,
        "misses": None,
    }

    m = re.search(r"Total active translations:\s*(\d+)", raw_text)
    if m:
        out["active_translations"] = int(m.group(1))

    m = re.search(r"Peak translations:\s*(\d+)", raw_text)
    if m:
        out["peak_translations"] = int(m.group(1))

    m = re.search(r"Hits:\s*(\d+)\s+Misses:\s*(\d+)", raw_text)
    if m:
        out["hits"] = int(m.group(1))
        out["misses"] = int(m.group(2))

    return out


def parse_crypto_ipsec_summary(raw_text, source_command=None):
    """Parse `show crypto ipsec sa count` (primary) or `show crypto ipsec sa`
    (long form — count derived by summing the per-SA `spi:` lines).

    Returns crypto-section fields.
    """
    out = {
        "active_sas": None,
        "total_sas": None,
        "cloned_sas": None,
    }

    m = re.search(r"Total IPsec SAs:\s*(\d+)", raw_text)
    if m:
        out["total_sas"] = int(m.group(1))
    m = re.search(r"Active IPsec SAs:\s*(\d+)", raw_text)
    if m:
        out["active_sas"] = int(m.group(1))
    m = re.search(r"Cloned IPsec SAs:\s*(\d+)", raw_text)
    if m:
        out["cloned_sas"] = int(m.group(1))

    if out["active_sas"] is not None:
        return out

    # Derive from long-form output: each active SA has one indented `spi:` line.
    # The "current outbound spi:" header line does not match (no leading "spi:" token).
    spi_lines = re.findall(
        r"^\s+spi:\s*0x[0-9a-fA-F]+", raw_text, re.MULTILINE
    )
    if spi_lines:
        out["active_sas"] = len(spi_lines)

    return out


def parse_license_summary(raw_text, source_command=None):
    """Parse `show license summary` / `show license all` / `show license feature`.

    Detects three dialects:
      - Smart Licensing: `License Usage:` header followed by entitlement table.
      - Classic w/ License Level: single `License Level: <tier>` line.
      - Classic w/ Feature blocks (`show license all`): `Feature: <name>` blocks
        with a following `License State:` line.
    """
    out = {
        "model": None,
        "tier": None,
        "entitlements": [],
        "compliance_status": None,
        "features": [],
    }

    if "License Usage:" in raw_text:
        out["model"] = "smart"
        in_table = False
        statuses = []
        for line in raw_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("License") and "Entitlement Tag" in stripped:
                in_table = True
                continue
            if not in_table:
                continue
            if stripped.startswith("---") or not stripped:
                continue
            m = re.match(
                r"(\S+)\s+\(([^)]+)\)\s+(\d+)\s+(.+)$", stripped
            )
            if not m:
                continue
            name, tag, count, status = m.group(1), m.group(2), int(m.group(3)), m.group(4).strip()
            out["entitlements"].append(
                {"name": name, "tag": tag, "count": count, "status": status}
            )
            statuses.append(status)
        if out["entitlements"]:
            out["tier"] = out["entitlements"][0]["name"]
        if statuses:
            out["compliance_status"] = "IN USE" if all(s == "IN USE" for s in statuses) else statuses[0]
        return out

    feature_re = re.compile(r"^Feature:\s*(\S+)\s+Version:\s*(\S+)", re.MULTILINE)
    if feature_re.search(raw_text):
        out["model"] = "classic"
        blocks = re.split(r"^Feature:\s*", raw_text, flags=re.MULTILINE)
        for block in blocks[1:]:
            head = block.splitlines()[0]
            name = head.split()[0] if head.split() else None
            type_m = re.search(r"License Type:\s*(.+?)\s*$", block, re.MULTILINE)
            state_m = re.search(r"License State:\s*(.+?)\s*$", block, re.MULTILINE)
            out["features"].append({
                "name": name,
                "type": type_m.group(1).strip() if type_m else None,
                "state": state_m.group(1).strip() if state_m else None,
            })
        in_use = [f for f in out["features"] if f.get("state") and "In Use" in f["state"]]
        if in_use:
            out["tier"] = in_use[0]["name"]
            out["compliance_status"] = in_use[0]["state"]
        return out

    m = re.search(r"^\s*License Level:\s*(\S+)", raw_text, re.MULTILINE)
    if m:
        out["model"] = "classic"
        out["tier"] = m.group(1)

    return out


def parse_cpu_processes(raw_text, source_command=None):
    """Parse `show processes cpu sorted`. Returns platform-section CPU fields."""
    out = {
        "cpu_5sec_pct": None,
        "cpu_5sec_interrupt_pct": None,
        "cpu_1min_pct": None,
        "cpu_5min_pct": None,
        "top_processes": [],
    }

    m = re.search(
        r"CPU utilization for five seconds:\s*(\d+)%/(\d+)%;\s*one minute:\s*(\d+)%;\s*five minutes:\s*(\d+)%",
        raw_text,
    )
    if m:
        out["cpu_5sec_pct"] = int(m.group(1))
        out["cpu_5sec_interrupt_pct"] = int(m.group(2))
        out["cpu_1min_pct"] = int(m.group(3))
        out["cpu_5min_pct"] = int(m.group(4))

    in_table = False
    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip()
        if not in_table:
            if stripped.startswith("PID") and "Runtime" in stripped and "Process" in stripped:
                in_table = True
            continue
        if not stripped:
            continue
        # Row shape: PID Runtime Invoked uSecs 5Sec% 1Min% 5Min% TTY Process...
        tokens = stripped.split()
        if len(tokens) < 9 or not tokens[0].isdigit():
            continue
        try:
            pid = int(tokens[0])
            cpu_5min = float(tokens[6].rstrip("%"))
        except (ValueError, IndexError):
            continue
        process_name = " ".join(tokens[8:])
        out["top_processes"].append({
            "pid": pid,
            "name": process_name,
            "cpu_5min_pct": cpu_5min,
        })

    return out


# Media-type → speed-class lookup for transceiver parsing. Embedding the
# table in the parser keeps the speed_class_refiner module dialect-free.
# Match prefixes (e.g. "1000BASE-T", "10GBASE-LR") so vendor variants
# resolve to the right class.
_TRANSCEIVER_MEDIA_SPEED = (
    ("1000BASE", "1G"),
    ("10GBASE", "10G"),
    ("25GBASE", "25G"),
    ("40GBASE", "40G"),
    ("100GBASE", "100G"),
)


def _media_type_to_speed(media_type):
    if not media_type:
        return None
    upper = media_type.upper()
    for prefix, speed in _TRANSCEIVER_MEDIA_SPEED:
        if upper.startswith(prefix):
            return speed
    return None


# Bandwidth (Kbit/sec) → speed-class lookup for `show interfaces` parsing.
# Exact match by Cisco-reported bandwidth value.
_BW_KBIT_TO_SPEED = {
    100000: "100M",
    1000000: "1G",
    10000000: "10G",
    25000000: "25G",
    40000000: "40G",
    100000000: "100G",
}


def parse_interfaces_transceiver(raw_text, source_command=None):
    """Parse `show interfaces transceiver` and `show interfaces transceiver
    detail`. Returns the runtime.interfaces section's transceiver lookup.

    The table form (no `detail`) carries only optical power readings — those
    let us know a transceiver is present but not what speed it is. The
    `detail` form adds a `Media Type:` line per interface, which is the
    load-bearing field for #17.

    Output:
        {"transceivers_by_interface": {
            "TenGigabitEthernet0/0/1": {
                "media_type": "1000BASE-T",
                "speed_inferred": "1G",
                "tx_power_dbm": -2.1,        # nullable
                "rx_power_dbm": -3.5,        # nullable
                "temperature_c": 33.4,       # nullable
            },
            ...
        }}

    Per-interface entries appear under whichever form yielded data; both
    forms can populate the same dict if both are present in the harvest.
    """
    out = {}

    # --- Table form: per-interface row with optical metrics ---
    # Header line includes "Temperature" "Voltage" "Current" "Tx Power" "Rx Power".
    # Data rows: Port  Temp  Voltage  Current  TxPower  RxPower
    table_data_re = re.compile(
        r"^\s*(\S+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*$"
    )
    in_data_block = False
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            in_data_block = False
            continue
        if "Tx Power" in stripped or "TxPower" in stripped:
            in_data_block = True
            continue
        # Skip the dashed separator under the header.
        if in_data_block and set(stripped) <= set("- "):
            continue
        if not in_data_block:
            continue
        m = table_data_re.match(line)
        if not m:
            continue
        port, temp, _voltage, _current, tx, rx = m.groups()
        try:
            entry = out.setdefault(port, {})
            entry["temperature_c"] = float(temp)
            entry["tx_power_dbm"] = float(tx)
            entry["rx_power_dbm"] = float(rx)
        except ValueError:
            continue

    # --- Detail form: per-interface stanza with Media Type ---
    # Stanza shape:
    #   <interface-name>
    #     Transceiver Type      : SFP
    #     Media Type            : 1000BASE-T
    #     ...
    intf_header_re = re.compile(r"^([A-Za-z]+\S*)\s*$")
    media_re = re.compile(r"^\s*Media\s+Type\s*:\s*(\S.*?)\s*$", re.IGNORECASE)
    current_intf = None
    for line in raw_text.splitlines():
        m_intf = intf_header_re.match(line)
        if m_intf:
            candidate = m_intf.group(1)
            # Don't pick up table-data rows or random words as an interface
            # header — interface names start with a known type prefix.
            if any(candidate.lower().startswith(p) for p in (
                "gigabit", "tengig", "twentyfivegig", "fortygig", "hundredgig",
                "fastethernet", "ethernet", "te", "gi", "fa", "fo", "hu",
            )):
                current_intf = candidate
                continue
        m_media = media_re.match(line)
        if m_media and current_intf:
            media = m_media.group(1).strip()
            entry = out.setdefault(current_intf, {})
            entry["media_type"] = media
            speed = _media_type_to_speed(media)
            if speed:
                entry["speed_inferred"] = speed

    return {"transceivers_by_interface": out}


def parse_interfaces(raw_text, source_command=None):
    """Parse `show interfaces` (long form). Returns the runtime.interfaces
    section's operational lookup.

    Per-interface stanza begins with `<name> is <state>, line protocol is
    <state>` and includes a `BW <kbit> Kbit/sec` line. The `BW` value is
    used by the speed_class_refiner to detect cases like a TenGigabitEthernet
    slot driving a 1G optic.

    Output:
        {"operational_by_interface": {
            "TenGigabitEthernet0/0/1": {
                "line_protocol": "up",
                "bandwidth_kbit": 1000000,
                "speed_inferred": "1G",
            },
            ...
        }}
    """
    out = {}

    intf_header_re = re.compile(
        r"^(\S+)\s+is\s+\S.*?,\s*line\s+protocol\s+is\s+(\S+)",
        re.IGNORECASE,
    )
    bw_re = re.compile(r"BW\s+(\d+)\s+Kbit", re.IGNORECASE)

    current_intf = None
    for line in raw_text.splitlines():
        m_intf = intf_header_re.match(line)
        if m_intf:
            current_intf = m_intf.group(1)
            out.setdefault(current_intf, {})["line_protocol"] = m_intf.group(2).lower()
            continue
        if current_intf is None:
            continue
        m_bw = bw_re.search(line)
        if m_bw:
            kbit = int(m_bw.group(1))
            entry = out[current_intf]
            entry["bandwidth_kbit"] = kbit
            speed = _BW_KBIT_TO_SPEED.get(kbit)
            if speed:
                entry["speed_inferred"] = speed

    return {"operational_by_interface": out}


INTENT_PARSERS = {
    "inventory": parse_inventory,
    "version": parse_version,
    "route_table_ipv4_summary": parse_route_table_ipv4_summary,
    "nat_statistics": parse_nat_statistics,
    "crypto_ipsec_summary": parse_crypto_ipsec_summary,
    "license_summary": parse_license_summary,
    "cpu_processes": parse_cpu_processes,
    "interfaces_transceiver": parse_interfaces_transceiver,
    "interfaces_operational": parse_interfaces,
}
