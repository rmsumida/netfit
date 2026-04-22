# netfit

Offline hardware-refresh planning tool for network devices. Takes a dumped running-config, analyzes its workload (interfaces, routing, services, crypto, scale), and ranks candidate replacement platforms by fit. No LLM calls, no network — fully deterministic.

**Currently supported input:** Cisco IOS / IOS-XE running-configs. The pipeline is structured so that additional input families (NX-OS, IOS-XR, other vendors) can be added as new analyzer front-ends without changing the assessor, scoring, or reporting stages.

## Quick start

```bash
pip install -r requirements.txt
python main.py path/to/your-device-config.txt
```

Outputs land in `output/` by default:

| File | What it is |
|------|------------|
| `sanitized_config.txt` | Redacted copy of the input (IPs / usernames tokenized, secrets replaced with `<REDACTED_*>` markers) |
| `sanitization_mappings.json` | Token → original mapping table, so you can correlate a sanitized report back to real values |
| `analysis_report.json` | Structured workload extraction: interfaces, routing, services, scale signals |
| `platform_comparison.{json,md,html}` | Ranked multi-platform comparison with per-candidate breakdown |
| `best_fit_report.{md,html}` | Focused single-platform "what should I buy" view scoped to the winning candidate |

## CLI

```
python main.py INPUT [options]

  INPUT                  Path to a single config file, or a directory of configs
                         (*.txt / *.cfg / *.conf) for batch mode.

  -o, --output DIR       Output directory (default: output/)
  --rules FILE           Sanitization rules YAML (default: rules.yaml)
  --platforms DIR        Candidate platform profile directory (default: platforms/)
  --analyze-sanitized    Run the analyzer against the sanitized config
                         (default: analyze the original for parser fidelity;
                         sanitized artifacts are still emitted)
  --no-sanitize          Skip sanitization entirely (input treated as already-clean)
```

### Single-device mode

```bash
python main.py input/router_config.txt
```

### Batch mode

Process every config in a directory; produces per-device subfolders plus a cross-device roll-up:

```bash
python main.py path/to/configs/ --output refresh_2026/
```

### Combined-harvest mode (NetBrain single file)

When NetBrain harvests `show running-config` alongside the runtime show commands in one file, drop it directly:

```bash
python3 main.py input/router_combined_harvest.txt
```

netfit auto-detects the NetBrain `#---` delimiter signature, splits the file, sanitizes both the config and the runtime bodies, and runs the full pipeline. No flag needed. Mutually exclusive with `--runtime-csv`.

Output structure:

```
refresh_2026/
  _batch_summary.json          # cross-device summary + platform-fit matrix
  _batch_summary.md            # human-readable version of the above
  device-1/
    sanitized_config.txt
    analysis_report.json
    platform_comparison.{json,md,html}
    best_fit_report.{md,html}
  device-2/
    ...
```

## Configuration

### `rules.yaml` — sanitization toggles

Toggle which categories of data get redacted. Setting any flag to `false` disables that category.

```yaml
sanitize:
  hostname: false
  ip_addresses: true
  usernames: true
  snmp_communities: true
  enable_secrets: true
  tacacs_radius_keys: true
  crypto_keys: true
  email_addresses: true
```

### `platforms/*.yaml` — candidate platforms

One YAML per candidate SKU. Each declares `capabilities`, `scale`, `constraints`, `fit_preferences`, and optional `notes`. See `platforms/c8500-12x.yaml` for the canonical shape, or [documents/ARCHITECTURE.md](documents/ARCHITECTURE.md) for the full schema.

To evaluate a new platform: drop a new YAML in `platforms/` — no code change needed.

## How it works

```
input config
     │
     ▼
sanitizer.py ──► sanitized_config.txt + mappings.json
     │
     ▼
analyzer.py ──► analysis_report.json
     │
     ▼
platform_compare.py ──► platform_comparison.{json,md,html}
                        best_fit_report.{md,html}
   (calls assessor.py per YAML in platforms/)
```

Each stage writes its output to disk as JSON, so any downstream stage can be re-run independently against a frozen prior-stage artifact. The analyzer is the only stage with vendor/OS-specific parsing — extending netfit to a new device family is scoped to that module. Full details in [documents/ARCHITECTURE.md](documents/ARCHITECTURE.md).

## Testing

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/
```

Sanitizer regex correctness (including the historical TACACS-key leak regression), platform comparison logic, speed-allocation scenarios, backward-compatibility with earlier analysis JSON shapes, platform YAML schema validation, and end-to-end pipeline output.

> **Note for contributors:** `tests/test_pipeline_e2e.py` skips gracefully when `input/router_config.txt` is absent. To exercise the full pipeline locally, drop any Cisco IOS/IOS-XE running-config at that path (it will not be committed — see `.gitignore`), or point the fixture at your own sample.

## Repository layout

| Path | Contents |
|------|----------|
| `main.py` | CLI entry point + batch-mode orchestration |
| `sanitizer.py` | Secret redaction + PII tokenization |
| `analyzer.py` | Config feature extraction — vendor/OS-specific parsing lives here (Cisco IOS/IOS-XE today, via `ciscoconfparse`) |
| `assessor.py` | Per-target compatibility / capacity findings |
| `platform_compare.py` | Fitness scoring, ranking, Markdown / HTML rendering |
| `rules.yaml` | Sanitization toggles |
| `platforms/` | Candidate platform YAML profiles |
| `input/` | Drop-folder for configs to analyze (contents gitignored; directory kept via `.gitkeep`) |
| `output/` | All generated artifacts (gitignored; auto-created on first run) |
| `tests/` | pytest suite |
| `documents/ARCHITECTURE.md` | Full architecture and design document |
| `documents/NETBRAIN_HARVEST.md` | NetBrain feature/data harvest notes |
| `CLAUDE.md` | Repo-specific guidance for Claude Code sessions (governance + technical) |
| `governance/` | Session protocol, decisions, action items, project context |
| `sessions/` | Per-session logs |

## Scope and roadmap

netfit is built around a **vendor/OS boundary**: the analyzer is the only stage that knows how to parse a specific config dialect. Everything downstream — the assessor, fitness scoring, ranking, and report rendering — operates on a vendor-neutral `analysis_report.json` schema and a vendor-neutral `platforms/*.yaml` schema. Adding support for a new device family should be additive and scoped to:

1. A new analyzer front-end (or a dialect branch inside `analyzer.py`) that emits the same `analysis_report.json` shape.
2. New sanitizer patterns for any secret/key formats specific to that OS.
3. New platform YAMLs for candidate targets in that family.

Assessor logic and scoring weights should not need to change to onboard a new dialect.

**Coverage status:**

| Input family | Status |
|--------------|--------|
| Cisco IOS / IOS-XE | Supported |
| Cisco NX-OS | Not yet — pipeline designed to accept it; analyzer front-end needed |
| Cisco IOS-XR | Not yet — same |
| Non-Cisco (Arista EOS, Juniper Junos, etc.) | Not yet — same |

Open development work is tracked as [GitHub Issues](https://github.com/rmsumida/netfit/issues).

## Determinism guarantee

Same input config + same `rules.yaml` + same `platforms/*.yaml` produces byte-identical output every time. The pipeline performs no network I/O, no API calls, no model inference. It can run in air-gapped environments.

---

## Project Governance

This repo uses a lightweight session-based governance system alongside the code.

### Quick start — new session

1. `git pull`
2. **Claude Chat:** open a new conversation in the netfit Project — context loads automatically, Claude briefs you
3. **Claude Code:** `cd` into the repo — `CLAUDE.md` loads automatically, Claude briefs you
4. Work through the session
5. End the session:
   - In Claude Chat: say **"Shutdown prompt"** → paste generated prompt into Claude Code
   - In Claude Code: say **"Session shutdown"** → handles everything directly

### Governance files

```
governance/
  PROJECT_CONTEXT.md      ← Master project state
  ACTION_ITEMS.md         ← Project-level action items (dev work → GitHub Issues)
  SESSION_INDEX.md        ← Index of all sessions
  DECISIONS.md            ← Decision register with rationale
  TRIGGER_COMMANDS.md     ← Reference for the two trigger commands
  PROJECT_INSTRUCTIONS.md ← Claude Chat project instructions
sessions/
  session-NNN.md          ← Individual session logs
```

- **PROJECT_CONTEXT.md** — single source of truth; updated at end of every session.
- **ACTION_ITEMS.md** — tracks project-level / governance items. Code-level development work is tracked as GitHub Issues.
- **DECISIONS.md** — locked decisions require new material information to reopen.
- **Session numbering** — sequential (001, 002, 003…).

## License

(Add your license here.)
