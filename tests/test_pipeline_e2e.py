"""End-to-end smoke test for the full sanitize → analyze → compare pipeline.

Runs `main()` against the seeded `input/router_config.txt` fixture, writes
outputs to a tmp dir, and asserts the final JSON has the expected shape and
expected workload numbers. This is a regression net: if a refactor silently
breaks field plumbing between the pipeline stages, this test catches it.
"""
import json
from pathlib import Path

import pytest

from sanitizer import CiscoConfigSanitizer, load_rules
from analyzer import analyze_config, save_report
from platform_compare import build_platform_comparison_reports


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pipeline_outputs(tmp_path_factory):
    """Run the full pipeline once per test module against the seeded input."""
    input_config = PROJECT_ROOT / "input" / "router_config.txt"
    if not input_config.exists():
        pytest.skip(f"Seed input {input_config} not present")

    outdir = tmp_path_factory.mktemp("pipeline_out")

    sanitized = outdir / "sanitized_config.txt"
    mappings = outdir / "sanitization_mappings.json"
    analysis = outdir / "analysis_report.json"
    cmp_json = outdir / "platform_comparison.json"
    cmp_md = outdir / "platform_comparison.md"
    cmp_html = outdir / "platform_comparison.html"

    rules = load_rules(str(PROJECT_ROOT / "rules.yaml"))
    sanitizer = CiscoConfigSanitizer(rules)
    sanitized.write_text(sanitizer.sanitize(input_config.read_text()))
    mappings.write_text(json.dumps(sanitizer.get_mappings(), indent=2))

    report = analyze_config(str(input_config))
    save_report(report, str(analysis))

    build_platform_comparison_reports(
        analysis_json_path=str(analysis),
        target_profiles_folder=str(PROJECT_ROOT / "platforms"),
        comparison_json_output=str(cmp_json),
        comparison_md_output=str(cmp_md),
        comparison_html_output=str(cmp_html),
    )

    return {
        "outdir": outdir,
        "sanitized": sanitized,
        "mappings": mappings,
        "analysis": json.loads(analysis.read_text()),
        "comparison": json.loads(cmp_json.read_text()),
        "markdown": cmp_md.read_text(),
        "html": cmp_html.read_text(),
    }


# ---------------------------------------------------------------------------
# Sanitizer stage
# ---------------------------------------------------------------------------

def test_sanitized_output_has_no_leaked_ipv4(pipeline_outputs):
    sanitized = pipeline_outputs["sanitized"].read_text()
    # Any surviving non-trivial IPv4 literal is a leak. Tokens like IP_123 are expected.
    import re
    leaked = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", sanitized)
    # 0.0.0.0 is preserved by design (default route / any-source idiom).
    leaked = [ip for ip in leaked if ip != "0.0.0.0"]
    assert not leaked, f"Found {len(leaked)} unsanitized IPv4 literal(s): {leaked[:5]}"


def test_sanitized_output_has_no_leaked_hex_keys(pipeline_outputs):
    """Regression for the encryption-type capture bug that left hex key
    material visible after 'key 7' lines."""
    import re
    sanitized = pipeline_outputs["sanitized"].read_text()
    # Look for ' key <REDACTED_*> <long-hex>' — the exact shape of the old leak.
    leaks = re.findall(r"key <REDACTED_\w+>\s+([A-Fa-f0-9]{20,})", sanitized)
    assert not leaks, f"Hex key material leaked next to redaction marker: {leaks[:3]}"


# ---------------------------------------------------------------------------
# Analyzer stage
# ---------------------------------------------------------------------------

def test_analysis_has_expected_top_level_sections(pipeline_outputs):
    analysis = pipeline_outputs["analysis"]
    expected_sections = {
        "summary", "inventory", "interfaces", "switching", "routing",
        "high_availability", "security", "services", "policy",
        "management_plane", "crypto_vpn",
    }
    missing = expected_sections - set(analysis)
    assert not missing, f"Analysis JSON missing sections: {missing}"


def test_analysis_interface_counts_are_internally_consistent(pipeline_outputs):
    interfaces = pipeline_outputs["analysis"]["interfaces"]
    assert interfaces["active_total"] <= interfaces["total"]
    assert interfaces["active_physical_count"] <= interfaces["active_total"]
    # Every active-by-type entry must also be in the overall by_type map.
    for intf_type in interfaces.get("active_physical_by_type", {}):
        assert intf_type in interfaces["by_type"], (
            f"Active type {intf_type} not in overall by_type map"
        )


def test_analysis_expected_workload_baseline(pipeline_outputs):
    """Known-good workload numbers for the committed sample config. If these
    drift, either the analyzer changed behavior or the input changed —
    either way, explain-or-update."""
    interfaces = pipeline_outputs["analysis"]["interfaces"]
    assert interfaces["active_physical_count"] == 21
    assert interfaces["active_subinterfaces"] == 49
    by_type = interfaces["active_physical_by_type"]
    assert by_type.get("GigabitEthernet") == 13
    assert by_type.get("TenGigabitEthernet") == 8


# ---------------------------------------------------------------------------
# Platform comparison stage
# ---------------------------------------------------------------------------

def test_comparison_has_all_platforms(pipeline_outputs):
    comparison = pipeline_outputs["comparison"]
    assert comparison["platform_count"] == 4
    names = {r["platform_name"] for r in comparison["results"]}
    assert names == {
        "Cisco_C8500-12X",
        "Cisco_C8500-12X4QC",
        "Cisco_C8500-20X6C",
        "Cisco_C8500L-8S4X",
    }


def test_comparison_best_fit_is_20x6c(pipeline_outputs):
    """The sample workload (21 active physical, mix of 1G/10G) should favor
    the platform with the most native port capacity, which is C8500-20X6C
    (26 physical ports native). If this ever flips, the scoring logic
    changed — verify deliberately."""
    comparison = pipeline_outputs["comparison"]
    assert comparison["top_ranked_platform"] == "Cisco_C8500-20X6C"


def test_comparison_results_are_sorted_by_fitness(pipeline_outputs):
    scores = [r["fitness_score"] for r in pipeline_outputs["comparison"]["results"]]
    assert scores == sorted(scores, reverse=True), (
        f"Results not sorted by fitness descending: {scores}"
    )


def test_comparison_result_has_structured_interface_comparison(pipeline_outputs):
    """Each per-platform result must carry the interface_comparison block so
    downstream reporting has a stable schema to work from."""
    for result in pipeline_outputs["comparison"]["results"]:
        ic = result.get("interface_comparison")
        assert ic is not None, f"Missing interface_comparison on {result['platform_name']}"
        for key in ("physical_capacity_ok", "subinterface_capacity_ok",
                    "speed_match_ok", "management_fit",
                    "source_active_physical_by_speed_class",
                    "target_native_supply_by_speed_class"):
            assert key in ic, f"interface_comparison missing {key}"


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def test_markdown_report_includes_device_context_and_all_platforms(pipeline_outputs):
    md = pipeline_outputs["markdown"]
    assert "# Multi-Platform Refresh Comparison" in md
    assert "## Source Device Context" in md
    assert "## Ranked Comparison Table" in md
    assert "## Per-Platform Detail" in md
    for platform in ("Cisco_C8500-12X", "Cisco_C8500-12X4QC",
                     "Cisco_C8500-20X6C", "Cisco_C8500L-8S4X"):
        assert platform in md, f"Markdown missing platform section: {platform}"


def test_html_report_is_real_html_not_pre_wrapped_markdown(pipeline_outputs):
    """Regression for the old bug where HTML was just <pre>{markdown}</pre>."""
    html_out = pipeline_outputs["html"]
    assert html_out.startswith("<!DOCTYPE html>")
    assert "<style>" in html_out
    assert "<table>" in html_out
    # Severity/recommendation badges must render as real CSS classes, not
    # literal markdown-style icons.
    assert 'class="rec' in html_out or 'class="badge' in html_out
    # Must NOT be the old '<pre>-wrapped markdown' shape.
    assert "<pre>" not in html_out[:2000]


# ---------------------------------------------------------------------------
# Assessor calibration: the fix in D4 means the winning platform should now
# earn a real LIKELY_FIT / CONDITIONAL_FIT, not a consolation "top-ranked
# despite NOT_RECOMMENDED" verdict.
# ---------------------------------------------------------------------------

def test_best_fit_platform_earns_actual_recommendation(pipeline_outputs):
    """The sample workload has 21 active physical interfaces (13 1G + 8 10G).
    C8500-20X6C has 26 native physical ports (20×1G + 6×10G). With the D4
    field-alignment fix, the assessor should see the workload fits and stop
    counting shutdown Serial interfaces against it."""
    comparison = pipeline_outputs["comparison"]
    best_fit = next(
        r for r in comparison["results"]
        if r["platform_name"] == comparison["best_fit_platform"]
    )
    rec = best_fit["assessment"]["assessment_summary"]["overall_recommendation"]
    assert rec in ("LIKELY_FIT", "CONDITIONAL_FIT"), (
        f"Best-fit platform should be recommended, got {rec!r}. "
        f"Before D4 every platform was NOT_RECOMMENDED because the assessor "
        f"was counting all 138 interfaces (including shutdown/logical) rather "
        f"than the 21 active physical ones."
    )
    assert comparison["recommended_platform"] is not None


def test_undersized_platforms_still_flag_interface_overflow(pipeline_outputs):
    """The fix must not paper over genuine capacity problems — the 12-port
    C8500 variants genuinely cannot host 21 active physical interfaces and
    should continue to fail scale checks."""
    comparison = pipeline_outputs["comparison"]
    undersized = [r for r in comparison["results"]
                  if r["platform_name"] in ("Cisco_C8500-12X", "Cisco_C8500L-8S4X")]
    for r in undersized:
        findings = r["assessment"]["findings"]
        interface_overflow = [
            f for f in findings
            if "active physical interface count exceeds" in f["title"].lower()
        ]
        assert interface_overflow, (
            f"{r['platform_name']} should still flag physical interface overflow "
            f"(21 active > 12 max). Findings: {[f['title'] for f in findings]}"
        )


def test_spanning_tree_not_flagged_for_router_only_config(pipeline_outputs):
    """Regression for the analyzer bug where `spanning-tree extend system-id`
    (a universal IOS default) caused every WAN router to register as having
    STP enabled, which then failed the STP-support check on every non-L2
    target platform."""
    analysis = pipeline_outputs["analysis"]
    stp = analysis["switching"]["spanning_tree"]
    assert stp["present"] is False, (
        "Spanning-tree should not be reported as 'present' for a pure WAN "
        "router config. The only STP line in the sample is "
        "'spanning-tree extend system-id' which is a default directive."
    )
