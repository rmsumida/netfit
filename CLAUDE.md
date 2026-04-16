# CLAUDE.md — netfit

Offline hardware-refresh planning tool for network devices. Takes a dumped running-config, analyzes its workload (interfaces, routing, services, crypto, scale), and ranks candidate replacement platforms by fit. No LLM calls, no network — fully deterministic.

**Currently supported input:** Cisco IOS / IOS-XE running-configs. Pipeline is partitioned so additional input families (NX-OS, IOS-XR, non-Cisco) can be added as new analyzer front-ends without touching the assessor, scoring, or reporting stages.

---

## Part 1 — Governance & Session Protocol

### Project Structure

```
governance/
  PROJECT_CONTEXT.md   — Master project state (goals, decisions, issues, contacts)
  ACTION_ITEMS.md      — Project-level action items (dev work lives in GitHub Issues)
  SESSION_INDEX.md     — One-line index of all sessions
  DECISIONS.md         — Decision register with rationale
  TRIGGER_COMMANDS.md  — Reference doc for trigger commands
  PROJECT_INSTRUCTIONS.md — Claude Chat project instructions (not used by Code)
sessions/
  session-NNN.md       — Individual session logs
documents/             — Architecture docs, datasheets, deliverables
input/                 — Drop-folder for configs to analyze (contents gitignored)
platforms/             — Candidate platform YAML profiles
tests/                 — pytest suite
```

### On First Interaction

When the user starts a conversation in this repo, read:
1. `governance/PROJECT_CONTEXT.md`
2. `governance/ACTION_ITEMS.md`
3. `governance/SESSION_INDEX.md`

Then brief the user:
- Current session number (last session in index + 1)
- One-line summary of last session
- Open critical/blocking items (from ACTION_ITEMS.md and GitHub Issues)
- Recommended focus

Ask: "What happened since last session, and what do you want to focus on today?"

### Trigger: "Session shutdown"

When the user says **"Session shutdown"**:

1. Read current state of all governance files
2. Ask: "What was accomplished this session?" and "Any items to close, add, or update?"
3. Perform ALL of the following:

**Create session log** — `sessions/session-NNN.md` with date, platform, context, work done, decisions, open items, files created/updated.

**Update SESSION_INDEX.md** — append row: `| [NNN](../sessions/session-NNN.md) | YYYY-MM-DD | Summary | Key outcomes |`

**Update ACTION_ITEMS.md** — mark items `[x]` with completion date, add new items with sequential numbering + priority, recalculate the summary table.

**Update PROJECT_CONTEXT.md** — `Last updated` date, session number, any changed fields.

**Update DECISIONS.md** — new decisions with sequential DEC-NNN numbering, status, rationale, alternatives. Update status of existing decisions if changed.

**Commit and push**
```bash
git add -A
git commit -m "Session NNN close-out: <brief summary>"
git push
```

### Between-Session Commands

One-off commands (no session log):

- "Mark items X and Y as complete" → update `ACTION_ITEMS.md`, commit, push
- "Add a new item: ..." → add to `ACTION_ITEMS.md` with proper formatting, commit, push
- "Update item X notes: ..." → edit notes field, commit, push

---

## Part 2 — Technical Guide

### Commands

Install deps:
```bash
pip install -r requirements.txt           # runtime
pip install -r requirements-dev.txt       # adds pytest
```

Single-device run:
```bash
python main.py input/router_config.txt
```

Batch run (directory of `*.txt` / `*.cfg` / `*.conf`):
```bash
python main.py path/to/configs/ --output path/to/output/
```

Flags on `main.py`:
- `-o/--output DIR` — output directory (default `output/`)
- `--rules FILE` — sanitization rules YAML (default `rules.yaml`)
- `--platforms DIR` — candidate profile directory (default `platforms/`)
- `--analyze-sanitized` — analyze the sanitized config instead of the original
- `--no-sanitize` — skip sanitization (treat input as already-clean)

Tests:
```bash
python3 -m pytest tests/                   # full suite (66 tests)
python3 -m pytest tests/test_sanitizer.py  # single file
python3 -m pytest tests/ -v -k "best_fit"  # filter by name
```

### Pipeline architecture

`main.py` orchestrates four stages. Data flows via JSON files so each stage can be re-run independently against the previous stage's artifact.

```
input/*.txt
        │
        ▼
  sanitizer.py ──► output/.../sanitized_config.txt
                   output/.../sanitization_mappings.json
        │
        ▼
  analyzer.py ──► output/.../analysis_report.json
        │
        ▼
  platform_compare.py ──► output/.../platform_comparison.{json,md,html}
                          output/.../best_fit_report.{md,html}
     (calls assessor.py per YAML in platforms/)
```

In batch mode, each device gets its own subdirectory and a cross-device `_batch_summary.{json,md}` is written alongside.

### Stage contracts

- **sanitizer.py** — `rules.yaml` toggles what gets redacted. IPs/usernames/hostnames/emails are tokenized via `TokenMapper` (stable `IP_001`, `USER_001`, … per distinct value, exported to `sanitization_mappings.json`). Secrets/keys are replaced with `<REDACTED_*>` markers.

  The regex list `SECRET_LINE_PATTERNS` at module scope is the canonical reference. Each entry captures **three groups** — `(prefix, secret, suffix)` — so the optional `(?:\s+\d+)?` encryption-type digit stays in the prefix and the actual hex/ASCII secret is what gets redacted. Before the iteration-1 fix, generic `\bpassword\b` / `\bkey\b` fallbacks captured the encryption-type digit as the "secret" and left the real material visible on the line. There is **no blind generic fallback** — every command that carries a secret gets an explicit pattern, and lines that merely contain the words "password" / "secret" / "key" in descriptions or command keywords are left alone.

- **analyzer.py** — **this is the vendor/OS-specific stage.** Current implementation wraps `ciscoconfparse.CiscoConfParse` to parse Cisco IOS/IOS-XE. Adding another dialect (NX-OS, IOS-XR, non-Cisco) means adding a branch or a parallel front-end here; downstream stages consume the JSON schema, not the config text. Produces `analysis_report.json` with sections `summary`, `inventory`, `interfaces`, `switching`, `routing`, `high_availability`, `security`, `services`, `policy`, `management_plane`, `crypto_vpn`, plus `refresh_risks` and `migration_considerations` lists. Downstream stages access via `_get(dct, [path…], default)` — adding a field is additive-safe, renaming a field silently breaks consumers.

  **Field semantics to respect when extending:** `active_*` excludes shutdown, subinterfaces, and logical interfaces (loopback/tunnel/SVI/port-channel). The assessor and platform_compare both expect these to reflect workload demand, not nameplate count. Don't regress to scoring against `interfaces.total` or `by_type` — those include shutdown/legacy ports and produce false "exceeds scale" findings.

  STP presence signal (`switching.spanning_tree.present`) is deliberately narrow — it excludes `spanning-tree extend system-id` (universal IOS default) and only trips on real usage indicators (mode, per-VLAN config, logging, or interface-level portfast/bpduguard).

- **assessor.py** — `assess_refresh(analysis, target_profile)` → `{findings: [...], assessment_summary: {...}}`. Findings have `severity ∈ {critical, high, medium, low, info}` with `SEVERITY_SCORES` {40, 25, 15, 5, 0}. The summary's `overall_recommendation ∈ {LIKELY_FIT, CONDITIONAL_FIT, HIGH_RISK, NOT_RECOMMENDED, UNKNOWN}`:
    - ANY critical finding → NOT_RECOMMENDED (hard floor)
    - `total_risk_score >= 80` → HIGH_RISK
    - `total_risk_score >= 35` → CONDITIONAL_FIT
    - otherwise → LIKELY_FIT

  Because ANY critical finding flips the verdict, false-positive criticals are the main risk. The assessor uses `active_physical_count` against `max_physical_interfaces` (with fallback to `max_interfaces`), and `active_physical_by_type` for unsupported-type detection, specifically to avoid counting shutdown Serial/legacy ports against modern platforms.

- **platform_compare.py** — single canonical module containing: I/O helpers, `_allocate_speed_capacity` (greedy upward-substitution matcher: 1G → {1G,10G,25G,40G,100G}, 10G → {10G,25G,40G,100G}, etc.), `rank_assessment`, `compute_platform_fitness` (starts at 1000, applies penalties/bonuses, returns `(score, breakdown, interface_comparison)`), `compare_platforms`, and the Markdown / HTML renderers. The HTML renderer is a real styled document with severity/recommendation badges and per-platform `<details>` sections — **not** the old `<pre>`-wrapped markdown.

  `build_platform_comparison_reports` is the orchestration entry point. It writes the multi-platform comparison (JSON/MD/HTML) plus a focused `best_fit_report.{md,html}` scoped to the winning platform only.

### Platform profile schema (`platforms/*.yaml`)

Four top-level sections consumed by the pipeline:
- `capabilities` — boolean feature flags + `supported_interface_types` list.
- `scale` — numeric ceilings. Required: `max_interfaces`, `max_physical_interfaces`, `max_subinterfaces`, `max_vrfs`, `max_bgp_neighbors`, `max_static_routes`, and `ports` dict with `native` / `breakout` / `reserved_or_dedicated` keyed by speed class (`1G`, `10G`, `25G`, `40G`, `100G`). `max_physical_interfaces` falls back to `max_interfaces` if absent.
- `constraints.intended_role` — `"wan_edge"` grants a bonus; other roles neutral.
- `fit_preferences` — weighted tuning knobs (`role_weight`, `throughput_weight`, `services_weight`, `crypto_weight`, `routing_scale_weight`, `role_alignment`, `branch_bias`). Multipliers are hardcoded in `compute_platform_fitness`.

`platforms/archive/` holds older profile formats — **not** loaded; only top-level `*.yaml` / `*.yml` are.

### Test structure

- `tests/test_sanitizer.py` — parametrized regression tests for every redaction pattern plus non-secret passthrough cases. New sanitizer regex changes should add cases here.
- `tests/test_platform_compare.py` — `_allocate_speed_capacity` scenarios, ranking tie-breakers, backward-compat with pre-iteration-1 analysis JSON shapes, platform YAML schema validation.
- `tests/test_pipeline_e2e.py` — runs the full pipeline against `input/router_config.txt`, asserts output shape, workload baseline numbers, best-fit platform, and calibration health (best-fit earns a real LIKELY_FIT / CONDITIONAL_FIT, undersized platforms still flag interface overflow). **The repo does not ship a sample config** — the fixture is gitignored and these tests skip when it is absent. Contributors must drop their own Cisco IOS/IOS-XE config at `input/router_config.txt` (or repoint the fixture) to exercise them locally.

### Architectural invariants (do not regress)

1. **Vendor/OS boundary** — only `analyzer.py` parses config dialect. Assessor, scoring, and reporting are vendor-neutral. Adding a new dialect must not require changes to `assessor.py` or `platform_compare.py`. Captured as DEC-001.
2. **Determinism** — same input + same `rules.yaml` + same `platforms/*.yaml` → byte-identical output. No network I/O, no API calls, no model inference.
3. **Active-interface semantics** — assessor and scoring read `active_physical_count` / `active_physical_by_type`, not nameplate counts. Preserve this distinction when extending the analyzer.
4. **Explicit secret patterns** — no generic `\bpassword\b` / `\bkey\b` fallback in the sanitizer. Every secret-bearing command has its own three-group `(prefix, secret, suffix)` regex.

### Where to find more

- `documents/ARCHITECTURE.md` — full architecture and design document.
- `documents/NETBRAIN_HARVEST.md` — NetBrain feature/data harvest notes.
- Active development work — tracked as GitHub Issues (`gh issue list`).
