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
from speed_class_refiner import refine_speed_classes


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
    report_md = outdir / "report.md"
    report_html = outdir / "report.html"

    rules = load_rules(str(PROJECT_ROOT / "rules.yaml"))
    sanitizer = CiscoConfigSanitizer(rules)
    sanitized.write_text(sanitizer.sanitize(input_config.read_text()))
    mappings.write_text(json.dumps(sanitizer.get_mappings(), indent=2))

    report = analyze_config(str(input_config))
    # Mirror main.py: refine speed classes from runtime data before save.
    # No runtime is merged in this fixture, so the refiner is a no-op
    # except for writing summary.speed_class_inference.
    refine_speed_classes(report)
    save_report(report, str(analysis))

    build_platform_comparison_reports(
        analysis_json_path=str(analysis),
        target_profiles_folder=str(PROJECT_ROOT / "platforms"),
        comparison_json_output=str(cmp_json),
        report_md_output=str(report_md),
        report_html_output=str(report_html),
    )

    return {
        "outdir": outdir,
        "sanitized": sanitized,
        "mappings": mappings,
        "analysis": json.loads(analysis.read_text()),
        "comparison": json.loads(cmp_json.read_text()),
        "markdown": report_md.read_text(),
        "html": report_html.read_text(),
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
    assert comparison["platform_count"] == 5
    names = {r["platform_name"] for r in comparison["results"]}
    assert names == {
        "Cisco_C8500-12X",
        "Cisco_C8500-12X4QC",
        "Cisco_C8500-20X6C",
        "Cisco_C8500L-8S4X",
        "Cisco_N9K-93180YC-FX3",
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

def test_markdown_report_includes_unified_sections_and_all_platforms(pipeline_outputs):
    """The unified report must carry verdict-first structure and surface every
    candidate platform. Scoring is deliberately in the bottom appendix, not
    the top-line table."""
    md = pipeline_outputs["markdown"]
    assert "# Hardware Refresh Report" in md
    assert "## Verdict" in md
    assert "## Source Device Context" in md
    assert "### Routing scale" in md
    assert "### NAT scale" in md
    assert "### IPsec / VPN scale" in md
    assert "## Ranked Candidates" in md
    assert "## Best-Fit Detail" in md
    assert "## Scoring Methodology (Appendix)" in md
    for platform in ("Cisco_C8500-12X", "Cisco_C8500-12X4QC",
                     "Cisco_C8500-20X6C", "Cisco_C8500L-8S4X"):
        assert platform in md, f"Markdown missing platform section: {platform}"


def test_ranked_table_has_no_fitness_column(pipeline_outputs):
    """Regression for report unification: the ranked-candidates table must
    show the verdict label, not the fitness score (scoring moved to appendix).
    If fitness reappears in the top table, the appendix-only boundary has
    regressed."""
    md = pipeline_outputs["markdown"]
    # Locate the Ranked Candidates section's header row.
    ranked_start = md.index("## Ranked Candidates")
    appendix_start = md.index("## Scoring Methodology")
    ranked_block = md[ranked_start:appendix_start]
    # Header row should not contain "Fitness".
    header_line = next(
        (line for line in ranked_block.splitlines() if line.startswith("| Rank")),
        None,
    )
    assert header_line is not None, "Ranked Candidates table missing header row"
    assert "Fitness" not in header_line, (
        f"Fitness column leaked into ranked-candidates table: {header_line!r}. "
        f"Scoring should live only in the bottom appendix."
    )
    # Appendix should still carry the fitness table.
    assert "| Rank | Platform | Fitness |" in md[appendix_start:], (
        "Scoring Methodology appendix should still show per-platform fitness."
    )


def test_no_separate_best_fit_report_file_produced(pipeline_outputs):
    """Report unification: only `report.md` / `report.html` (+ the JSON
    artifact) land in the output directory. No `best_fit_report.*`."""
    outdir = pipeline_outputs["outdir"]
    stale = list(outdir.glob("best_fit_report.*")) + list(outdir.glob("platform_comparison.md")) + list(outdir.glob("platform_comparison.html"))
    assert not stale, f"Stale output files present: {[p.name for p in stale]}"
    assert (outdir / "report.md").exists()
    assert (outdir / "report.html").exists()
    assert (outdir / "platform_comparison.json").exists()


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
    # Unified structure: Verdict is the first H2, Scoring Methodology is the
    # last. If these swap, someone has re-elevated scoring above the verdict.
    verdict_pos = html_out.find("<h2>Verdict</h2>")
    scoring_pos = html_out.find("Scoring Methodology")
    assert 0 < verdict_pos < scoring_pos, (
        "Verdict must appear before Scoring Methodology in the HTML report."
    )


def test_device_context_renders_nat_and_ipsec_scale_sections(pipeline_outputs):
    """Device-context section must call out NAT and IPsec scale explicitly
    — previously only BGP/VRF/static were surfaced, which hid workload
    shape from the reader."""
    md = pipeline_outputs["markdown"]
    assert "### NAT scale" in md
    assert "### IPsec / VPN scale" in md
    # The seeded router config has no runtime harvest merged, so we should
    # see config-derived lines without runtime translation / SA counts.
    nat_block_start = md.index("### NAT scale")
    nat_block_end = md.index("###", nat_block_start + len("### NAT scale"))
    nat_block = md[nat_block_start:nat_block_end]
    assert "NAT configured:" in nat_block


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


# ---------------------------------------------------------------------------
# Combined-harvest mode (issue #14): single NetBrain file containing both the
# running-config and the runtime show commands. Pipeline auto-detects on the
# `#---` delimiter signature and drives the shared-sanitizer sanitize-then-
# parse loop for runtime records.
# ---------------------------------------------------------------------------

COMBINED_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "combined_harvest_minimal.txt"


@pytest.fixture(scope="module")
def combined_outputs(tmp_path_factory):
    from main import process_single_device
    outdir = tmp_path_factory.mktemp("combined_e2e")
    process_single_device(
        COMBINED_FIXTURE,
        outdir,
        PROJECT_ROOT / "rules.yaml",
        PROJECT_ROOT / "platforms",
        quiet=True,
    )
    return {
        "outdir": outdir,
        "analysis": json.loads((outdir / "analysis_report.json").read_text()),
        "mappings": json.loads((outdir / "sanitization_mappings.json").read_text()),
        "sanitized": (outdir / "sanitized_config.txt").read_text(),
        "report_md": (outdir / "report.md").read_text(),
        "report_html": (outdir / "report.html").read_text(),
    }


def test_combined_harvest_e2e_produces_runtime_section(combined_outputs):
    analysis = combined_outputs["analysis"]
    # Config-derived sections intact.
    assert "interfaces" in analysis
    assert "routing" in analysis
    # Runtime sections populated from the same file.
    assert "runtime" in analysis
    runtime = analysis["runtime"]
    assert runtime["harvest_source"] == "netbrain"
    assert runtime["inventory"]["chassis_pid"] == "ASR1001-X"
    assert runtime["nat"]["active_translations"] == 42
    assert runtime["route_table"]["ipv4_total"] == 3


def test_combined_harvest_sanitization_mappings_cover_config_and_runtime(
    combined_outputs,
):
    mappings = combined_outputs["mappings"]
    # Config-side tokens: the operator username from the config.
    assert "usernames" in mappings
    assert "operator-bob" in mappings["usernames"]
    # Runtime-side tokens: chassis + module serials from show inventory.
    assert "serial_numbers" in mappings
    assert "FOX1234ABCD" in mappings["serial_numbers"]
    assert "JAE5678XYZW" in mappings["serial_numbers"]


def test_combined_harvest_runtime_serials_are_tokenized_in_report(combined_outputs):
    runtime = combined_outputs["analysis"]["runtime"]
    # The raw serial strings must not survive into the analysis report.
    raw_serials = ("FOX1234ABCD", "JAE5678XYZW")
    runtime_str = json.dumps(runtime)
    for raw in raw_serials:
        assert raw not in runtime_str, f"Serial {raw} leaked into runtime section"
    # And the chassis serial in the inventory section is the tokenized marker.
    assert "<REDACTED_SERIAL_" in runtime["inventory"]["chassis_serial"]


def test_combined_harvest_report_surfaces_runtime_nat_translations(combined_outputs):
    """When a runtime harvest is merged, the device-context section of the
    unified report should surface the live NAT translation counts alongside
    the config-side flag. The minimal fixture publishes 42 active
    translations, so the string `active=42` must appear in the report."""
    md = combined_outputs["report_md"]
    assert "### NAT scale" in md
    assert "active=42" in md, (
        "Runtime NAT translation count not surfaced in unified report. "
        "`runtime['nat']['active_translations']` is 42 in the fixture — "
        "the device-context renderer should show `active=42`."
    )


def test_combined_harvest_sanitized_file_is_config_body_only(combined_outputs):
    # sanitized_config.txt holds the sanitized running-config body, not the
    # full harvest file — no `#---` headers, no runtime show output.
    sanitized = combined_outputs["sanitized"]
    assert "#---" not in sanitized
    assert "Total active translations" not in sanitized
    # The hostname directive is present (tokenized via the hostname rule).
    assert "hostname" in sanitized


def test_combined_harvest_mutex_with_runtime_csv(tmp_path):
    from main import process_single_device
    with pytest.raises(SystemExit) as excinfo:
        process_single_device(
            COMBINED_FIXTURE,
            tmp_path / "out",
            PROJECT_ROOT / "rules.yaml",
            PROJECT_ROOT / "platforms",
            runtime_csv=COMBINED_FIXTURE,
            quiet=True,
        )
    assert "mutually exclusive" in str(excinfo.value)


def test_combined_harvest_no_running_config_raises(tmp_path):
    from main import process_single_device
    runtime_only = tmp_path / "runtime_only.txt"
    runtime_only.write_text(
        "#--- solo-01 show inventory Execute at 2026-04-21 12:00:00\n"
        "solo-01#show inventory\n"
        "NAME: \"Chassis\", DESCR: \"Cisco ASR1001-X Chassis\"\n"
        "PID: ASR1001-X       , VID: V07 , SN: FOX0000ABCD\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as excinfo:
        process_single_device(
            runtime_only,
            tmp_path / "out",
            PROJECT_ROOT / "rules.yaml",
            PROJECT_ROOT / "platforms",
            quiet=True,
        )
    assert "no `show running-config` block" in str(excinfo.value)


def test_combined_harvest_determinism(tmp_path):
    """Determinism invariant (#14 criterion 12): same input, same rules, same
    platforms → byte-identical analysis_report.json across two runs."""
    from main import process_single_device
    out_a = tmp_path / "run_a"
    out_b = tmp_path / "run_b"
    for outdir in (out_a, out_b):
        process_single_device(
            COMBINED_FIXTURE,
            outdir,
            PROJECT_ROOT / "rules.yaml",
            PROJECT_ROOT / "platforms",
            quiet=True,
        )
    a_bytes = (out_a / "analysis_report.json").read_bytes()
    b_bytes = (out_b / "analysis_report.json").read_bytes()
    assert a_bytes == b_bytes, "analysis_report.json differs between identical runs"
    # Mappings file must also be byte-identical — token IDs are deterministic.
    m_a = (out_a / "sanitization_mappings.json").read_bytes()
    m_b = (out_b / "sanitization_mappings.json").read_bytes()
    assert m_a == m_b, "sanitization_mappings.json differs between identical runs"


def test_two_file_workflow_unchanged(tmp_path):
    """Regression guard: the existing two-file workflow must keep producing
    the same-shape outputs as before. We run it against the native_export
    fixture that existing tests already exercise."""
    from main import process_single_device
    # Build a minimal config file matching the hostname in the native fixture.
    cfg = tmp_path / "rtr-edge-01.cfg"
    cfg.write_text(
        "!\nhostname rtr-edge-01\n!\n"
        "interface GigabitEthernet0/0/0\n"
        " ip address 192.0.2.1 255.255.255.0\n"
        " no shutdown\n"
        "!\nend\n",
        encoding="utf-8",
    )
    process_single_device(
        cfg,
        tmp_path / "out",
        PROJECT_ROOT / "rules.yaml",
        PROJECT_ROOT / "platforms",
        runtime_csv=PROJECT_ROOT / "tests" / "fixtures" / "netbrain" / "native_export.txt",
        quiet=True,
    )
    analysis = json.loads((tmp_path / "out" / "analysis_report.json").read_text())
    assert "runtime" in analysis
    assert analysis["runtime"]["inventory"]["chassis_pid"] == "ASR1013"


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


# ---------------------------------------------------------------------------
# Speed-class refinement (issue #17): runtime data reclassifies demand from
# the analyzer's name-based default (TenGigabitEthernet → 10G) to the
# transceiver-derived value (e.g. a 10G slot running a 1G optic → 1G demand).
# ---------------------------------------------------------------------------

TRANSCEIVER_FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "combined_harvest_with_transceiver.txt"
)


def test_combined_harvest_speed_class_inference_refines_demand(tmp_path):
    """The fixture has 2 active TenGigabitEthernet interfaces; the
    transceiver harvest declares 1× 1000BASE-T and 1× 10GBASE-LR. The
    refiner should reclassify the first to 1G, leaving the rolled-up
    counter at {1G:1, 10G:1} instead of the analyzer's name-based {10G:2}."""
    from main import process_single_device
    process_single_device(
        TRANSCEIVER_FIXTURE,
        tmp_path / "out",
        PROJECT_ROOT / "rules.yaml",
        PROJECT_ROOT / "platforms",
        quiet=True,
    )
    analysis = json.loads((tmp_path / "out" / "analysis_report.json").read_text())

    # Refined per-speed counter — transceiver overrides interface-name default.
    assert analysis["interfaces"]["active_physical_by_speed_class"] == {"1G": 1, "10G": 1}

    # Inference summary populated; both refinements came from transceiver data.
    inference = analysis["summary"]["speed_class_inference"]
    assert inference["by_transceiver"] == 2
    assert inference["by_operational"] == 0
    assert inference["by_interface_type"] == 0

    # Per-interface audit fields preserve the analyzer's original call.
    details = analysis["interfaces"]["details"]
    by_name = {d["name"]: d for d in details}
    assert by_name["TenGigabitEthernet0/0/1"]["effective_speed_class"] == "1G"
    assert by_name["TenGigabitEthernet0/0/1"]["effective_speed_class_source"] == "transceiver"
    assert by_name["TenGigabitEthernet0/0/1"]["effective_speed_class_original"] == "10G"
    assert by_name["TenGigabitEthernet0/0/2"]["effective_speed_class"] == "10G"
    assert by_name["TenGigabitEthernet0/0/2"]["effective_speed_class_source"] == "transceiver"


def test_no_runtime_path_leaves_speed_classes_at_analyzer_defaults(pipeline_outputs):
    """The seeded sample run has no runtime data — the refiner should be a
    no-op and inference should be entirely by_interface_type."""
    analysis = pipeline_outputs["analysis"]
    inference = analysis["summary"].get("speed_class_inference")
    assert inference is not None, "Refiner should always write the inference summary"
    assert inference["by_transceiver"] == 0
    assert inference["by_operational"] == 0
    assert inference["by_interface_type"] == analysis["interfaces"]["active_physical_count"]
    # Every active physical detail should record interface_type as the source.
    for d in analysis["interfaces"]["details"]:
        if d.get("is_active") and d.get("is_physical"):
            assert d["effective_speed_class_source"] == "interface_type"


def test_two_file_workflow_uses_transceiver_data_when_present(tmp_path):
    """The native_export.txt fixture now carries real `show interfaces
    transceiver detail` output. The two-file workflow should pick that up
    and reclassify rtr-edge-01's TenGigabitEthernet0/0/1 to 1G."""
    from main import process_single_device
    cfg = tmp_path / "rtr-edge-01.cfg"
    cfg.write_text(
        "!\nhostname rtr-edge-01\n!\n"
        "interface TenGigabitEthernet0/0/1\n"
        " ip address 192.0.2.1 255.255.255.0\n"
        " no shutdown\n!\n"
        "interface TenGigabitEthernet0/0/2\n"
        " ip address 192.0.2.5 255.255.255.0\n"
        " no shutdown\n!\nend\n",
        encoding="utf-8",
    )
    process_single_device(
        cfg,
        tmp_path / "out",
        PROJECT_ROOT / "rules.yaml",
        PROJECT_ROOT / "platforms",
        runtime_csv=PROJECT_ROOT / "tests" / "fixtures" / "netbrain" / "native_export.txt",
        quiet=True,
    )
    analysis = json.loads((tmp_path / "out" / "analysis_report.json").read_text())
    by_speed = analysis["interfaces"]["active_physical_by_speed_class"]
    # 0/0/1 has a 1000BASE-T optic, 0/0/2 has a 10GBASE-LR optic.
    assert by_speed == {"1G": 1, "10G": 1}
