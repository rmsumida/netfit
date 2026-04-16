# {{PROJECT_NAME}} — Project Context

> **Purpose:** This document is the single source of truth for every Claude session.
> Last updated: {{DATE}} | Session: 001

---

## 1. Project Summary

| Field | Value |
|-------|-------|
| Project | {{PROJECT_NAME}} |
| Description | {{PROJECT_DESCRIPTION}} |
| Owner | {{OWNER_NAME}} |
| Start date | {{DATE}} |
| Target completion | {{TARGET_DATE}} |

## 2. Strategic Decisions (Locked)

| Decision | Status | Date | Rationale |
|----------|--------|------|-----------|
| (none yet) | | | |

## 3. Goals & Success Criteria

- {{GOAL_1}}
- {{GOAL_2}}

## 4. Financial Position (if applicable)

| Item | Amount |
|------|--------|
| Budget | {{BUDGET}} |
| Spent to date | $0 |
| Remaining | {{BUDGET}} |

## 5. Open Issues

| # | Issue | Status | Blocking? | Next Action |
|---|-------|--------|-----------|-------------|
| (none yet) | | | | |

## 6. Key Contacts

| Role | Name / Company | Contact | Status |
|------|----------------|---------|--------|
| (none yet) | | | |

## 7. Documents in This Repo

| File | Location | Description |
|------|----------|-------------|
| CLAUDE.md | / (repo root) | Auto-context for Claude Code |
| PROJECT_CONTEXT.md | /governance/ | This file — master project state |
| ACTION_ITEMS.md | /governance/ | Master action item tracker |
| SESSION_INDEX.md | /governance/ | One-line index of all sessions |
| DECISIONS.md | /governance/ | Decision register with rationale |
| TRIGGER_COMMANDS.md | /governance/ | Reference for trigger commands |
| PROJECT_INSTRUCTIONS.md | /governance/ | Claude Chat project instructions |
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
