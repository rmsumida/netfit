"""Loader tests: native-text splitter, alias normalization, hostname filter,
% Invalid input skip, end-to-end load_runtime_for_device."""
import logging
from pathlib import Path

import pytest

from runtime_loader import (
    ALIAS_MAP,
    INTENT_TARGETS,
    PSEUDO_INTENTS,
    _split_native_export,
    _strip_prompt_echo,
    assemble_runtime_from_records,
    is_combined_harvest,
    load_runtime_for_device,
    normalize_command,
    split_combined_harvest,
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
        if intent in PSEUDO_INTENTS:
            # Pseudo-intents (running_config, startup_config) are routed by
            # split_combined_harvest rather than the INTENT_PARSERS dispatch;
            # they intentionally have no INTENT_TARGETS entry.
            continue
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


# -------------------------
# combined-harvest detection + splitter (issue #14)
# -------------------------

def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


COMBINED_BASIC = """\
#--- rtr-combo-01 show running-config Execute at 2026-04-20 12:00:00
rtr-combo-01#show running-config
!
hostname rtr-combo-01
!
interface GigabitEthernet0/0
 ip address 10.0.0.1 255.255.255.0
!
end

#--- rtr-combo-01 show inventory Execute at 2026-04-20 12:00:01
rtr-combo-01#show inventory
NAME: "Chassis", DESCR: "Cisco ASR1001-X Chassis"
PID: ASR1001-X       , VID: V07 , SN: FOX1234ABCD

#--- rtr-combo-01 show ip nat statistics Execute at 2026-04-20 12:00:02
rtr-combo-01#show ip nat statistics
Total active translations: 55 (0 static, 55 dynamic; 0 extended)
"""


def test_is_combined_harvest_detects_hash_delimiter(tmp_path):
    positive = _write(tmp_path, "combined.txt", COMBINED_BASIC)
    assert is_combined_harvest(positive) is True


def test_is_combined_harvest_rejects_plain_config(tmp_path):
    plain = _write(
        tmp_path, "plain.cfg",
        "!\nhostname router\n!\ninterface Gi0/0\n ip address 1.1.1.1 255.255.255.0\n",
    )
    assert is_combined_harvest(plain) is False


def test_is_combined_harvest_rejects_empty_file(tmp_path):
    empty = _write(tmp_path, "empty.txt", "")
    assert is_combined_harvest(empty) is False


def test_is_combined_harvest_rejects_csv(tmp_path):
    csv_path = _write(
        tmp_path, "harvest.csv",
        "device_name,command,result\nrtr,show inventory,NAME\n",
    )
    assert is_combined_harvest(csv_path) is False


def test_is_combined_harvest_skips_leading_blank_lines(tmp_path):
    p = _write(tmp_path, "combo.txt", "\n\n" + COMBINED_BASIC)
    assert is_combined_harvest(p) is True


def test_split_combined_harvest_extracts_config(tmp_path):
    p = _write(tmp_path, "combined.txt", COMBINED_BASIC)
    config_text, runtime_records, hostname = split_combined_harvest(p)
    assert hostname == "rtr-combo-01"
    assert config_text is not None
    assert "hostname rtr-combo-01" in config_text
    assert "interface GigabitEthernet0/0" in config_text
    # Runtime records: inventory + nat. Order preserved; command text raw.
    intents = [intent for intent, _body, _cmd, _ts in runtime_records]
    assert intents == ["inventory", "nat_statistics"]


def test_split_combined_harvest_strips_prompt_echo(tmp_path):
    p = _write(tmp_path, "combined.txt", COMBINED_BASIC)
    config_text, _runtime, _host = split_combined_harvest(p)
    # The first non-empty line of the running-config body was
    # `rtr-combo-01#show running-config` — it must be gone.
    assert "show running-config" not in config_text
    assert config_text.lstrip().startswith("!")


def test_split_combined_harvest_runtime_bodies_ready_for_dispatch(tmp_path):
    p = _write(tmp_path, "combined.txt", COMBINED_BASIC)
    _cfg, runtime_records, _host = split_combined_harvest(p)
    runtime = assemble_runtime_from_records(runtime_records)
    assert runtime["inventory"]["chassis_pid"] == "ASR1001-X"
    assert runtime["nat"]["active_translations"] == 55


COMBINED_WITH_STARTUP = """\
#--- rtr-dual-01 show startup-config Execute at 2026-04-20 12:00:00
rtr-dual-01#show startup-config
!
hostname rtr-dual-01
!
interface GigabitEthernet0/0
 description STARTUP-ONLY
!
end

#--- rtr-dual-01 show running-config Execute at 2026-04-20 12:00:01
rtr-dual-01#show running-config
!
hostname rtr-dual-01
!
interface GigabitEthernet0/0
 description RUNNING
!
end
"""


def test_split_combined_harvest_prefers_running_over_startup(tmp_path, caplog):
    p = _write(tmp_path, "dual.txt", COMBINED_WITH_STARTUP)
    with caplog.at_level(logging.INFO, logger="runtime_loader"):
        config_text, runtime_records, hostname = split_combined_harvest(p)
    assert hostname == "rtr-dual-01"
    assert "description RUNNING" in config_text
    assert "STARTUP-ONLY" not in config_text
    assert runtime_records == []
    assert any(
        "Dropping startup-config" in rec.getMessage()
        for rec in caplog.records
    )


ONLY_RUNTIME = """\
#--- rtr-nocfg-01 show inventory Execute at 2026-04-20 12:00:00
rtr-nocfg-01#show inventory
NAME: "Chassis", DESCR: "Cisco ASR1001-X Chassis"
PID: ASR1001-X       , VID: V07 , SN: FOX1234ABCD
"""


def test_split_combined_harvest_no_running_config_returns_none(tmp_path):
    p = _write(tmp_path, "runtime_only.txt", ONLY_RUNTIME)
    assert split_combined_harvest(p) == (None, None, None)


MULTI_DEVICE = """\
#--- rtr-a show running-config Execute at 2026-04-20 12:00:00
rtr-a#show running-config
hostname rtr-a
end

#--- rtr-b show inventory Execute at 2026-04-20 12:00:01
rtr-b#show inventory
NAME: "Chassis", DESCR: "Cisco ASR Chassis"
PID: ASR1001-X       , VID: V07 , SN: FOX9999ABCD
"""


def test_split_combined_harvest_multi_device_raises(tmp_path):
    p = _write(tmp_path, "multi.txt", MULTI_DEVICE)
    with pytest.raises(ValueError) as excinfo:
        split_combined_harvest(p)
    msg = str(excinfo.value)
    assert "rtr-a" in msg
    assert "rtr-b" in msg
    assert "2 devices" in msg


HOSTNAME_MISMATCH = """\
#--- router-a show running-config Execute at 2026-04-20 12:00:00
router-a#show running-config
!
hostname router-b
!
end
"""


def test_split_combined_harvest_warns_on_hostname_mismatch(tmp_path, caplog):
    p = _write(tmp_path, "mismatch.txt", HOSTNAME_MISMATCH)
    with caplog.at_level(logging.WARNING, logger="runtime_loader"):
        _cfg, _runtime, hostname = split_combined_harvest(p)
    # Device field wins — it matches the rest of the harvest.
    assert hostname == "router-a"
    assert any(
        "Hostname mismatch" in rec.getMessage()
        for rec in caplog.records
    )


def test_split_combined_harvest_skips_invalid_input_record(tmp_path, caplog):
    fixture = (
        "#--- rtr-x show running-config Execute at 2026-04-20 12:00:00\n"
        "rtr-x#show running-config\n"
        "hostname rtr-x\n"
        "end\n"
        "\n"
        "#--- rtr-x show license summary Execute at 2026-04-20 12:00:01\n"
        "rtr-x#show license summary\n"
        "                    ^\n"
        "% Invalid input detected at '^' marker.\n"
    )
    p = _write(tmp_path, "invalid_input.txt", fixture)
    with caplog.at_level(logging.WARNING, logger="runtime_loader"):
        _cfg, runtime_records, _host = split_combined_harvest(p)
    assert runtime_records == []
    assert any(
        "Invalid input" in rec.getMessage() for rec in caplog.records
    )


def test_split_combined_harvest_accepts_explicit_hostname(tmp_path):
    p = _write(tmp_path, "combined.txt", COMBINED_BASIC)
    cfg, _runtime, host = split_combined_harvest(p, hostname="rtr-combo-01")
    assert host == "rtr-combo-01"
    assert cfg is not None


def test_two_file_runtime_loader_skips_running_config_record(tmp_path, caplog):
    # Regression guard: if a user accidentally feeds a combined-harvest file
    # through --runtime-csv (two-file workflow), the running_config pseudo-
    # intent must be skipped, not crash INTENT_PARSERS dispatch.
    p = _write(tmp_path, "combined.txt", COMBINED_BASIC)
    with caplog.at_level(logging.INFO, logger="runtime_loader"):
        out = load_runtime_for_device(p, "rtr-combo-01")
    assert out is not None
    assert out["inventory"]["chassis_pid"] == "ASR1001-X"
    assert any(
        "combined-harvest" in rec.getMessage()
        for rec in caplog.records
    )
