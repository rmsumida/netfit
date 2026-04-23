"""Per-intent runtime parser tests (one per MVP intent)."""
from pathlib import Path

import pytest

from runtime_parsers import (
    parse_inventory,
    parse_version,
    parse_route_table_ipv4_summary,
    parse_nat_statistics,
    parse_crypto_ipsec_summary,
    parse_license_summary,
    parse_cpu_processes,
    parse_interfaces_transceiver,
    parse_interfaces,
)


FIXTURES = Path(__file__).parent / "fixtures" / "netbrain"


def _read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# -------------------------
# inventory
# -------------------------

def test_parse_inventory_extracts_chassis():
    out = parse_inventory(_read("inventory.txt"))
    assert out["chassis_pid"] == "ASR1013"
    assert out["chassis_serial"] == "XOX1234ABCD"


def test_parse_inventory_classifies_modules_and_transceivers():
    out = parse_inventory(_read("inventory.txt"))
    module_slots = [m["slot"] for m in out["modules"]]
    transceiver_slots = [t["slot"] for t in out["transceivers"]]
    assert "module 0" in module_slots
    assert "module F1" in module_slots
    assert "SPA subslot 0/0" in module_slots
    assert "subslot 0/0 transceiver 0" in transceiver_slots
    assert "subslot 0/1 transceiver 0" in transceiver_slots
    # Chassis must not appear in modules/transceivers list.
    assert "Chassis" not in module_slots


def test_parse_inventory_preserves_pid_vid_serial():
    out = parse_inventory(_read("inventory.txt"))
    esp = next(m for m in out["modules"] if m["slot"] == "module F1")
    assert esp["pid"] == "ASR1000-ESP100"
    assert esp["vid"] == "V03"
    assert esp["serial"] == "JJJ12345678"


def test_parse_inventory_handles_empty_input():
    assert parse_inventory("") == {
        "chassis_pid": None, "chassis_serial": None,
        "modules": [], "transceivers": [],
    }


# -------------------------
# version
# -------------------------

def test_parse_version_extracts_software_and_image():
    out = parse_version(_read("version.txt"))
    assert out["software_version"] == "16.03.07"
    assert out["image_name"] == "bootflash:asr1000rpx86-universalk9.16.03.07.SPA.bin"
    assert out["rommon_version"] == "IOS-XE ROMMON"


def test_parse_version_uptime_to_seconds():
    out = parse_version(_read("version.txt"))
    # 1 year + 24 weeks + 3 days + 12 hours + 8 minutes
    expected = (365 + 24 * 7 + 3) * 86400 + 12 * 3600 + 8 * 60
    assert out["uptime_seconds"] == expected


def test_parse_version_classic_license_level():
    sample = (
        "Cisco IOS XE Software, Version 17.09.04a\n"
        "router uptime is 5 days\n"
        "License Level: advipservices\n"
    )
    out = parse_version(sample)
    assert out["license_level"] == "advipservices"


# -------------------------
# route_table_ipv4_summary
# -------------------------

def test_parse_route_summary_total_and_memory():
    out = parse_route_table_ipv4_summary(_read("route_table_ipv4_summary.txt"))
    # Total row: Networks=42, Subnets=487602
    assert out["ipv4_total"] == 42 + 487602
    assert out["ipv4_memory_bytes"] == 140940980


def test_parse_route_summary_per_protocol_breakdown():
    out = parse_route_table_ipv4_summary(_read("route_table_ipv4_summary.txt"))
    by_proto = out["ipv4_by_protocol"]
    assert by_proto["connected"] == 0 + 24
    assert by_proto["static"] == 0 + 17
    assert by_proto["ospf"] == 12 + 487
    assert by_proto["bgp"] == 18 + 487034
    # The "internal" row is accounting overhead, not routes — must be excluded.
    assert "internal" not in by_proto


def test_parse_route_summary_skips_indented_subtotals():
    # The Intra-area / External / Internal continuation lines must not be
    # parsed as protocol rows.
    out = parse_route_table_ipv4_summary(_read("route_table_ipv4_summary.txt"))
    assert "Intra-area:" not in out["ipv4_by_protocol"]
    assert "External:" not in out["ipv4_by_protocol"]


def test_parse_route_summary_tolerates_missing_header():
    # Some sanitization passes strip the "Route Source" header. The parser
    # must still extract per-protocol counts from the bare body.
    sample = (
        "IP routing table name is default (0x0)\n"
        "connected 0 100 0 10000 30000\n"
        "static    2 10  0 1500  5000\n"
        "bgp 65001 250 20000 0 2000000 6500000\n"
        "Total     800 21000 0 2100000 8500000\n"
    )
    out = parse_route_table_ipv4_summary(sample)
    assert out["ipv4_total"] == 800 + 21000
    assert out["ipv4_by_protocol"]["bgp"] == 250 + 20000


# -------------------------
# nat_statistics
# -------------------------

def test_parse_nat_full():
    out = parse_nat_statistics(_read("nat_statistics.txt"))
    assert out["active_translations"] == 12453
    assert out["peak_translations"] == 18900
    assert out["hits"] == 4823498234
    assert out["misses"] == 124993


def test_parse_nat_sparse_returns_nulls_for_missing():
    out = parse_nat_statistics(_read("nat_statistics_sparse.txt"))
    assert out["active_translations"] == 4444
    assert out["peak_translations"] is None
    assert out["hits"] is None
    assert out["misses"] is None


# -------------------------
# crypto_ipsec_summary
# -------------------------

def test_parse_crypto_count_form():
    out = parse_crypto_ipsec_summary(
        _read("crypto_ipsec_sa_count.txt"),
        source_command="show crypto ipsec sa count",
    )
    assert out["active_sas"] == 247
    assert out["total_sas"] == 247
    assert out["cloned_sas"] == 0


def test_parse_crypto_long_form_derives_count():
    # Fixture has 2 tunnels, each with 1 inbound + 1 outbound spi line = 4 SAs.
    # The "current outbound spi:" header line must NOT be counted (it's not
    # a per-SA spi: line).
    out = parse_crypto_ipsec_summary(
        _read("crypto_ipsec_sa_detail.txt"),
        source_command="show crypto ipsec sa",
    )
    assert out["active_sas"] == 4
    assert out["total_sas"] is None  # not present in long form


# -------------------------
# license_summary
# -------------------------

def test_parse_license_smart():
    out = parse_license_summary(_read("license_summary_smart.txt"))
    assert out["model"] == "smart"
    assert out["tier"] == "network-advantage_T1"
    assert out["compliance_status"] == "IN USE"
    names = [e["name"] for e in out["entitlements"]]
    assert names == ["network-advantage_T1", "dna-advantage_T1"]


def test_parse_license_classic_level():
    out = parse_license_summary(_read("license_summary_classic.txt"))
    assert out["model"] == "classic"
    assert out["tier"] == "advipservices"


def test_parse_license_classic_feature_blocks():
    out = parse_license_summary(_read("license_all_classic_feature.txt"))
    assert out["model"] == "classic"
    assert out["tier"] == "adventerprise"
    feature_names = [f["name"] for f in out["features"]]
    assert feature_names == ["adventerprise", "ipbase"]
    advent = next(f for f in out["features"] if f["name"] == "adventerprise")
    assert advent["state"] == "Active, In Use"


# -------------------------
# cpu_processes
# -------------------------

def test_parse_cpu_headline():
    out = parse_cpu_processes(_read("cpu_processes.txt"))
    assert out["cpu_5sec_pct"] == 23
    assert out["cpu_5sec_interrupt_pct"] == 8
    assert out["cpu_1min_pct"] == 19
    assert out["cpu_5min_pct"] == 18


def test_parse_cpu_top_processes():
    out = parse_cpu_processes(_read("cpu_processes.txt"))
    names = [p["name"] for p in out["top_processes"]]
    assert "BGP Scanner" in names
    assert "IP Input" in names
    bgp = next(p for p in out["top_processes"] if p["name"] == "BGP Scanner")
    assert bgp["pid"] == 192
    assert bgp["cpu_5min_pct"] == pytest.approx(2.74)


# -------------------------
# interfaces transceiver
# -------------------------

_TRANSCEIVER_SAMPLE = """\
                            Optical   Optical
            Temperature  Voltage  Current   Tx Power  Rx Power
Port           (Celsius)    (Volts)  (mA)      (dBm)     (dBm)
---------    -----------  -------  --------  --------  --------
Te0/0/1            33.4     3.31     8.4      -2.1      -3.5
Te0/0/2            32.1     3.30    10.2       0.4       0.1

TenGigabitEthernet0/0/1
  Transceiver Type      : SFP+
  Media Type            : 1000BASE-T
  Connector Type        : RJ45
TenGigabitEthernet0/0/2
  Transceiver Type      : SFP+
  Media Type            : 10GBASE-LR
  Connector Type        : LC
"""


def test_parse_interfaces_transceiver_picks_up_table_optical_metrics():
    out = parse_interfaces_transceiver(_TRANSCEIVER_SAMPLE)
    by_intf = out["transceivers_by_interface"]
    assert by_intf["Te0/0/1"]["temperature_c"] == pytest.approx(33.4)
    assert by_intf["Te0/0/1"]["tx_power_dbm"] == pytest.approx(-2.1)
    assert by_intf["Te0/0/1"]["rx_power_dbm"] == pytest.approx(-3.5)
    assert by_intf["Te0/0/2"]["tx_power_dbm"] == pytest.approx(0.4)


def test_parse_interfaces_transceiver_infers_speed_from_media_type():
    out = parse_interfaces_transceiver(_TRANSCEIVER_SAMPLE)
    by_intf = out["transceivers_by_interface"]
    assert by_intf["TenGigabitEthernet0/0/1"]["media_type"] == "1000BASE-T"
    assert by_intf["TenGigabitEthernet0/0/1"]["speed_inferred"] == "1G"
    assert by_intf["TenGigabitEthernet0/0/2"]["media_type"] == "10GBASE-LR"
    assert by_intf["TenGigabitEthernet0/0/2"]["speed_inferred"] == "10G"


def test_parse_interfaces_transceiver_handles_unknown_media_type():
    sample = """\
GigabitEthernet0/0/0
  Transceiver Type      : SFP
  Media Type            : SOMETHING-EXOTIC-XYZ
"""
    out = parse_interfaces_transceiver(sample)
    by_intf = out["transceivers_by_interface"]
    assert by_intf["GigabitEthernet0/0/0"]["media_type"] == "SOMETHING-EXOTIC-XYZ"
    # No speed_inferred field when the prefix doesn't match the lookup table.
    assert "speed_inferred" not in by_intf["GigabitEthernet0/0/0"]


def test_parse_interfaces_transceiver_table_only_yields_no_speed():
    """The bare table form (no `detail` keyword) carries optical metrics
    but no media type — speed_inferred should be absent."""
    sample = """\
                            Optical   Optical
            Temperature  Voltage  Current   Tx Power  Rx Power
Port           (Celsius)    (Volts)  (mA)      (dBm)     (dBm)
---------    -----------  -------  --------  --------  --------
Te0/0/1            33.4     3.31     8.4      -2.1      -3.5
"""
    out = parse_interfaces_transceiver(sample)
    by_intf = out["transceivers_by_interface"]
    assert "speed_inferred" not in by_intf["Te0/0/1"]
    assert "media_type" not in by_intf["Te0/0/1"]


# -------------------------
# interfaces (operational)
# -------------------------

_INTERFACES_SAMPLE = """\
TenGigabitEthernet0/0/1 is up, line protocol is up
  Hardware is 10G, address is aabb.cc00.0011
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
TenGigabitEthernet0/0/2 is up, line protocol is up
  Hardware is 10G, address is aabb.cc00.0022
  MTU 1500 bytes, BW 10000000 Kbit/sec, DLY 10 usec,
GigabitEthernet0/0/0 is administratively down, line protocol is down
  MTU 1500 bytes, BW 1000000 Kbit/sec, DLY 10 usec,
"""


def test_parse_interfaces_extracts_line_protocol_and_bandwidth():
    out = parse_interfaces(_INTERFACES_SAMPLE)
    by_intf = out["operational_by_interface"]
    assert by_intf["TenGigabitEthernet0/0/1"]["line_protocol"] == "up"
    assert by_intf["TenGigabitEthernet0/0/1"]["bandwidth_kbit"] == 1000000
    assert by_intf["TenGigabitEthernet0/0/1"]["speed_inferred"] == "1G"
    assert by_intf["TenGigabitEthernet0/0/2"]["bandwidth_kbit"] == 10000000
    assert by_intf["TenGigabitEthernet0/0/2"]["speed_inferred"] == "10G"
    assert by_intf["GigabitEthernet0/0/0"]["line_protocol"] == "down"


def test_parse_interfaces_handles_unrecognized_bandwidth():
    sample = """\
Ethernet1/1 is up, line protocol is up
  MTU 1500 bytes, BW 99999 Kbit/sec, DLY 10 usec,
"""
    out = parse_interfaces(sample)
    by_intf = out["operational_by_interface"]
    assert by_intf["Ethernet1/1"]["bandwidth_kbit"] == 99999
    assert "speed_inferred" not in by_intf["Ethernet1/1"]
