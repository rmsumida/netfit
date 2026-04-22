# Decision Register — netfit

> Tracks all significant decisions with rationale, alternatives considered, and status.
> Decisions marked LOCKED should not be revisited without new material information.

---

## DEC-001: Vendor/OS boundary — analyzer.py is the only dialect-aware stage

| Field | Value |
|-------|-------|
| Status | LOCKED |
| Date | 2026-04-16 |
| Session | 001 |

**Decision:** Only `analyzer.py` parses a specific config dialect. The assessor (`assessor.py`), scoring / ranking (`platform_compare.py`), and report rendering are vendor-neutral and consume the JSON schema produced by the analyzer.

**Alternatives Considered:**
1. Per-dialect end-to-end pipelines — each dialect gets its own analyzer, assessor, and scoring. Rejected: duplicates scoring/reporting logic and makes calibration drift across dialects.
2. Generic parser that auto-detects dialect — Rejected: parsing quality suffers; explicit front-ends are clearer and more maintainable.

**Rationale:** Adding a new dialect (NX-OS, IOS-XR, Arista, Juniper) should be an additive change — a new analyzer front-end emitting the same `analysis_report.json` shape, plus new sanitizer patterns and new platform YAMLs. Assessor and scoring logic must not change per dialect, or calibration becomes per-dialect and unmaintainable.

**Conditions for revisiting:** A dialect whose workload semantics genuinely don't map to the current `analysis_report.json` schema — at which point the schema, not the boundary, is what would need extending.

---

## DEC-002: Flat repo layout

| Field | Value |
|-------|-------|
| Status | LOCKED |
| Date | 2026-04-16 |
| Session | 001 |

**Decision:** All code and configuration lives at the repo root. Governance scaffolding (`governance/`, `sessions/`) sits alongside. No nested `src/` or `netfit/` package directory.

**Alternatives Considered:**
1. Nest code under `src/netfit/` — standard Python layout. Rejected: no install story (this is a CLI tool, not a library), single-package, and it would push every import path one level deeper without benefit.
2. Split code and governance into separate repos — Rejected: the governance overlay is useful precisely because it travels with the code.

**Rationale:** The repo *is* netfit. A nested package dir would add ceremony without clarifying anything. Governance files are prefixed (`governance/`, `sessions/`) so they don't crowd the code root.

**Conditions for revisiting:** If netfit ever ships as an installable package on PyPI, reassess.

---

## DEC-003: GitHub Issues as the dev-work tracker

| Field | Value |
|-------|-------|
| Status | LOCKED |
| Date | 2026-04-16 |
| Session | 001 |

**Decision:** Code-level development work (features, bugs, enhancements) is tracked as GitHub Issues. `governance/ACTION_ITEMS.md` is reserved for project-level / governance items that don't belong on an issue tracker.

**Alternatives Considered:**
1. Everything in `ACTION_ITEMS.md` — the template's default. Rejected: duplicates issue data and loses GitHub's native linking to commits/PRs.
2. Everything in GitHub Issues — no `ACTION_ITEMS.md` at all. Rejected: some governance items (vendor decisions, calibration review cadence, doc updates) don't belong as code issues.

**Rationale:** Dev work benefits from GitHub's commit/PR cross-linking and triage workflows. Governance items benefit from being versioned alongside the rest of the governance docs so they're visible in session briefings.

**Conditions for revisiting:** If the split causes confusion about where a given item lives, reconsider — but the rule of thumb (code change → issue; cadence / decision / stakeholder item → `ACTION_ITEMS.md`) should scale.

---

## DEC-004: Runtime parsers keyed by intent, not raw command string

| Field | Value |
|-------|-------|
| Status | LOCKED |
| Date | 2026-04-19 |
| Session | 002 |

**Decision:** `runtime_parsers.py` exposes one function **per intent key** (e.g., `crypto_ipsec_summary`, `license_summary`, `optics`), not one per exact show-command string. The loader normalizes each incoming `command` field to an intent key via an alias map before dispatching. Parsers accept `(raw_text, source_command)` so they can branch on alias when output shapes differ.

**Alternatives Considered:**
1. One parser per exact command string (`parse_show_crypto_ipsec_sa_count`, `parse_show_crypto_ipsec_sa`, etc.) — Rejected: explodes parser count linearly with train/platform variants; adding a new train would mean adding new parser modules instead of a one-line alias entry.
2. Single mega-parser that sniffs output format and self-routes — Rejected: mixes dispatch and parsing, harder to unit-test, and output-sniffing is fragile vs. an explicit alias map driven by the known command string that was run.

**Rationale:** Validated on Cisco ASR 1013 / IOS-XE 16.03.07 (session 002) — the modern-IOS-XE commands `show crypto ipsec sa count`, `show license summary`, and `show interfaces transceiver [detail]` all return `% Invalid input` on that older train. The working aliases (`show crypto ipsec sa`, `show license all`) produce outputs that are strict supersets of the modern short-form outputs, so a single intent-keyed parser can absorb both inputs with a light branch. This keeps the parser count bounded by intents (~15) rather than by the Cartesian product of (intent × train × dialect).

**Conditions for revisiting:** A situation where two aliases of the same intent have such divergent output shapes that the "branch on source_command" pattern becomes unwieldy — at which point split the parser, but keep the intent-keyed loader dispatch.

---

## DEC-005: Dual-track fixture strategy — greenfield-reference + production-shape fixtures coexist

| Field | Value |
|-------|-------|
| Status | LOCKED |
| Date | 2026-04-20 |
| Session | 003 |

**Decision:** Lab-generated and public-reference fixtures are maintained on two parallel tracks that serve different purposes and neither deprecates the other:
- **Greenfield-reference** — represents what new deployments *should* look like (e.g., IKEv2 + per-partner VRF + tunnel protection). Used for architectural documentation and onboarding. First example: `documents/ipsec_pattern_a_spec.md`.
- **Production-shape** — represents what real production configs actually look like today (e.g., crypto-map legacy IPsec + global-table + BGP peer-group-scoped policy). Used for parser robustness and regression coverage. First example: `documents/crypto_map_global_wan_edge_spec.md`.

Each fixture's `SOURCES.md` labels which track it belongs to.

**Alternatives Considered:**
1. Only production-shape — Rejected: loses architectural-intent documentation; onboarding material would reflect legacy shapes rather than the direction new work should take.
2. Only greenfield-reference — Rejected: loses parser coverage on real-world shapes, which is exactly where sanitizer / analyzer bugs surface.
3. Single blended fixture — Rejected: neither faithful to production nor aspirational for greenfield; serves no audience well.

**Rationale:** Parser robustness and architectural documentation are genuinely different requirements and trying to satisfy both with one fixture family produces a fixture that satisfies neither. The production-shape track exists because real configs are messier than the greenfield ideal; the greenfield track exists because netfit should also be able to show what good looks like.

**Conditions for revisiting:** If the two tracks start drifting in coverage such that one dominates and the other atrophies, reassess whether both are still pulling their weight — but do not collapse them into a single family without an explicit decision.

---

## DEC-006: Sanitizer covers both config and runtime bodies; runs before parser dispatch in both paths

| Field | Value |
|-------|-------|
| Status | LOCKED |
| Date | 2026-04-21 |
| Session | 005 |

**Decision:** `CiscoConfigSanitizer` is no longer a config-only tool. In the combined-harvest path, one sanitizer instance is constructed per device and applied to (a) the extracted running-config body and (b) every runtime harvest record body, in both cases **before** any parser or analyzer sees the text. Token-ID counters are instance-level on `TokenMapper`, which means the config and all N runtime bodies share one monotonically-numbered keyspace and one merged `sanitization_mappings.json`. The sanitizer grows explicit `(prefix, secret, suffix)` patterns for runtime-output artifacts (chassis / module serial numbers, license UDI, smart-license Registration Tokens) alongside the existing config-line secret patterns — no generic `\bpassword\b` / `\bkey\b` catch-all is introduced.

**Alternatives Considered:**
1. Leave the sanitizer config-only and filter runtime-side secrets at the parser — rejected: couples every parser to redaction policy, duplicates sanitizer state, breaks the single-source-of-truth mapping file, and forces parser changes every time sanitizer rules move.
2. Run a fresh sanitizer instance per body — rejected: token counters would reset to 001 on each body, the same IP seen in the config and in `show ip route` output would get two different tokens, and `sanitization_mappings.json` would need per-body sub-namespaces (or worse, collisions).
3. Run sanitization only when the input is a combined harvest — rejected: asymmetric behavior between `--runtime-csv` and combined modes, same leak surface remains for two-file workflow users; the runtime patterns are cheap enough to run unconditionally.

**Rationale:** The combined-harvest path fundamentally changes operator expectations. The moment the config and the runtime arrive in one file, "I enabled sanitization" means "sanitize everything in this file" — no reasonable user model treats it otherwise. A single sanitizer instance applied to every body, with instance-level monotonic token counters, keeps the keyspace unified and keeps `sanitization_mappings.json` authoritative. The runtime-specific patterns (SN / UDI / smart-license-token) are draft-validated from sample output in `documents/NETBRAIN_HARVEST.md` and retain the same three-group `(prefix, secret, suffix)` shape as existing secret patterns so the "no generic fallback" invariant continues to hold.

The determinism implication — same combined-harvest input → byte-identical `analysis_report.json` across runs — is guarded by `test_combined_harvest_determinism` in `tests/test_pipeline_e2e.py`. The instance-level counter invariant itself is guarded by `test_sanitizer_token_counter_persists_across_sanitize_calls` in `tests/test_sanitizer.py`.

**Conditions for revisiting:** A future dialect (NX-OS, IOS-XR) whose runtime-output secret shapes can't be reduced to explicit three-group patterns — at which point the decision to revisit is the **three-group convention**, not the "sanitize runtime too" principle.

---

*New decisions should be added with sequential numbering (DEC-007, etc.)*
