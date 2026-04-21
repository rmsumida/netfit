# netfit — Project Context

> **Purpose:** Single source of truth for every Claude session.
> Last updated: 2026-04-20 | Session: 003

---

## 1. Project Summary

| Field | Value |
|-------|-------|
| Project | netfit |
| Description | Offline, deterministic hardware-refresh planning tool for network devices. Analyzes Cisco IOS/IOS-XE running-configs and ranks candidate replacement platforms by fit. |
| Owner | Ryan |
| Repo | https://github.com/rmsumida/netfit |
| Start date | 2026-04-16 |
| Target completion | Ongoing |

## 2. Strategic Decisions (Locked)

| Decision | Status | Date | Rationale |
|----------|--------|------|-----------|
| DEC-001 — Vendor/OS boundary: only `analyzer.py` parses config dialect | LOCKED | 2026-04-16 | Keeps new-dialect onboarding additive; assessor and scoring stay vendor-neutral |
| DEC-002 — Flat repo layout: code at root, governance alongside | LOCKED | 2026-04-16 | netfit is the repo; a nested `src/` adds no value |
| DEC-003 — GitHub Issues for dev work; ACTION_ITEMS.md for governance | LOCKED | 2026-04-16 | Avoids duplicating issue data across two trackers |
| DEC-004 — Runtime parsers keyed by intent, not raw command string | LOCKED | 2026-04-19 | Platform/train variation (validated ASR1013/16.03.07) means one intent has multiple exact-command aliases |

## 3. Goals & Success Criteria

- Ship a deterministic, offline pipeline that ranks replacement platforms for a given config (done — Cisco IOS/IOS-XE)
- Preserve the vendor/OS boundary so new dialects (NX-OS, IOS-XR, Arista, Juniper) can be added without touching assessor / scoring / reporting
- Keep the 208-test pytest suite green; every sanitizer regex change adds a regression test
- Same input + same rules + same platforms → byte-identical output, every time

## 4. Financial Position

N/A — open-source tooling project. No budget tracked.

## 5. Open Issues

Development work is tracked as GitHub Issues. Run `gh issue list` or see https://github.com/rmsumida/netfit/issues.

Governance-level issues (non-code):

| # | Issue | Status | Blocking? | Next Action |
|---|-------|--------|-----------|-------------|
| (none yet) | | | | |

## 6. Key Contacts

| Role | Name / Company | Contact | Status |
|------|----------------|---------|--------|
| Owner / primary developer | Ryan | hachigen888@gmail.com | Active |

## 7. Documents in This Repo

| File | Location | Description |
|------|----------|-------------|
| CLAUDE.md | / (repo root) | Auto-context for Claude Code (governance + technical) |
| README.md | / | Public-facing product README |
| PROJECT_CONTEXT.md | /governance/ | This file — master project state |
| ACTION_ITEMS.md | /governance/ | Governance / project-level action tracker |
| SESSION_INDEX.md | /governance/ | One-line index of all sessions |
| DECISIONS.md | /governance/ | Decision register with rationale |
| TRIGGER_COMMANDS.md | /governance/ | Reference for trigger commands |
| PROJECT_INSTRUCTIONS.md | /governance/ | Claude Chat project instructions |
| ARCHITECTURE.md | /documents/ | Full architecture and design document |
| NETBRAIN_HARVEST.md | /documents/ | NetBrain feature/data harvest notes |
| session-NNN.md | /sessions/ | Individual session logs |

## 8. Session Protocol

### Session Start (Automatic — no trigger needed)
- **Claude Chat:** Project knowledge files load automatically. Claude briefs you on first message.
- **Claude Code:** `CLAUDE.md` instructs Claude Code to read governance files and brief you on first interaction.

### Two Trigger Commands

| Command | Where | What it does |
|---------|-------|--------------|
| **"Shutdown prompt"** | Claude Chat (end of session) | Generates a prompt you paste into Claude Code to close out the session |
| **"Session shutdown"** | Claude Code (end of session) | Directly closes out the session — updates all files, commits, pushes |

### Between Sessions (Claude Code)
- Mark items complete: `claude "Mark items X and Y as complete in governance/ACTION_ITEMS.md with today's date"`
- Add new items: `claude "Add a new item to governance/ACTION_ITEMS.md: ..."`
- Update notes: `claude "Update item X notes in governance/ACTION_ITEMS.md: ..."`

---

*This document is machine-generated and maintained by Claude across sessions.*
