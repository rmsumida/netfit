"""Unit tests for speed_class_refiner.refine_speed_classes — the post-
analyzer pass that promotes runtime-derived speed classes over the
analyzer's interface-name heuristic.

Tests construct minimal report dicts to keep the refiner exercised in
isolation; the e2e refinement scenario lives in test_pipeline_e2e.py."""
import copy

from speed_class_refiner import refine_speed_classes


def _make_report(details=None, runtime=None):
    return {
        "summary": {},
        "interfaces": {
            "active_physical_by_speed_class": {},
            "details": details or [],
        },
        "runtime": runtime,
    }


def _make_detail(name, intf_type, speed, is_active=True, is_physical=True):
    return {
        "name": name,
        "type": intf_type,
        "is_active": is_active,
        "is_physical": is_physical,
        "effective_speed_class": speed,
    }


def test_refiner_no_runtime_is_noop():
    """Without runtime data, the refiner should leave speed classes
    untouched and just record the inference summary."""
    report = _make_report(
        details=[
            _make_detail("TenGigabitEthernet0/0/1", "TenGigabitEthernet", "10G"),
            _make_detail("GigabitEthernet0/0/0", "GigabitEthernet", "1G"),
        ],
        runtime=None,
    )
    refine_speed_classes(report)
    details = report["interfaces"]["details"]
    assert details[0]["effective_speed_class"] == "10G"
    assert details[0]["effective_speed_class_source"] == "interface_type"
    assert details[1]["effective_speed_class"] == "1G"
    assert report["interfaces"]["active_physical_by_speed_class"] == {"10G": 1, "1G": 1}
    assert report["summary"]["speed_class_inference"] == {
        "by_interface_type": 2, "by_transceiver": 0, "by_operational": 0,
    }


def test_refiner_promotes_transceiver_speed_over_interface_type():
    """A 10G interface running a 1G transceiver should be reclassified to 1G."""
    report = _make_report(
        details=[
            _make_detail("TenGigabitEthernet0/0/1", "TenGigabitEthernet", "10G"),
            _make_detail("TenGigabitEthernet0/0/2", "TenGigabitEthernet", "10G"),
        ],
        runtime={"interfaces": {"transceivers_by_interface": {
            "TenGigabitEthernet0/0/1": {"media_type": "1000BASE-T", "speed_inferred": "1G"},
            "TenGigabitEthernet0/0/2": {"media_type": "10GBASE-LR", "speed_inferred": "10G"},
        }}},
    )
    refine_speed_classes(report)
    details = report["interfaces"]["details"]
    assert details[0]["effective_speed_class"] == "1G"
    assert details[0]["effective_speed_class_source"] == "transceiver"
    assert details[0]["effective_speed_class_original"] == "10G"
    assert details[1]["effective_speed_class"] == "10G"
    assert details[1]["effective_speed_class_source"] == "transceiver"
    # Recomputed counter reflects the refined values.
    assert report["interfaces"]["active_physical_by_speed_class"] == {"1G": 1, "10G": 1}


def test_refiner_falls_back_to_operational_when_transceiver_missing():
    """When transceiver data is unavailable for an interface, fall back to
    bandwidth-derived operational speed."""
    report = _make_report(
        details=[
            _make_detail("TenGigabitEthernet0/0/3", "TenGigabitEthernet", "10G"),
        ],
        runtime={"interfaces": {
            "transceivers_by_interface": {},
            "operational_by_interface": {
                "TenGigabitEthernet0/0/3": {
                    "line_protocol": "up",
                    "bandwidth_kbit": 1000000,
                    "speed_inferred": "1G",
                },
            },
        }},
    )
    refine_speed_classes(report)
    detail = report["interfaces"]["details"][0]
    assert detail["effective_speed_class"] == "1G"
    assert detail["effective_speed_class_source"] == "operational"
    assert report["summary"]["speed_class_inference"]["by_operational"] == 1


def test_refiner_recomputes_active_physical_by_speed_class_only_for_active_physical():
    """Shutdown interfaces and logical interfaces must NOT contribute to
    the rolled-up counter — matches the analyzer's filter at lines 376-388."""
    report = _make_report(
        details=[
            _make_detail("TenGigabitEthernet0/0/1", "TenGigabitEthernet", "10G"),
            # Shutdown — should not count.
            _make_detail("TenGigabitEthernet0/0/2", "TenGigabitEthernet", "10G", is_active=False),
            # Logical — should not count.
            _make_detail("Loopback0", "Loopback", None, is_physical=False),
            _make_detail("GigabitEthernet0/0/0", "GigabitEthernet", "1G"),
        ],
        runtime=None,
    )
    refine_speed_classes(report)
    # Only the two active physical interfaces should appear.
    assert report["interfaces"]["active_physical_by_speed_class"] == {"10G": 1, "1G": 1}
    # And the inference counter should match (only counts active physical).
    counts = report["summary"]["speed_class_inference"]
    assert sum(counts.values()) == 2


def test_refiner_writes_summary_inference_counts():
    """summary.speed_class_inference must sum to the active-physical count
    and track the source mix."""
    report = _make_report(
        details=[
            _make_detail("TenGigabitEthernet0/0/1", "TenGigabitEthernet", "10G"),
            _make_detail("TenGigabitEthernet0/0/2", "TenGigabitEthernet", "10G"),
            _make_detail("GigabitEthernet0/0/0", "GigabitEthernet", "1G"),
        ],
        runtime={"interfaces": {"transceivers_by_interface": {
            "TenGigabitEthernet0/0/1": {"media_type": "1000BASE-T", "speed_inferred": "1G"},
        }}},
    )
    refine_speed_classes(report)
    counts = report["summary"]["speed_class_inference"]
    assert counts["by_transceiver"] == 1
    assert counts["by_operational"] == 0
    assert counts["by_interface_type"] == 2  # the other 10G + the 1G
    assert sum(counts.values()) == 3


def test_refiner_records_source_and_original_per_interface():
    """Each refined detail should preserve the original analyzer call
    alongside the new effective_speed_class_source."""
    report = _make_report(
        details=[
            _make_detail("TenGigabitEthernet0/0/1", "TenGigabitEthernet", "10G"),
        ],
        runtime={"interfaces": {"transceivers_by_interface": {
            "TenGigabitEthernet0/0/1": {"media_type": "1000BASE-T", "speed_inferred": "1G"},
        }}},
    )
    refine_speed_classes(report)
    detail = report["interfaces"]["details"][0]
    assert detail["effective_speed_class"] == "1G"
    assert detail["effective_speed_class_original"] == "10G"
    assert detail["effective_speed_class_source"] == "transceiver"


def test_refiner_is_idempotent():
    """Calling refine twice should produce identical state — useful when
    a runtime re-merge happens (e.g., in --runtime-dir mode)."""
    report = _make_report(
        details=[
            _make_detail("TenGigabitEthernet0/0/1", "TenGigabitEthernet", "10G"),
        ],
        runtime={"interfaces": {"transceivers_by_interface": {
            "TenGigabitEthernet0/0/1": {"media_type": "1000BASE-T", "speed_inferred": "1G"},
        }}},
    )
    refine_speed_classes(report)
    snapshot = copy.deepcopy(report)
    refine_speed_classes(report)
    assert report == snapshot
    # The original is locked in on first refine — second refine doesn't
    # update it to the previously-refined value.
    detail = report["interfaces"]["details"][0]
    assert detail["effective_speed_class_original"] == "10G"


def test_refiner_handles_missing_runtime_interfaces_section_gracefully():
    """Runtime present but with no interfaces section should be a clean no-op."""
    report = _make_report(
        details=[_make_detail("GigabitEthernet0/0/0", "GigabitEthernet", "1G")],
        runtime={"inventory": {"chassis_pid": "ASR1001-X"}},
    )
    refine_speed_classes(report)
    detail = report["interfaces"]["details"][0]
    assert detail["effective_speed_class"] == "1G"
    assert detail["effective_speed_class_source"] == "interface_type"
