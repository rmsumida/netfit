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
