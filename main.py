"""CLI entry point for the hardware-refresh config analysis pipeline.

Single-device mode (default):
    python main.py input/router_config.txt

Batch mode (directory of configs):
    python main.py configs/

Outputs land under the output directory, one subfolder per device in batch
mode, plus a cross-device roll-up summary.
"""
import argparse
import json
import sys
from pathlib import Path

from sanitizer import CiscoConfigSanitizer, load_rules
from analyzer import analyze_config, save_report
from platform_compare import build_platform_comparison_reports
from runtime_loader import (
    assemble_runtime_from_records,
    is_combined_harvest,
    load_runtime_for_device,
    split_combined_harvest,
)


CONFIG_EXTENSIONS = ("*.txt", "*.cfg", "*.conf")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="netfit",
        description="Analyze a network-device config and rank candidate replacement platforms for a hardware refresh. (Current input support: Cisco IOS / IOS-XE.)",
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Path to a single config file, OR a directory of config files for batch mode.",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("output"),
        help="Output directory (default: output/).",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path("rules.yaml"),
        help="Sanitization rules YAML (default: rules.yaml).",
    )
    parser.add_argument(
        "--platforms",
        type=Path,
        default=Path("platforms"),
        help="Directory of candidate platform YAML profiles (default: platforms/).",
    )
    parser.add_argument(
        "--analyze-sanitized",
        action="store_true",
        help="Run the analyzer against the sanitized config (default: analyze the "
             "original for parser fidelity; sanitized artifacts are still emitted).",
    )
    parser.add_argument(
        "--no-sanitize",
        action="store_true",
        help="Skip sanitization entirely (input is treated as already-clean).",
    )
    runtime_group = parser.add_mutually_exclusive_group()
    runtime_group.add_argument(
        "--runtime-csv",
        type=Path,
        default=None,
        help="Path to a NetBrain harvest export (CSV or native text). The "
             "loader filters records by each device's hostname.",
    )
    runtime_group.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="Directory of NetBrain harvest exports. For each device, every "
             "file is tried until one yields runtime data for the hostname.",
    )
    return parser.parse_args(argv)


def _validate_args(args):
    if not args.input.exists():
        raise FileNotFoundError(f"Input path does not exist: {args.input}")
    if not args.rules.exists():
        raise FileNotFoundError(f"Rules file not found: {args.rules}")
    if not args.platforms.exists():
        raise FileNotFoundError(f"Platforms directory not found: {args.platforms}")
    platform_files = list(args.platforms.glob("*.yaml")) + list(args.platforms.glob("*.yml"))
    if not platform_files:
        raise FileNotFoundError(f"No platform YAML profiles in {args.platforms}")
    if args.runtime_csv is not None:
        if not args.runtime_csv.exists():
            raise FileNotFoundError(f"Runtime export not found: {args.runtime_csv}")
        if args.runtime_csv.is_dir():
            raise IsADirectoryError(
                f"--runtime-csv expects a file, got a directory: {args.runtime_csv}. "
                f"Use --runtime-dir instead."
            )
    if args.runtime_dir is not None:
        if not args.runtime_dir.exists():
            raise FileNotFoundError(f"Runtime directory not found: {args.runtime_dir}")
        if not args.runtime_dir.is_dir():
            raise NotADirectoryError(
                f"--runtime-dir expects a directory, got a file: {args.runtime_dir}. "
                f"Use --runtime-csv instead."
            )


def _discover_batch_inputs(directory):
    found = []
    for pattern in CONFIG_EXTENSIONS:
        found.extend(sorted(directory.glob(pattern)))
    # Dedupe while preserving order.
    seen = set()
    unique = []
    for path in found:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _resolve_runtime_for(hostname, runtime_csv=None, runtime_dir=None):
    """Look up runtime data for `hostname` from one of the harvest sources.

    Returns the runtime dict or None. With --runtime-dir, files are tried in
    sorted order; the first file that yields a non-None match wins.
    """
    if runtime_csv is not None:
        return load_runtime_for_device(runtime_csv, hostname)
    if runtime_dir is not None:
        for path in sorted(Path(runtime_dir).iterdir()):
            if not path.is_file():
                continue
            out = load_runtime_for_device(path, hostname)
            if out is not None:
                return out
    return None


def process_single_device(input_file, output_dir, rules_path, platforms_dir,
                          *, analyze_sanitized=False, no_sanitize=False, quiet=False,
                          runtime_csv=None, runtime_dir=None):
    """Run the full pipeline against a single config file.

    Returns a dict with keys: `device_name`, `output_dir`, `comparison`
    (parsed platform_comparison.json content).
    """
    # Combined-harvest auto-detection. If the input file's first non-empty
    # line is a NetBrain `#---` delimiter, treat it as a combined harvest —
    # the running-config body feeds the normal config pipeline and all other
    # records feed the runtime pipeline via the shared sanitizer instance.
    combined_mode = is_combined_harvest(input_file)
    if combined_mode and (runtime_csv or runtime_dir):
        raise SystemExit(
            f"Combined-harvest input detected at {input_file}, but --runtime-csv"
            f"/--runtime-dir was also provided. These are mutually exclusive — "
            f"combined harvests carry both config and runtime in one file. Drop "
            f"the runtime flag or use a non-combined config file."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sanitized_file = output_dir / "sanitized_config.txt"
    mappings_file = output_dir / "sanitization_mappings.json"
    report_file = output_dir / "analysis_report.json"
    comparison_json = output_dir / "platform_comparison.json"
    comparison_md = output_dir / "platform_comparison.md"
    comparison_html = output_dir / "platform_comparison.html"
    best_fit_md = output_dir / "best_fit_report.md"
    best_fit_html = output_dir / "best_fit_report.html"

    combined_runtime_records = None
    combined_hostname = None
    if combined_mode:
        config_text, combined_runtime_records, combined_hostname = (
            split_combined_harvest(input_file)
        )
        if config_text is None:
            raise SystemExit(
                f"Combined harvest at {input_file} contains no `show running-"
                f"config` block. Either harvest a running-config and re-run, or "
                f"use the two-file workflow with --runtime-csv."
            )
    else:
        config_text = input_file.read_text(encoding="utf-8", errors="ignore")

    sanitizer = None
    if no_sanitize:
        sanitized_file.write_text(config_text, encoding="utf-8")
        mappings_file.write_text("{}", encoding="utf-8")
    else:
        rules = load_rules(str(rules_path))
        sanitizer = CiscoConfigSanitizer(rules)
        sanitized_file.write_text(sanitizer.sanitize(config_text), encoding="utf-8")

    if combined_mode:
        # Analyzer always reads config text from disk; in combined-harvest
        # mode the "original" input file isn't a bare config, so fall back
        # to the extracted config_text either way. --analyze-sanitized keeps
        # the sanitized-text path; otherwise we write an unsanitized copy
        # so the analyzer can parse genuine identifiers.
        if analyze_sanitized:
            analysis_source = sanitized_file
        else:
            analysis_source = output_dir / "extracted_config.txt"
            analysis_source.write_text(config_text, encoding="utf-8")
    else:
        analysis_source = sanitized_file if analyze_sanitized else input_file
    report = analyze_config(str(analysis_source))
    hostname = (
        combined_hostname
        or report.get("inventory", {}).get("hostname")
        or input_file.stem
    )

    if combined_mode:
        # Drive the per-record sanitize-then-parse loop from here so the
        # shared sanitizer tokenizes runtime IPs, serials, and UDIs into the
        # same mappings file as the config. In --no-sanitize mode the
        # sanitizer is None and bodies pass through verbatim.
        body_transform = sanitizer.sanitize if sanitizer is not None else None
        runtime = assemble_runtime_from_records(
            combined_runtime_records, body_transform=body_transform
        )
        report["runtime"] = runtime
        if not quiet:
            print(
                f"[{input_file.name}] combined-harvest: config + "
                f"{len(combined_runtime_records)} runtime record(s) merged "
                f"for {hostname}"
            )
    else:
        runtime = _resolve_runtime_for(hostname, runtime_csv, runtime_dir)
        if runtime is not None:
            report["runtime"] = runtime
            if not quiet:
                print(f"[{input_file.name}] runtime data merged for {hostname}")
        elif (runtime_csv or runtime_dir) and not quiet:
            print(f"[{input_file.name}] no runtime records for {hostname}", file=sys.stderr)

    if sanitizer is not None:
        mappings_file.write_text(
            json.dumps(sanitizer.get_mappings(), indent=2), encoding="utf-8"
        )

    save_report(report, str(report_file))

    comparison = build_platform_comparison_reports(
        analysis_json_path=str(report_file),
        target_profiles_folder=str(platforms_dir),
        comparison_json_output=str(comparison_json),
        comparison_md_output=str(comparison_md),
        comparison_html_output=str(comparison_html),
        best_fit_md_output=str(best_fit_md),
        best_fit_html_output=str(best_fit_html),
    )

    if not quiet:
        print(f"[{input_file.name}] analysis → {analysis_source}")
        print(f"[{input_file.name}] best-fit: {comparison.get('best_fit_platform')} "
              f"(recommended={comparison.get('recommended_platform')})")

    return {
        "device_name": report.get("summary", {}).get("hostname") or input_file.stem,
        "input_file": str(input_file),
        "output_dir": str(output_dir),
        "comparison": comparison,
    }


def run_batch(input_dir, output_dir, rules_path, platforms_dir,
              *, analyze_sanitized=False, no_sanitize=False,
              runtime_csv=None, runtime_dir=None):
    """Process every config file in `input_dir` and emit a roll-up summary."""
    inputs = _discover_batch_inputs(input_dir)
    if not inputs:
        raise FileNotFoundError(
            f"No config files (*.txt, *.cfg, *.conf) found in {input_dir}"
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    device_results = []
    for path in inputs:
        device_out = output_dir / path.stem
        try:
            result = process_single_device(
                path, device_out, rules_path, platforms_dir,
                analyze_sanitized=analyze_sanitized,
                no_sanitize=no_sanitize,
                quiet=True,
                runtime_csv=runtime_csv,
                runtime_dir=runtime_dir,
            )
        except Exception as exc:
            print(f"[{path.name}] FAILED: {exc}", file=sys.stderr)
            device_results.append({
                "device_name": path.stem,
                "input_file": str(path),
                "output_dir": str(device_out),
                "error": str(exc),
            })
            continue
        device_results.append(result)
        comparison = result["comparison"]
        print(f"[{path.name}] best-fit={comparison.get('best_fit_platform')} "
              f"recommended={comparison.get('recommended_platform')}")

    summary = _build_batch_summary(device_results)
    (output_dir / "_batch_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (output_dir / "_batch_summary.md").write_text(
        _render_batch_markdown(summary), encoding="utf-8"
    )
    return summary


def _build_batch_summary(device_results):
    """Roll up per-device comparisons into a cross-device matrix.

    Produces:
      devices: one row per device with best-fit, recommendation, key counts.
      platform_fit_matrix: dict of platform → dict of device → fitness score.
    """
    devices = []
    platform_fit_matrix = {}

    for r in device_results:
        if "error" in r:
            devices.append({
                "device_name": r["device_name"],
                "input_file": r["input_file"],
                "output_dir": r["output_dir"],
                "error": r["error"],
            })
            continue

        comparison = r["comparison"]
        devices.append({
            "device_name": r["device_name"],
            "input_file": r["input_file"],
            "output_dir": r["output_dir"],
            "top_ranked_platform": comparison.get("top_ranked_platform"),
            "recommended_platform": comparison.get("recommended_platform"),
            "best_fit_platform": comparison.get("best_fit_platform"),
        })
        for result in comparison.get("results", []):
            platform = result["platform_name"]
            platform_fit_matrix.setdefault(platform, {})[r["device_name"]] = {
                "fitness_score": result["fitness_score"],
                "overall_recommendation": (
                    result.get("assessment", {})
                          .get("assessment_summary", {})
                          .get("overall_recommendation")
                ),
            }

    return {
        "device_count": len(device_results),
        "successful": sum(1 for d in devices if "error" not in d),
        "failed": sum(1 for d in devices if "error" in d),
        "devices": devices,
        "platform_fit_matrix": platform_fit_matrix,
    }


def _render_batch_markdown(summary):
    devices = summary.get("devices", [])
    matrix = summary.get("platform_fit_matrix", {})

    lines = ["# Batch Refresh Comparison", ""]
    lines.append(
        f"**{summary['successful']}** of **{summary['device_count']}** devices processed successfully."
    )
    if summary["failed"]:
        lines.append(f"**{summary['failed']}** devices failed — see entries with `error` field.")
    lines.append("")

    # Per-device summary.
    lines.append("## Per-Device Best Fit")
    lines.append("")
    lines.append("| Device | Top-Ranked | Recommended | Output |")
    lines.append("|--------|-----------|-------------|--------|")
    for d in devices:
        if "error" in d:
            lines.append(f"| {d['device_name']} | — | — | ERROR: {d['error']} |")
            continue
        recommended = d.get("recommended_platform") or "_none_"
        lines.append(
            f"| {d['device_name']} | "
            f"{d.get('top_ranked_platform', '—')} | "
            f"{recommended} | "
            f"`{d['output_dir']}` |"
        )
    lines.append("")

    # Platform-fit matrix: rows = platforms, cols = devices, cells = fitness score.
    if matrix:
        device_names = sorted({
            dev for per_device in matrix.values() for dev in per_device
        })
        lines.append("## Platform-Fit Matrix")
        lines.append("")
        lines.append("Fitness scores (higher is better) across all devices.")
        lines.append("")
        header = "| Platform | " + " | ".join(device_names) + " |"
        sep = "|----------|" + "|".join(["---"] * len(device_names)) + "|"
        lines.append(header)
        lines.append(sep)
        for platform in sorted(matrix):
            cells = []
            for device in device_names:
                cell = matrix[platform].get(device)
                cells.append(f"{cell['fitness_score']:.0f}" if cell else "—")
            lines.append(f"| {platform} | " + " | ".join(cells) + " |")

    return "\n".join(lines) + "\n"


def main(argv=None):
    args = _parse_args(argv)
    _validate_args(args)

    if args.input.is_dir():
        run_batch(
            args.input, args.output, args.rules, args.platforms,
            analyze_sanitized=args.analyze_sanitized,
            no_sanitize=args.no_sanitize,
            runtime_csv=args.runtime_csv,
            runtime_dir=args.runtime_dir,
        )
        print(f"\nBatch summary: {args.output / '_batch_summary.md'}")
    else:
        process_single_device(
            args.input, args.output, args.rules, args.platforms,
            analyze_sanitized=args.analyze_sanitized,
            no_sanitize=args.no_sanitize,
            runtime_csv=args.runtime_csv,
            runtime_dir=args.runtime_dir,
        )


if __name__ == "__main__":
    main()
