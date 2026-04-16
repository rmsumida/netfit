# Trigger Commands Reference — {{PROJECT_NAME}}

> Two commands manage session close-out. Session start is automatic — no trigger needed.

---

## Session Start (Automatic)

**Claude Chat:** Project knowledge files (`PROJECT_CONTEXT.md` + `ACTION_ITEMS.md`) are loaded automatically via Claude Project. Claude briefs you on status at the start of every new conversation. No trigger needed.

**Claude Code:** `CLAUDE.md` in the repo root instructs Claude Code to read governance files and brief you on first interaction. No trigger needed.

---

## "Shutdown prompt"

**Where:** Claude Chat (end of session)
**What you do:** Say "Shutdown prompt". Copy the generated prompt into Claude Code.

**What Claude Chat generates:** A single prompt you paste into Claude Code that:

1. **Creates** `sessions/session-NNN.md` — full session log
2. **Appends row** to `governance/SESSION_INDEX.md`
3. **Updates** `governance/ACTION_ITEMS.md` — close items, add items, update notes, recalculate summary
4. **Updates** `governance/PROJECT_CONTEXT.md` — changed fields
5. **Updates** `governance/DECISIONS.md` — new or changed decisions
6. **Commits and pushes** to GitHub

---

## "Session shutdown"

**Where:** Claude Code (end of session, when working directly in Code)
**What you do:** Say "Session shutdown".

**What Claude Code does:**
1. Reads the current state of all governance files
2. Asks you: "What was accomplished this session?"
3. Asks you: "Any items to close, add, or update?"
4. Creates `sessions/session-NNN.md`
5. Updates all governance files
6. Commits and pushes to GitHub

---

## Between Sessions (Claude Code)

One-off commands. No session log created.

```bash
claude "Mark items X and Y as complete in governance/ACTION_ITEMS.md with today's date"
claude "Add a new HIGH priority item to governance/ACTION_ITEMS.md: description"
claude "Update item X notes in governance/ACTION_ITEMS.md: new info"
```
