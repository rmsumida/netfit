# CLAUDE.md — {{PROJECT_NAME}}

{{PROJECT_DESCRIPTION}}

## Project Structure

```
governance/
  PROJECT_CONTEXT.md   — Master project state (goals, decisions, issues, contacts, finances)
  ACTION_ITEMS.md      — All action items with status, priority, and notes
  SESSION_INDEX.md     — One-line index of all sessions
  DECISIONS.md         — Decision register with rationale
  TRIGGER_COMMANDS.md  — Reference doc for trigger commands
  PROJECT_INSTRUCTIONS.md — Claude Chat project instructions (not used by Code)
sessions/
  session-NNN.md       — Individual session logs
budget/                — Financial models, spreadsheets
documents/             — Contracts, invoices, reports, deliverables
photos/                — Progress photos, visual documentation
```

## On First Interaction

When the user starts a conversation in this repo, read:
1. `governance/PROJECT_CONTEXT.md`
2. `governance/ACTION_ITEMS.md`
3. `governance/SESSION_INDEX.md`

Then brief the user:
- Current session number (last session in index + 1)
- One-line summary of last session
- Open critical/blocking items
- Recommended focus

Ask: "What happened since last session, and what do you want to focus on today?"

## Trigger: "Session shutdown"

When the user says **"Session shutdown"**:

1. Read the current state of all governance files
2. Ask the user: "What was accomplished this session?" and "Any items to close, add, or update?"
3. Based on their answers, perform ALL of the following:

### Create session log
- Create `sessions/session-NNN.md` with: date, platform (Claude Code), context, work done, decisions made, open items for next session, files created/updated

### Update SESSION_INDEX.md
- Append a new row to the table: `| [NNN](../sessions/session-NNN.md) | YYYY-MM-DD | Summary | Key outcomes |`

### Update ACTION_ITEMS.md
- Mark completed items: change `[ ]` to `[x]`, add completion date and note
- Add new items with sequential numbering, proper phase, priority emoji, and notes
- Update notes on existing items as needed
- **Recalculate the summary table** at the top (count open items by priority category, update closed count)

### Update PROJECT_CONTEXT.md
- Update `Last updated` date and session number
- Update any changed fields: contacts, issues, financial position, completed work

### Update DECISIONS.md
- Add any new decisions with sequential DEC-NNN numbering, status, date, session, rationale, and alternatives considered
- Update status of existing decisions if changed

### Commit and push
```bash
git add -A
git commit -m "Session NNN close-out: <brief summary>"
git push
```

## Between-Session Commands

The user may also give one-off commands outside of a full session. These do NOT create a session log:

- "Mark items X and Y as complete" → Update ACTION_ITEMS.md, commit, push
- "Add a new item: ..." → Add to ACTION_ITEMS.md with proper formatting, commit, push
- "Update item X notes: ..." → Edit the notes field, commit, push

## General Rules

{{PROJECT_RULES}}
