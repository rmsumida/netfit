# {{PROJECT_NAME}}

> {{PROJECT_DESCRIPTION}}

## Quick Start — New Session

1. **Sync the repo** (`git pull`)
2. **Claude Chat:** Open a new conversation in the {{PROJECT_NAME}} Project — context loads automatically, Claude briefs you
3. **Claude Code:** `cd` into the repo — `CLAUDE.md` loads automatically, Claude briefs you
4. **Work through the session**
5. **End the session:**
   - In Claude Chat: say **"Shutdown prompt"** → paste the generated prompt into Claude Code
   - In Claude Code: say **"Session shutdown"** → handles everything directly

## Trigger Commands

| Command | Where | What happens |
|---------|-------|--------------|
| **"Shutdown prompt"** | Chat | Generates a close-out prompt to paste into Claude Code |
| **"Session shutdown"** | Code | Directly updates all files, commits, and pushes |

Session start is automatic — no trigger needed. Project instructions (Chat) and CLAUDE.md (Code) handle it.

## Between Sessions — Claude Code

```bash
# Mark items complete
claude "Mark items X and Y as complete in governance/ACTION_ITEMS.md with today's date"

# Add new items
claude "Add a new item to governance/ACTION_ITEMS.md: {{description}}"

# Update notes on an item
claude "Update item X notes in governance/ACTION_ITEMS.md: {{new info}}"

# Commit changes
git add -A && git commit -m "Updated action items" && git push
```

## Repo Structure

```
{{REPO_NAME}}/
├── CLAUDE.md                  ← Auto-context for Claude Code
├── README.md
├── governance/
│   ├── PROJECT_CONTEXT.md     ← Master project state (Claude Chat Project knowledge file)
│   ├── ACTION_ITEMS.md        ← Task tracker (Claude Chat Project knowledge file)
│   ├── SESSION_INDEX.md       ← Lightweight index of all sessions
│   ├── DECISIONS.md           ← Decision register with rationale
│   ├── TRIGGER_COMMANDS.md    ← Reference for the two trigger commands
│   └── PROJECT_INSTRUCTIONS.md ← Paste into Claude Chat Project settings
├── budget/                    ← Financial models, spreadsheets
├── documents/                 ← Contracts, invoices, reports, deliverables
├── sessions/
│   ├── session-001.md         ← Individual session logs
│   └── ...
└── photos/                    ← Progress photos, visual documentation
```

## Governance Rules

### Documents
- **PROJECT_CONTEXT.md** is the single source of truth. Updated at end of every session.
- **ACTION_ITEMS.md** tracks all tasks. Updated at end of every session and between sessions via Claude Code.
- **SESSION_INDEX.md** is a lightweight index. Individual session logs live in `sessions/`.
- **DECISIONS.md** tracks all significant decisions. Locked decisions require new material information to reopen.

### Session Protocol
- Session start is automatic (Project instructions in Chat, CLAUDE.md in Code)
- Session end uses trigger commands: **"Shutdown prompt"** (Chat) or **"Session shutdown"** (Code)
- Session numbering is sequential (001, 002, 003...)

### Decision Status
- **LOCKED** — decided, not revisiting without new material info
- **Under review** — leaning toward a direction, needs more data
- **In progress** — actively being worked
- **Deferred** — intentionally postponed to a later phase

## Tool Ecosystem

| Tool | Purpose |
|------|---------|
| Claude Chat | Primary session interface — strategy, analysis, document generation |
| Claude Code | Repo management, file updates, session close-outs |
| GitHub | Document storage, version control, persistence across sessions |
| Obsidian | (Optional) Local knowledge graph linked to repo |
| VS Code | Direct file editing when needed |
