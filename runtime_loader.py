"""Runtime-data loader for NetBrain harvest exports.

Accepts the NetBrain native-text export (CLI Command Template output, with
`#--- <device> <command> Execute at <timestamp>` delimiter headers) or a CSV
shaped roughly like `(device_name, command, result, [timestamp])`. Returns the
runtime data for one hostname as a dict shaped to fit under
`analysis_report.json`'s `runtime` section, or None if no records match.

Dispatch is keyed by intent (DEC-004), not by raw command string. Adding a new
alias for an existing intent is an ALIAS_MAP entry — no new parser code
required unless the alias produces a different output dialect.
"""
import csv
import logging
import re
from pathlib import Path

from runtime_parsers import INTENT_PARSERS


log = logging.getLogger(__name__)


# Canonical raw-command-string -> intent-key map. Match is case-insensitive
# after whitespace collapsing.
ALIAS_MAP = {
    "show inventory": "inventory",
    "show version": "version",
    "show ip route summary": "route_table_ipv4_summary",
    "show ip nat statistics": "nat_statistics",
    "show crypto ipsec sa count": "crypto_ipsec_summary",
    "show crypto ipsec sa": "crypto_ipsec_summary",
    "show license summary": "license_summary",
    "show license all": "license_summary",
    "show license feature": "license_summary",
    "show processes cpu sorted": "cpu_processes",
    # Combined-harvest: the running-config body is extracted as a text block
    # and routed to the config pipeline (not INTENT_PARSERS). The startup-config
    # intent is recognized so the splitter can explicitly drop it; see
    # split_combined_harvest for the routing logic.
    "show running-config": "running_config",
    "show startup-config": "startup_config",
}


# Where each intent's parser output gets merged in the runtime dict.
INTENT_TARGETS = {
    "inventory": "inventory",
    "version": "platform",
    "route_table_ipv4_summary": "route_table",
    "nat_statistics": "nat",
    "crypto_ipsec_summary": "crypto",
    "license_summary": "license",
    "cpu_processes": "platform",
}

# Intents that do NOT flow through INTENT_PARSERS. These are recognized so the
# combined-harvest splitter can route them explicitly (running-config body →
# config pipeline; startup-config → dropped with a log line). They have no
# parser and no runtime-dict target, so the normal dispatch skips them.
PSEUDO_INTENTS = {"running_config", "startup_config"}


_HEADER_RE = re.compile(
    r"^#---\s+(?P<device>\S+)\s+(?P<command>.+?)\s+Execute at\s+"
    r"(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*$"
)
_INVALID_INPUT_RE = re.compile(r"%\s*Invalid input detected", re.IGNORECASE)
_PROMPT_ECHO_RE = re.compile(r"^\S+[>#]\s*(show\s+\S.*)$", re.IGNORECASE)

_DEVICE_COL_SYNONYMS = ("device_name", "hostname", "device")
_COMMAND_COL_SYNONYMS = ("command", "cmd")
_OUTPUT_COL_SYNONYMS = ("result", "output", "raw_output")
_TIMESTAMP_COL_SYNONYMS = ("timestamp", "time", "executed_at")


def normalize_command(cmd):
    """Lowercase + collapse whitespace; strip terminal pager / pipe args."""
    if not cmd:
        return ""
    s = re.sub(r"\s+", " ", cmd.strip()).lower()
    # Strip trailing `| include ...` / `| section ...` server-side filters —
    # the underlying intent is the same.
    s = re.sub(r"\s*\|\s*(include|exclude|section|begin|count)\b.*$", "", s)
    return s


def _strip_prompt_echo(body):
    """If the first non-empty body line is `<host>(>|#) show ...`, drop it."""
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        if _PROMPT_ECHO_RE.match(line.strip()):
            return "\n".join(lines[idx + 1:])
        return body
    return body


def _split_native_export(text):
    """Split a native NetBrain text export into (device, command, timestamp, body) tuples."""
    records = []
    current = None
    body_lines = []
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            if current is not None:
                records.append((*current, "\n".join(body_lines)))
            current = (m.group("device"), m.group("command"), m.group("timestamp"))
            body_lines = []
        else:
            if current is not None:
                body_lines.append(line)
    if current is not None:
        records.append((*current, "\n".join(body_lines)))
    return records


def _read_csv(path):
    """Yield (device, command, timestamp, body) from a CSV with flexible column names."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return
        col_map = {(name or "").strip().lower(): name for name in reader.fieldnames}
        device_col = next((col_map[s] for s in _DEVICE_COL_SYNONYMS if s in col_map), None)
        command_col = next((col_map[s] for s in _COMMAND_COL_SYNONYMS if s in col_map), None)
        output_col = next((col_map[s] for s in _OUTPUT_COL_SYNONYMS if s in col_map), None)
        ts_col = next((col_map[s] for s in _TIMESTAMP_COL_SYNONYMS if s in col_map), None)
        if not (device_col and command_col and output_col):
            raise ValueError(
                f"CSV {path}: missing required columns. Found {reader.fieldnames}; "
                f"need one of {_DEVICE_COL_SYNONYMS} + one of {_COMMAND_COL_SYNONYMS} + "
                f"one of {_OUTPUT_COL_SYNONYMS}."
            )
        for row in reader:
            yield (
                (row.get(device_col) or "").strip(),
                (row.get(command_col) or "").strip(),
                (row.get(ts_col) or "").strip() if ts_col else "",
                row.get(output_col) or "",
            )


def _detect_format(path):
    """Return 'csv' or 'native' based on extension and a peek of the file."""
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        return "csv"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#---"):
                return "native"
            if line.strip():
                break
    return "csv" if suffix in (".tsv",) else "native"


def _iter_records(path):
    fmt = _detect_format(path)
    if fmt == "csv":
        yield from _read_csv(path)
        return
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    yield from _split_native_export(text)


def is_combined_harvest(path):
    """Return True if the file's first non-empty line matches the `#---` header.

    A combined harvest is a NetBrain native-text export that bundles `show
    running-config` alongside the other runtime show commands. Auto-detection
    keys on the same delimiter the splitter already recognizes, so any file
    whose first non-empty line is a `#---` record header is considered
    combined-shaped — the downstream splitter handles the "no running-config
    record" case with a clear error.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                return bool(_HEADER_RE.match(line.rstrip("\n")))
    except OSError:
        return False
    return False


_HOSTNAME_DIRECTIVE_RE = re.compile(r"^\s*hostname\s+(\S+)\s*$", re.IGNORECASE)


def split_combined_harvest(export_path, hostname=None):
    """Split a combined-harvest file into (config_text, runtime_records, hostname).

    `runtime_records` is a list of `(intent, body, command, timestamp)` tuples
    for every non-config, non-skipped record matching the resolved hostname.
    The caller (main.py) drives the per-record sanitize-then-parse loop so
    the sanitizer instance can be shared across config and runtime bodies.

    If `hostname` is None, it is inferred from the first `show running-config`
    record's device field. If that record's config body also carries a
    `hostname FOO` directive that disagrees with the device field, a warning
    is logged and the device field wins (it matches the rest of the harvest).

    Returns (None, None, None) if no `show running-config` record is found —
    caller should raise a clear error to the operator.

    Prefers `show running-config` over `show startup-config` when both are
    present for the target hostname; startup-config records are dropped with
    a log line.

    Raises ValueError if the file contains records for more than one distinct
    device (case-insensitive comparison).
    """
    records = list(_iter_records(export_path))

    # Multi-device detection runs across ALL records, not just the hostname-
    # filtered subset — a combined harvest with two devices is an operator
    # scoping error that we want to surface, not silently narrow past.
    distinct = sorted({
        device.strip().lower()
        for device, _cmd, _ts, _body in records
        if device.strip()
    })
    if len(distinct) > 1:
        raise ValueError(
            f"Combined harvest contains {len(distinct)} devices: "
            f"{', '.join(distinct)}. netfit currently handles one device per "
            f"invocation. Narrow the NetBrain device scope or pre-split the "
            f"export."
        )

    # Infer hostname from the first running-config record's device field.
    resolved_host = (hostname or "").strip().lower() or None
    if resolved_host is None:
        for device, command, _ts, _body in records:
            if ALIAS_MAP.get(normalize_command(command)) == "running_config":
                resolved_host = device.strip().lower()
                break

    if resolved_host is None:
        # No running-config record. Log any startup-config sightings for the
        # operator's benefit and return the sentinel.
        for device, command, _ts, _body in records:
            if ALIAS_MAP.get(normalize_command(command)) == "startup_config":
                log.info(
                    "Dropping startup-config; no running-config found on %s.",
                    device,
                )
        return None, None, None

    config_text = None
    runtime_records = []
    for device, command, timestamp, body in records:
        if device.strip().lower() != resolved_host:
            continue
        intent = ALIAS_MAP.get(normalize_command(command))
        if intent == "running_config":
            if config_text is not None:
                # Multiple running-config records in one harvest is unusual
                # but benign — keep the first, warn on later ones.
                log.warning(
                    "Multiple show running-config records on %s; keeping first.",
                    device,
                )
                continue
            config_text = _strip_prompt_echo(body)
            continue
        if intent == "startup_config":
            log.info("Dropping startup-config; running-config takes precedence.")
            continue
        if intent is None:
            log.warning(
                "Skipping %r on %s: command does not map to a known intent.",
                command, device,
            )
            continue
        if _INVALID_INPUT_RE.search(body):
            log.warning(
                "Skipping %r on %s: device returned '%% Invalid input'.",
                command, device,
            )
            continue
        runtime_records.append((intent, body, command, timestamp))

    if config_text is None:
        return None, None, None

    # Hostname-directive disagreement check: if the config body declares a
    # different `hostname FOO` than the harvest device field, the device field
    # wins (the rest of the records are keyed by it) but the mismatch is worth
    # surfacing.
    for line in config_text.splitlines():
        m = _HOSTNAME_DIRECTIVE_RE.match(line)
        if m:
            cfg_host = m.group(1).strip().lower()
            if cfg_host and cfg_host != resolved_host:
                log.warning(
                    "Hostname mismatch: harvest device field %r != config "
                    "directive %r. Using device field.",
                    resolved_host, cfg_host,
                )
            break

    return config_text, runtime_records, resolved_host


def assemble_runtime_from_records(runtime_records, body_transform=None):
    """Dispatch sanitized-or-raw runtime record bodies through INTENT_PARSERS.

    `runtime_records` is the list returned by split_combined_harvest:
    `[(intent, body, command, timestamp), ...]`. `body_transform` is an
    optional callable that receives a body and returns the sanitized body —
    main.py passes the shared sanitizer's `sanitize` method so runtime IPs,
    serials, and UDIs are tokenized into the same mappings as the config.

    Returns the runtime dict shaped to fit under `analysis_report["runtime"]`,
    matching the shape produced by load_runtime_for_device.
    """
    runtime = {"harvest_source": "netbrain"}
    timestamps = []
    for intent, body, command, timestamp in runtime_records:
        parser = INTENT_PARSERS[intent]
        target = INTENT_TARGETS[intent]
        body_clean = _strip_prompt_echo(body)
        if body_transform is not None:
            body_clean = body_transform(body_clean)
        try:
            section = parser(body_clean, command)
        except Exception as exc:
            log.warning(
                "Parser %r failed on %r: %s. Skipping record.",
                intent, command, exc,
            )
            continue
        runtime.setdefault(target, {}).update(section)
        if timestamp:
            timestamps.append(timestamp)
    if timestamps:
        runtime["harvest_timestamp"] = max(timestamps)
    return runtime


def load_runtime_for_device(export_path, hostname):
    """Parse the export at `export_path` and return the runtime dict for `hostname`.

    Returns None if no records match the hostname (case-insensitive).
    Records with `% Invalid input` bodies are skipped with a warning.
    Records whose command does not normalize to a known intent are skipped
    with a warning.
    """
    target_host = (hostname or "").strip().lower()
    runtime = {"harvest_source": "netbrain"}
    timestamps = []
    matched_any = False

    for device, command, timestamp, body in _iter_records(export_path):
        if device.strip().lower() != target_host:
            continue
        matched_any = True
        if _INVALID_INPUT_RE.search(body):
            log.warning(
                "Skipping %r on %s: device returned '%% Invalid input'.",
                command, device,
            )
            continue
        intent = ALIAS_MAP.get(normalize_command(command))
        if intent is None:
            log.warning(
                "Skipping %r on %s: command does not map to a known intent.",
                command, device,
            )
            continue
        if intent in PSEUDO_INTENTS:
            # running-config / startup-config records don't belong in the
            # two-file workflow — they're handled by split_combined_harvest.
            log.info(
                "Skipping %r on %s in two-file runtime load; use combined-"
                "harvest auto-detect for config bodies.",
                command, device,
            )
            continue
        parser = INTENT_PARSERS[intent]
        target = INTENT_TARGETS[intent]
        body_clean = _strip_prompt_echo(body)
        try:
            section = parser(body_clean, command)
        except Exception as exc:
            log.warning(
                "Parser %r failed on %s/%r: %s. Skipping record.",
                intent, device, command, exc,
            )
            continue
        runtime.setdefault(target, {}).update(section)
        if timestamp:
            timestamps.append(timestamp)

    if not matched_any:
        return None

    if timestamps:
        runtime["harvest_timestamp"] = max(timestamps)

    return runtime
