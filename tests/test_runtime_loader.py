"""Loader tests: native-text splitter, alias normalization, hostname filter,
% Invalid input skip, end-to-end load_runtime_for_device."""
import logging
from pathlib import Path

import pytest

from runtime_loader import (
    ALIAS_MAP,
    INTENT_TARGETS,
    _split_native_export,
    _strip_prompt_echo,
    load_runtime_for_device,
    normalize_command,
)


FIXTURES = Path(__file__).parent / "fixtures" / "netbrain"


# -------------------------
# normalize_command
# -------------------------

def test_normalize_lowercase_and_collapse_whitespace():
    assert normalize_command("Show   Inventory ") == "show inventory"


def test_normalize_strips_pipe_filters():
    assert normalize_command("show ip route summary | include Total") == "show ip route summary"
    assert normalize_command("show processes cpu sorted | exclude 0.00") == "show processes cpu sorted"


# -------------------------
# alias map / intent targets — invariants
# -------------------------

def test_every_alias_has_a_target():
    for cmd, intent in ALIAS_MAP.items():
        assert intent in INTENT_TARGETS, f"{intent!r} (from {cmd!r}) missing INTENT_TARGETS entry"


# -------------------------
# native-export splitter
# -------------------------

def test_split_native_export_recovers_all_blocks():
    text = (FIXTURES / "native_export.txt").read_text(encoding="utf-8")
    records = _split_native_export(text)
    # 8 blocks for rtr-edge-01 (incl. the % Invalid input one — splitter emits
    # everything; loader is what filters) + 1 for rtr-edge-02 = 9.
    assert len(records) == 9
    assert records[0][0] == "rtr-edge-01"
    assert records[0][1] == "show inventory"
    assert records[0][2] == "2026-04-16 17:28:29"
    assert records[-1][0] == "rtr-edge-02"


def test_split_native_export_handles_no_headers():
    assert _split_native_export("just some text\nwith no headers\n") == []


# -------------------------
# prompt echo stripping
# -------------------------

def test_strip_prompt_echo_removes_first_line_prompt():
    body = "rtr-edge-01>show inventory\nNAME: \"Chassis\"\nPID: ASR1013\n"
    out = _strip_prompt_echo(body)
    assert out.startswith("NAME:")


def test_strip_prompt_echo_passes_through_when_no_prompt():
    body = "Total active translations: 4444\n"
    assert _strip_prompt_echo(body) == body


# -------------------------
# load_runtime_for_device — end to end
# -------------------------

def test_load_runtime_returns_none_for_unknown_hostname():
    path = FIXTURES / "native_export.txt"
    assert load_runtime_for_device(path, "no-such-device") is None


def test_load_runtime_case_insensitive_hostname():
    path = FIXTURES / "native_export.txt"
    out = load_runtime_for_device(path, "RTR-EDGE-01")
    assert out is not None


def test_load_runtime_assembles_expected_sections():
    path = FIXTURES / "native_export.txt"
    out = load_runtime_for_device(path, "rtr-edge-01")
    assert out is not None
    assert out["harvest_source"] == "netbrain"
    assert out["harvest_timestamp"] == "2026-04-16 17:28:29"
    assert out["inventory"]["chassis_pid"] == "ASR1013"
    assert out["platform"]["software_version"] == "16.03.07"
    assert out["platform"]["cpu_5min_pct"] == 18
    assert out["route_table"]["ipv4_total"] == 42 + 487602
    assert out["nat"]["active_translations"] == 4444
    assert out["crypto"]["active_sas"] == 2  # 1 inbound + 1 outbound spi in fixture
    assert out["license"]["model"] == "classic"


def test_load_runtime_skips_invalid_input_block(caplog):
    path = FIXTURES / "native_export.txt"
    with caplog.at_level(logging.WARNING, logger="runtime_loader"):
        load_runtime_for_device(path, "rtr-edge-01")
    # The `show interfaces transceiver` block returns "% Invalid input" — it
    # must be skipped with a warning, not parsed.
    assert any("Invalid input" in rec.getMessage() for rec in caplog.records)


def test_load_runtime_filters_to_target_hostname():
    path = FIXTURES / "native_export.txt"
    out2 = load_runtime_for_device(path, "rtr-edge-02")
    assert out2 is not None
    assert out2["inventory"]["chassis_pid"] == "ASR1001-X"
    # rtr-edge-02 only has show inventory in the fixture; other slots absent.
    assert "platform" not in out2
    assert "route_table" not in out2


# -------------------------
# CSV format
# -------------------------

def test_load_runtime_from_csv(tmp_path):
    csv_path = tmp_path / "harvest.csv"
    csv_path.write_text(
        'device_name,command,result,timestamp\n'
        'rtr-edge-01,show inventory,'
        '"NAME: ""Chassis"", DESCR: ""Cisco ASR1013 Chassis""\n'
        'PID: ASR1013         , VID: V01 , SN: XOX1234ABCD",'
        '2026-04-16 17:28:29\n'
        'rtr-edge-01,show ip nat statistics,'
        '"Total active translations: 9999 (0 static, 9999 dynamic; 0 extended)",'
        '2026-04-16 17:28:30\n',
        encoding="utf-8",
    )
    out = load_runtime_for_device(csv_path, "rtr-edge-01")
    assert out is not None
    assert out["inventory"]["chassis_pid"] == "ASR1013"
    assert out["nat"]["active_translations"] == 9999
    assert out["harvest_timestamp"] == "2026-04-16 17:28:30"


def test_load_runtime_csv_accepts_alternative_column_names(tmp_path):
    csv_path = tmp_path / "harvest.csv"
    csv_path.write_text(
        'hostname,cmd,output\n'
        'rtr-edge-01,show ip nat statistics,'
        '"Total active translations: 1234 (0 static, 1234 dynamic; 0 extended)"\n',
        encoding="utf-8",
    )
    out = load_runtime_for_device(csv_path, "rtr-edge-01")
    assert out is not None
    assert out["nat"]["active_translations"] == 1234
