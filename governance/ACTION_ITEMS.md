# Action Items — netfit

> **How to use this file:**
> - Code-level development work is tracked as **GitHub Issues** (`gh issue list`, https://github.com/rmsumida/netfit/issues). This file tracks **project-level / governance items only** (DEC-003).
> - Claude reads this automatically at session start (Chat via Project knowledge, Code via CLAUDE.md)
> - Mark items `[x]` when complete, add completion date in the Notes column
> - Claude updates this at the end of each session
> - Between sessions: `claude "Mark items X and Y as complete in governance/ACTION_ITEMS.md"`
>
> Last updated: 2026-04-16 | Session: 001

---

## Summary

| Status | Count |
|--------|-------|
| 🔴 Open — Critical | 0 |
| 🟡 Open — High | 1 |
| 🟢 Open — Standard | 1 |
| ⚪ Open — Admin | 0 |
| ✅ Closed | 2 |
| **Total** | **4** |

---

## Phase 1: Bootstrap & Migration

| # | Status | Item | Priority | Owner | Notes |
|---|--------|------|----------|-------|-------|
| 1 | [x] | Migrate existing netfit code from `netfit-staging/` into the governance template | 🔴 CRITICAL | Ryan | Closed 2026-04-16 (session 001). Flat layout at root; large docs moved to `documents/`. |
| 2 | [x] | Convert scratchpad TODO items into GitHub Issues | 🟡 HIGH | Ryan | Closed 2026-04-16 (session 001). 6 issues created (#1–#6). |
| 3 | [ ] | Triage duplicate issues created during migration | 🟡 HIGH | Ryan | Issues #1 and #3 both cover platform YAML extension; issues #2 and #5 both cover interactive charts. Merge or close duplicates. |
| 4 | [ ] | Drop a real Cisco IOS/IOS-XE config at `input/router_config.txt` so `tests/test_pipeline_e2e.py` runs locally | 🟢 STANDARD | Ryan | The 9 E2E tests currently skip without it. Gitignored — will not be committed. |

## Closed Items

| # | Item | Closed | Session | Notes |
|---|------|--------|---------|-------|
| 1 | Migrate existing netfit code into governance template | 2026-04-16 | 001 | See session-001.md for full migration log |
| 2 | Convert scratchpad TODO items into GitHub Issues | 2026-04-16 | 001 | Issues #1–#6; scratchpad.md deleted |

---

## Claude Code Quick Commands

```bash
# View all open action items
claude "Show me all open action items from governance/ACTION_ITEMS.md"

# Mark items complete
claude "Mark items 1 and 3 as complete in governance/ACTION_ITEMS.md with today's date"

# Add a new item
claude "Add a new Phase 1 HIGH priority item to governance/ACTION_ITEMS.md: description here"

# Update notes on an item
claude "Update item 6 notes in governance/ACTION_ITEMS.md: new information here"

# Dev work (separate tracker)
gh issue list
```
