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
