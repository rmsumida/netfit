# Architecture

This document describes the design of `netfit`: how the pipeline is laid out, what each module is responsible for, how data flows between stages, and the design decisions behind the scoring model.

## Overview

`netfit` is an **offline, deterministic** Python pipeline that takes a network-device running-config and ranks candidate replacement platforms by fit. Its primary use case is hardware refresh planning for end-of-life devices.

Input coverage today is Cisco IOS / IOS-XE. The vendor/OS-specific parsing is confined to `analyzer.py`; the downstream assessor, scoring, and reporting stages consume a vendor-neutral `analysis_report.json` so that additional device families (NX-OS, IOS-XR, non-Cisco) can be added as new analyzer front-ends without perturbing the rest of the pipeline.

There are no LLM calls, no network I/O, no external services. Same input → same output, every time.

The tool runs in two modes:
- **Single-device** — analyze one config file, produce one comparison report
- **Batch** — process every config file in a directory, produce per-device reports plus a cross-device roll-up matrix

## Vendor / OS boundary

The pipeline deliberately isolates everything dialect-specific in one place so that netfit can grow to cover additional network-device families without restructuring:

| Stage | Vendor/OS awareness |
|-------|---------------------|
| `sanitizer.py` | Partially dialect-specific — redaction patterns target Cisco IOS/IOS-XE secret-line formats today. Adding a new OS dialect means appending new patterns, not restructuring. |
| `analyzer.py` | **Fully dialect-specific.** Current implementation uses `ciscoconfparse` and targets IOS/IOS-XE. A new input family (NX-OS, IOS-XR, non-Cisco) is onboarded here — either as a dialect branch or a parallel front-end — and must emit the same `analysis_report.json` shape. |
| `assessor.py` | **Vendor-neutral.** Consumes the analyzer's JSON schema. Must not grow per-OS branches. |
| `platform_compare.py` | **Vendor-neutral.** Fitness math, ranking, and rendering are the same for any input family. |
| `platforms/*.yaml` | **Vendor-neutral schema, any-vendor content.** Bundled profiles today are Cisco C8500 variants, but the schema itself has no Cisco-specific fields — an Arista or Juniper target profile would use the same keys. |

When extending netfit, preserve this boundary. If a proposed change adds a conditional like `if vendor == "cisco": ...` to the assessor or platform_compare, that's a signal to move the logic upstream into the analyzer's output schema instead.

## Target architecture: hexagonal / ports and adapters

**Current state:** netfit is a procedural pipeline of four modules linked by JSON files. The vendor/OS boundary above is the first explicit separation between a **core** (workload-to-platform fitness reasoning) and its **adapters** (config dialect parsers, platform profile loaders, report renderers).

**Long-term direction:** evolve the codebase toward a hexagonal (ports-and-adapters) architecture, with a clearly defined application core surrounded by interchangeable adapters on each side.

Expected target shape:

| Role | Responsibility | Today | Future adapters |
|------|----------------|-------|-----------------|
| **Inbound adapter** (driving) | Trigger an analysis | `main.py` CLI | Web API, CI webhook, IDE plugin |
| **Core domain** | Workload modeling, fitness scoring, recommendation logic — pure, no I/O, no vendor knowledge | Partial: lives inside `assessor.py` + `platform_compare.py` but mixed with I/O and rendering | A `core/` package containing an immutable `Workload`, `CandidatePlatform`, `Assessment`, and the scoring functions — all pure, dependency-free |
| **Config parser port** | Ingest a device config, emit a `Workload` | `analyzer.py` (IOS/IOS-XE only, monolithic) | One adapter per dialect: `adapters/ios_xe.py`, `adapters/nx_os.py`, `adapters/ios_xr.py`, `adapters/arista_eos.py`, `adapters/junos.py` |
| **Sanitizer port** | Redact secrets, tokenize PII | `sanitizer.py` (IOS/IOS-XE patterns) | Per-dialect sanitizer adapters sharing a common interface |
| **Platform profile port** | Load candidate targets | YAML files under `platforms/` | Database-backed, API-fetched, or organization-catalog adapters |
| **Report renderer port** | Emit a comparison in a given format | Markdown, HTML, JSON inside `platform_compare.py` | `adapters/render_csv.py`, `adapters/render_slack.py`, `adapters/render_jira.py`, etc. — each swappable behind the same port interface |
| **Runtime-state port** (planned) | Augment static config with live device telemetry | Not implemented; see `NETBRAIN_HARVEST.md` | Adapters for NetBrain, SolarWinds, custom SNMP collectors, vendor-native APIs |

Guiding rules for changes that move us toward this target:

1. **Core knows no vendors, no file formats, no I/O.** If you find yourself importing `ciscoconfparse` or `yaml` into core logic, you're on the wrong side of the port.
2. **Adapters depend on the core, not vice versa.** An `Assessment` type lives in the core; an IOS-XE parser imports it. The core never imports from `adapters/`.
3. **Ports are defined by the core's needs, not the adapter's capabilities.** The `Workload` schema is what the assessor needs to reason about refresh fit — it does not mirror `ciscoconfparse`'s AST, and future adapters must translate *to* it, not extend it with dialect-specific fields.
4. **Refactor incrementally.** Today's `analysis_report.json` is the de facto `Workload` port contract. Formalizing it as a typed schema (pydantic/dataclass) is the next concrete step; wholesale restructuring into `core/` and `adapters/` folders can come after.

Treat this as the north star for structural decisions. Small features can ship in the current layout; structural changes (new dialects, new output formats, new profile sources) should move the codebase toward this shape rather than entrenching the current procedural layout.

## Design philosophy

| Principle | Why |
|-----------|-----|
| **Deterministic** | Migration decisions need to be auditable and reproducible. A non-deterministic recommendation engine is a non-starter for change-management approval. |
| **Offline / air-gapped friendly** | Network device configs are sensitive. The tool runs without internet access so it can be used in restricted environments. |
| **File-based stage handoffs** | Each stage writes its output to disk as JSON. Downstream stages can be re-run in isolation against a frozen artifact, which makes debugging and iteration much faster than re-running the whole pipeline. |
| **Additive-safe field access** | All cross-stage reads use a `_get(dct, [path], default)` helper so adding analyzer fields doesn't break existing assessors. Renaming fields, however, fails silently — so the tests assert key field shapes. |
| **No blind regex fallbacks in the sanitizer** | Generic catch-all patterns historically captured the wrong group and leaked secrets. Every command that carries a secret gets an explicit pattern; lines that merely *contain* secret-related words are left alone. |
| **Profile data is YAML, not Python** | Adding a new candidate platform should not require code changes — drop a new YAML file in `platforms/`. |

## Pipeline architecture

```
                    ┌─────────────────────────┐
                    │ input/router_config.txt │  (or directory in batch mode)
                    └────────────┬────────────┘
                                 │
                                 ▼
                       ┌──────────────────┐
                       │   sanitizer.py   │  reads rules.yaml
                       └────────┬─────────┘
                                │ writes
                                ▼
            ┌──────────────────────────────────────┐
            │  output/sanitized_config.txt         │
            │  output/sanitization_mappings.json   │
            └────────────────┬─────────────────────┘
                             │
                             ▼
                    ┌──────────────────┐
                    │   analyzer.py    │  uses ciscoconfparse
                    └────────┬─────────┘
                             │ writes
                             ▼
              ┌─────────────────────────────────┐
              │  output/analysis_report.json    │
              └────────────────┬────────────────┘
                               │
                               ▼
                ┌──────────────────────────────┐
                │    platform_compare.py       │  reads platforms/*.yaml
                │      (calls assessor.py      │
                │       per platform profile)  │
                └────────────────┬─────────────┘
                                 │ writes
                                 ▼
        ┌────────────────────────────────────────────┐
        │  output/platform_comparison.{json,md,html} │
        │  output/best_fit_report.{md,html}          │
        └────────────────────────────────────────────┘
```

In batch mode, each device gets its own subdirectory under the output root, plus a top-level `_batch_summary.{json,md}` containing a per-device best-fit table and a platform-fit matrix (platforms × devices, cells = fitness score).

### Why file-based handoffs

Each stage reads from and writes to disk. This is intentional:

1. **Independent re-runs.** If you tweak the assessor, you don't have to re-run sanitization and analysis — those JSON artifacts are still valid. `python -c "from platform_compare import build_platform_comparison_reports; build_platform_comparison_reports(...)"` works against any prior run's `analysis_report.json`.

2. **Debuggability.** When a finding looks wrong, you can diff `analysis_report.json` against expected values without rerunning the whole pipeline.

3. **Cache locality for batch mode.** Per-device subdirectories are independent — a failure on device 7 doesn't lose the work for devices 1–6.

The cost is a few hundred milliseconds of disk I/O per stage, which is negligible.

## Module contracts

### `sanitizer.py`

**Responsibility:** Redact secrets and tokenize PII (IP addresses, usernames, hostnames, emails) from the input config so the artifact is safe to share or commit.

**Inputs:**
- Raw config text (string)
- `rules.yaml` (toggles for which categories to redact)

**Outputs:**
- Sanitized config text
- Token mappings dict (`{category: {original: token}}`) — written to `sanitization_mappings.json`

**Key design points:**

- Two redaction strategies in use:
  - **Tokenization** for reversible mappings: IPs become `IP_001`, `IP_002`, …; usernames become `USER_001`, etc. The mapping is preserved in `sanitization_mappings.json` so an operator can correlate a sanitized report back to original values.
  - **Redaction markers** for irreversible secrets: passwords/keys/communities become `<REDACTED_ENABLE_SECRET>`, `<REDACTED_BGP_PASSWORD>`, etc. There is no "unredact" path — secret values are not preserved anywhere.

- `SECRET_LINE_PATTERNS` is the canonical list. Every entry captures three groups: `(prefix, secret, suffix)`. The optional `(?:\s+\d+)?` between the keyword and the secret absorbs Cisco's encryption-type indicator (0/5/7/8/9) into the prefix, so the secret group always points at the actual hash/key — not the type digit. Historically the generic `\bpassword\s+(\S+)` fallback had this backwards: it captured the digit as the "secret" and left the hash exposed in the trailing group.

- **No blind generic fallbacks.** Lines that merely contain the words "password", "secret", or "key" in descriptions, command names (`service password-encryption`), or identifier positions (`key chain MYNAME`, `crypto key generate rsa`) are deliberately not matched.

- **Idempotent.** `<REDACTED_*>` markers in the input are recognized and left alone; running the sanitizer twice produces the same output as running it once.

### `analyzer.py`

**Responsibility:** Parse the config and extract structured workload signals.

**Inputs:**
- Config text file path

**Outputs:**
- `analysis_report.json` with this top-level structure:
  ```
  {
    "summary": {hostname, interface_count, interface_types, routing_protocols_enabled, ...},
    "inventory": {chassis, modules, ...},
    "interfaces": {total, active_total, active_physical_count, by_type, active_physical_by_type,
                   active_physical_by_speed_class, subinterfaces, tunnels, loopbacks, details, ...},
    "switching": {vlans_defined_count, trunking_present, etherchannel_present, spanning_tree, ...},
    "routing": {protocols, vrfs, bgp, static_route_count, ...},
    "high_availability": {hsrp_present, vrrp_present, glbp_present},
    "security": {aaa_present, tacacs_present, radius_present, ...},
    "services": {snmp_present, nat_present, dhcp_server_present, ip_sla_present, ...},
    "policy": {qos_present, class_map_count, policy_map_count, ...},
    "management_plane": {vty_present, console_present, ...},
    "crypto_vpn": {crypto_present, isakmp_present, ikev2_present, ipsec_present, ...},
    "refresh_risks": [list of human-readable strings],
    "migration_considerations": [list of human-readable strings]
  }
  ```

**Key design points:**

- Built on `ciscoconfparse.CiscoConfParse` — a hierarchical parser that understands Cisco's indentation-based config structure (interface blocks, route-map sequences, line vty children, etc.).

- **Two parallel field families on `interfaces`:** the legacy raw counts (`total`, `by_type`, `subinterfaces`) and the iteration-1 *active* counts (`active_physical_count`, `active_physical_by_type`, `active_subinterfaces`, etc.). Active counts exclude shutdown ports, subinterfaces, and logical interfaces (loopback/tunnel/SVI/port-channel). Downstream consumers should prefer the active counts — they reflect actual workload demand, not nameplate inventory.

- **Speed-class histogram** (`active_physical_by_speed_class`) is the iteration-2 addition that drives speed-matching. It buckets active physical ports into `1G`, `10G`, `25G`, `40G`, `100G`.

- **STP presence detection is deliberately narrow.** The bare existence of any line matching `^spanning-tree` is *not* a signal — `spanning-tree extend system-id` is a universal IOS default that appears on routers which never actually run STP. Only mode/per-VLAN/logging/portfast/bpduguard usage trips the flag.

- **Field schema is additive-safe but not rename-safe.** Adding `active_management_interfaces` is harmless to existing consumers. Renaming `active_physical_count` to `physical_active_count` would silently zero out fitness penalties because the assessor's `_get()` would fall back to 0. Tests assert the field shape.

### `assessor.py`

**Responsibility:** Apply per-target compatibility and capacity rules to produce a list of findings (with severity) and a summary recommendation.

**Inputs:**
- Analysis dict (output of analyzer.py, in-memory)
- Target platform profile dict (one YAML's contents)

**Outputs:**
- A dict shaped:
  ```
  {
    "target_platform": "Cisco_C8500-20X6C",
    "assessment_summary": {
      "overall_recommendation": "LIKELY_FIT" | "CONDITIONAL_FIT" | "HIGH_RISK" | "NOT_RECOMMENDED" | "UNKNOWN",
      "total_risk_score": <int>,
      "finding_counts": {"critical": N, "high": N, "medium": N, "low": N, "info": N}
    },
    "findings": [{category, severity, score, title, detail, recommendation}, ...]
  }
  ```

**Severity scoring:**

| Severity | Score weight |
|----------|--------------|
| critical | 40 |
| high | 25 |
| medium | 15 |
| low | 5 |
| info | 0 |

`total_risk_score` is the sum of finding scores.

**Overall recommendation logic:**

```python
if any critical finding present:
    NOT_RECOMMENDED              # hard floor — any single critical kills the verdict
elif total_risk_score >= 80:
    HIGH_RISK
elif total_risk_score >= 35:
    CONDITIONAL_FIT
else:
    LIKELY_FIT
```

The "any critical → NOT_RECOMMENDED" floor means **false-positive criticals are the main calibration risk.** The assessor uses `active_physical_count` against `max_physical_interfaces` (with fallback to `max_interfaces`) and `active_physical_by_type` for unsupported-type detection specifically to avoid counting shutdown/legacy ports as if they were live demand.

**Categories of checks (non-exhaustive):**
- Interface scale: physical ports, L2 access/trunk, L3, port-channels, tunnels, subinterfaces, VLANs
- Interface type compatibility (active types vs. `supported_interface_types`)
- Switching: trunking, etherchannel, spanning-tree dependencies
- Routing: VRF, OSPF, EIGRP, BGP support + scale (neighbors, static routes)
- IPv6 dependency
- FHRP: HSRP, VRRP, GLBP
- Security: AAA, TACACS+, RADIUS, SSH, telnet, management ACL
- Services: SNMP, syslog, NTP, NAT, DHCP server, IP SLA, object tracking, flow monitoring
- Policy: QoS
- Crypto: crypto, ISAKMP, IKEv2, IPsec, tunnel interfaces
- Role / design fit: WAN-edge vs. switching-heavy mismatch, service-rich workload signals

### `platform_compare.py`

**Responsibility:** Orchestrate the assessor across all candidate platforms, compute fitness scores, rank, and render reports.

**Inputs:**
- Path to `analysis_report.json`
- Path to `platforms/` directory

**Outputs:**
- `platform_comparison.json` — full structured comparison (consumed by the rendering layer)
- `platform_comparison.md` — multi-platform Markdown comparison
- `platform_comparison.html` — same content as styled HTML
- `best_fit_report.md` — focused single-platform "what should I buy" view
- `best_fit_report.html` — same in styled HTML

**Composition:**

```
build_platform_comparison_reports
  ├─ load_json(analysis_path)
  ├─ load_target_profiles(platforms_dir)        ─► list of profile dicts
  ├─ compare_platforms(analysis, profiles)
  │    └─ for each profile:
  │         ├─ assess_refresh(analysis, profile)              ─► assessment
  │         ├─ compute_platform_fitness(analysis, profile, assessment)
  │         │    └─ _allocate_speed_capacity(...)             ─► allocation
  │         └─ rank_assessment(assessment)                    ─► tie-breaker tuple
  ├─ build_comparison_markdown(comparison, analysis)
  ├─ build_comparison_html(comparison, analysis)
  ├─ build_best_fit_markdown(comparison, analysis)
  └─ build_best_fit_html(comparison, analysis)
```

## Data model

Each pipeline stage writes its output as JSON. The schemas are not formally defined (no JSON Schema or pydantic), but the test suite asserts the key shapes that downstream stages depend on.

| File | Producer | Consumer(s) |
|------|----------|-------------|
| `sanitized_config.txt` | sanitizer | optionally analyzer (when `--analyze-sanitized`) |
| `sanitization_mappings.json` | sanitizer | (operator reference; no downstream code consumes it) |
| `analysis_report.json` | analyzer | assessor (via platform_compare) |
| `platform_comparison.json` | platform_compare | renderers (markdown, HTML), batch summary |
| `_batch_summary.json` | main.py (batch mode) | (final artifact) |

## Platform profile schema

Each YAML in `platforms/` declares one candidate platform. Required top-level sections:

```yaml
platform_name: "Cisco_C8500-20X6C"

capabilities:
  supported_interface_types: [GigabitEthernet, TenGigabitEthernet, ...]
  supports_subinterfaces: true
  supports_trunking: true
  supports_etherchannel: true
  supports_vrf: true
  supports_ospf: true
  supports_bgp: true
  supports_hsrp: true
  supports_aaa: true
  supports_qos: true
  supports_crypto: true
  supports_ipsec: true
  supports_tunnel_interfaces: true
  # ... more boolean feature flags

scale:
  max_interfaces: 36                      # nameplate total capacity
  max_physical_interfaces: 26             # active physical ports the chassis supports
  max_subinterfaces: 2000
  max_vrfs: 64
  max_bgp_neighbors: 1000
  max_static_routes: 16000
  ports:
    native:                               # native port inventory by speed
      1G: 20
      10G: 6
      25G: 0
      40G: 0
      100G: 0
    breakout:                             # breakout-capable counts (advertised, not yet consumed)
      40G_to_4x10G: 0
    reserved_or_dedicated:
      management: 1                       # dedicated management port count

constraints:
  intended_role: "wan_edge"               # influences fitness bonuses

fit_preferences:                          # weighting knobs (multipliers in compute_platform_fitness)
  role_alignment: "high_scale_wan_edge"   # role tag — must match wan_edge_roles set
  role_weight: 8
  throughput_weight: 8
  routing_scale_weight: 9
  services_weight: 8
  crypto_weight: 8
  branch_bias: "low"                      # high|low — biases for compact-branch workloads

notes:                                    # informational findings shown verbatim in report
  - "Best for high-density WAN aggregation."
```

**Falling back when fields are missing:** `max_physical_interfaces` falls back to `max_interfaces`. Missing capability flags default to `False`. Missing scale numbers are treated as no-limit (the corresponding capacity check is skipped).

## Fitness scoring model

`compute_platform_fitness(analysis, profile, assessment)` starts at **1000** and applies additive penalties and bonuses. Higher score = better fit.

### Base penalties (from assessor output)

| Source | Impact |
|--------|--------|
| Overall recommendation: `LIKELY_FIT` | 0 |
| Overall recommendation: `CONDITIONAL_FIT` | -80 |
| Overall recommendation: `HIGH_RISK` | -180 |
| Overall recommendation: `NOT_RECOMMENDED` | -1000 |
| Overall recommendation: `UNKNOWN` | -300 |
| Each critical finding | -250 |
| Each high finding | -80 |
| Each medium finding | -25 |
| Each low finding | -5 |
| `total_risk_score` | × -1.5 |

### Role and preference bonuses

| Source | Impact |
|--------|--------|
| `intended_role == "wan_edge"` | +80 |
| `role_alignment` in WAN-edge family | +`role_weight × 10` |
| Throughput weight | +`throughput_weight × 8` |
| Routing scale weight | +`routing_scale_weight × 10` |
| Services weight | +`services_weight × 8` |
| Crypto weight | +`crypto_weight × 8` |

### Interface-capacity penalties

| Condition | Impact |
|-----------|--------|
| Active physical > max_physical_interfaces | -300 |
| Active physical > 90% of max | -100 |
| Subinterfaces present, target doesn't support | -400 |
| Active subinterfaces > max | -250 |
| Active subinterfaces > 80% of max | -80 |
| Each unsupported interface type in active set | -200 |

### Scale-headroom (for BGP neighbors, static routes, VRFs, tunnels, L3, trunks)

Per metric, with weight `W` per category:

| Utilization | Impact |
|-------------|--------|
| > 100% | -W × 3 |
| > 85% | -W × 1.5 |
| > 70% | -W × 0.75 |
| < 25% | +W × 0.10 |
| 25–70% | +W × 0.30 |

### Workload-presence bonuses

Reward platforms whose `fit_preferences` weights match the workload that's actually configured. Examples:
- BGP present: +`routing_scale_weight × 6`
- NAT present: +`services_weight × 5`
- Crypto present: +`crypto_weight × 5`
- QoS present: +`services_weight × 3`

### Branch-bias calibration

If the platform has `branch_bias: high`:
- Penalize for heavy workloads (BGP neighbors > 100, tunnels > 100, VRFs > 16, static routes > 2000)
- Reward for genuinely compact workloads (all four metrics low simultaneously)

If the platform has `role_alignment: high_scale_wan_edge` but the workload is tiny (BGP < 20, tunnels < 20, VRFs < 4, static routes < 500, L3 < 12), apply a small overkill penalty.

### Switching-heavy penalties (for WAN-edge candidates)

| Condition | Impact |
|-----------|--------|
| Access ports > 12 | -50 |
| Access ports > 24 | -70 (cumulative with above) |
| Access ports > 48 | -100 (cumulative) |

### Routed-workload bonuses

| Condition | Impact |
|-----------|--------|
| L3 interfaces > access ports | +40 |
| BGP + tunnels both present | +40 |
| BGP + VRFs both present | +50 |
| NAT + crypto both present | +30 |

### Speed allocation outcome

If the workload's `active_physical_by_speed_class` can be allocated against the platform's `ports.native` supply (with upward substitution allowed):
- Allocation succeeds: +30
- Each unmet 40G/100G demand: -150
- Each unmet 1G/10G/25G demand: -60
- Total native supply > 1.5× total demand: +25
- Total native supply > 1.2× total demand: +10

### Port-role bonuses

| Condition | Impact |
|-----------|--------|
| Active management port present, target has dedicated management port | +25 |
| Active management port present, target has none | -60 |
| WAN role + active WAN interfaces | +15 |
| Active uplinks/port-channel members but target lacks port-channel support | -80 |
| Large LAN deployment on non-branch platform | -10 |

### Final ordering

Results sort by `(-fitness_score, rank_assessment(...))`. The `rank_assessment` tuple is `(recommendation_rank, critical_count, high_count, medium_count, low_count, total_risk_score)` — tie-breaker for fitness ties.

The `recommended_platform` is the highest-ranked candidate whose `overall_recommendation` is `LIKELY_FIT` or `CONDITIONAL_FIT`. If no candidate qualifies, `best_fit_platform` falls back to the top-ranked candidate but the report displays a warning.

## Speed allocation algorithm

`_allocate_speed_capacity(source_demand, target_native_supply, target_breakout)` greedily allocates speed-class demand against native supply, with upward substitution allowed.

**Substitution hierarchy:**
- 1G demand → can use {1G, 10G, 25G, 40G, 100G} supply
- 10G demand → can use {10G, 25G, 40G, 100G} supply
- 25G demand → can use {25G, 40G, 100G} supply
- 40G demand → can use {40G, 100G} supply
- 100G demand → only 100G supply

**Algorithm:**
1. For each speed in ascending order, try native (exact-speed) match first.
2. If native is exhausted, try upward substitution in ascending order of the substitute speed.
3. Track per-speed `matched_native`, `matched_breakout` (any upward match), and `unmet`.
4. Return `allocation_ok = (unmet_demand == {})`.

**Known gap:** breakout slots are advertised in the result but not yet consumed. Real breakout math (e.g., one 40G port becomes four 10G ports) is on the roadmap.

## Extension points

| You want to… | Edit |
|--------------|------|
| Add support for a new device family (NX-OS, IOS-XR, Arista EOS, Junos, …) | Add a dialect branch or parallel front-end in `analyzer.py` that emits the same `analysis_report.json` shape. Append OS-specific secret patterns to `SECRET_LINE_PATTERNS` in `sanitizer.py`. Add candidate target YAMLs under `platforms/`. Do **not** add vendor conditionals to `assessor.py` or `platform_compare.py`. |
| Add a new candidate platform | Drop a YAML in `platforms/` matching the schema above. |
| Add a new sanitization pattern | Append to `SECRET_LINE_PATTERNS` in `sanitizer.py`. Add a regression test in `tests/test_sanitizer.py`. |
| Extract a new analyzer field | Add it to the appropriate section in `analyzer.py`'s output. Existing assessors won't break (defaults via `_get`), but you'll need to wire it into the assessor explicitly to use it. |
| Add a new compatibility check | Add a finding to `assess_refresh` in `assessor.py`. Pick severity carefully — any `critical` finding flips the verdict to `NOT_RECOMMENDED`. |
| Adjust scoring weights | Edit the constants and bonuses in `compute_platform_fitness` in `platform_compare.py`. Add an E2E test asserting the expected best-fit doesn't regress. |
| Add a new output format | Add a `build_comparison_<format>(comparison, analysis)` function and wire it into `build_platform_comparison_reports`. |
| Change which file extensions are picked up in batch mode | Edit `CONFIG_EXTENSIONS` in `main.py`. |

## Testing

The test suite (`tests/`, run with `python3 -m pytest tests/`) is structured by module:

- **`test_sanitizer.py`** (34 tests) — every redaction pattern, non-secret passthrough cases, encryption-type preservation, IP tokenization, idempotency, the real-world TACACS leak regression.
- **`test_platform_compare.py`** (18 tests) — `_allocate_speed_capacity` parametrized scenarios (direct match, upward substitution, unmet demand, supply accounting), `rank_assessment` ordering, backward-compat with pre-iteration-1 analysis JSON, platform YAML schema validation.
- **`test_pipeline_e2e.py`** (14 tests) — runs the full pipeline against `input/router_config.txt` in a `tmp_path` directory and asserts on the resulting JSON / Markdown / HTML. Includes calibration-health checks: best-fit must earn `LIKELY_FIT` or `CONDITIONAL_FIT`, undersized platforms must still flag interface overflow, STP must not be reported as present for the sample WAN router config.

Total: 66 tests, run in ~1 second.

## Known limitations

| Limitation | Notes |
|------------|-------|
| **Breakout port math** | The allocation algorithm advertises breakout config but doesn't actually consume breakout slots. Adding 4× 10G via a 40G breakout doesn't yet count toward 10G supply. |
| **No committed sample fixture** | The E2E tests expect a config at `input/router_config.txt` and skip gracefully when it is absent. The repo ships with no sample (sensitive inputs are gitignored by default), so contributors must supply their own config to exercise the full pipeline locally. A synthetic small/medium/large fixture set checked into `tests/fixtures/` would close this gap. |
| **Single-dialect coverage** | Only Cisco IOS / IOS-XE is supported today. The pipeline's vendor/OS boundary (see §Vendor / OS boundary) is designed to accept NX-OS, IOS-XR, and non-Cisco input families as new analyzer front-ends, but none have been implemented. |
| **Platform profile data depth** | The four bundled profiles cover capability flags, scale numbers, and port inventory. They do *not* cover license tiers, throughput specs, PoE budgets, or stacking — which would be needed for some refresh decisions. |
| **No charts in HTML** | The HTML report uses styled tables with severity / recommendation badges. Interactive charts (e.g., severity distribution, interface type breakdown) would make the report more skimmable. |
| **No CSV export** | Useful for spreadsheet roll-ups in larger refresh projects. |
| **Field-rename fragility** | Cross-stage field access uses `_get(dct, [path], default)` which silently returns the default on missing keys. Renaming an analyzer field doesn't crash the assessor — it just zeros out the corresponding signal. The test suite catches the obvious cases but isn't exhaustive. |
