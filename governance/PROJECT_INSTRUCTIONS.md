# netfit — Project Instructions

You are assisting with netfit, an offline hardware-refresh planning tool for network devices (Cisco IOS/IOS-XE today; designed to accept additional dialects additively). This is a multi-session project with full context maintained across sessions via governance files in this project's knowledge base.

## On Every New Conversation

At the start of every new conversation, automatically:

**Step 1 — GitHub Sync Check:**
Fetch the latest governance files from GitHub to ensure Project Knowledge is current:
1. Fetch latest commit: `curl -s -H "Authorization: Bearer <PAT>" "https://api.github.com/repos/rmsumida/netfit/commits?per_page=1"` — extract SHA, date, message
2. Fetch PROJECT_CONTEXT.md: `curl -s -H "Authorization: Bearer <PAT>" "https://api.github.com/repos/rmsumida/netfit/contents/governance/PROJECT_CONTEXT.md"` — decode base64, extract Last Updated date and session number
3. Fetch ACTION_ITEMS.md: `curl -s -H "Authorization: Bearer <PAT>" "https://api.github.com/repos/rmsumida/netfit/contents/governance/ACTION_ITEMS.md"` — decode base64, extract summary counts and open critical items
4. Fetch SESSION_INDEX.md: `curl -s -H "Authorization: Bearer <PAT>" "https://api.github.com/repos/rmsumida/netfit/contents/governance/SESSION_INDEX.md"` — decode base64, extract last session row (date, summary, key outcomes)
5. Compare against Project Knowledge files. If divergent, flag it and use the **GitHub version as source of truth**.

GitHub API credentials:
- Repo: `rmsumida/netfit`
- PAT: `<insert personal access token here when configuring the Claude Chat project>`

**Step 2 — Session Brief:**
Respond with:
> **Session NNN** | Last session (YYYY-MM-DD): [one-line summary from SESSION_INDEX.md]
>
> **GitHub sync:** [in sync / divergent — details]
>
> **Open critical items:**
> - [list any 🔴 CRITICAL unchecked items from ACTION_ITEMS.md]
> - [also surface any `priority:critical` open GitHub Issues]
>
> **Expected this session:**
> - [items called out as "open for next session" in the last session log, OR unchecked HIGH priority items from ACTION_ITEMS.md]
>
> **Recommended focus:** [what you think is highest-value today based on critical path and blocking items]

Then ask: "What happened since last session, and what do you want to focus on today?"

Do NOT wait for the user to say "Session start" or any trigger phrase. Just do this automatically on the first message of every conversation.

## Trigger: "Shutdown prompt"

When the user says **"Shutdown prompt"**, generate a single code-fenced prompt that the user will paste into Claude Code. This prompt must instruct Claude Code to:

1. Create `sessions/session-NNN.md` with a full session log (date, platform, context, work done, decisions, open items, files created/updated)
2. Append a row to `governance/SESSION_INDEX.md` with session number, date, summary, and key outcomes
3. Update `governance/ACTION_ITEMS.md` — close completed items (with date + note), add new items, update notes on existing items, recalculate the summary table counts at the top
4. Update `governance/PROJECT_CONTEXT.md` — update Last Updated date, session number, and any changed fields (contacts, issues, decisions)
5. Update `governance/DECISIONS.md` — add any new decisions with full rationale, or note "no changes"
6. Run: `git add -A && git commit -m "Session NNN close-out: <summary>" && git push`

**Format rules for the generated prompt:**
- Wrap the entire prompt in a single code block
- The first line must be: `claude "Session close-out for netfit.`
- The closing line must be: `Then: git add -A && git commit -m 'Session NNN close-out: <summary>' && git push"`
- This allows the user to copy-paste the entire block directly into the terminal
- Be thorough and specific — Claude Code needs exact content for the session log, exact item numbers to close/add/update, and exact field changes for each governance file. Do not be vague.

## General Rules

### Architectural invariants (do not regress)

1. **Vendor/OS boundary (DEC-001, LOCKED)** — only `analyzer.py` parses config dialect. Assessor, scoring, and reporting are vendor-neutral. Adding a new dialect must not require changes to `assessor.py` or `platform_compare.py`.
2. **Determinism** — same input + same `rules.yaml` + same `platforms/*.yaml` → byte-identical output. No network I/O, no API calls, no model inference.
3. **Active-interface semantics** — assessor and scoring read `active_physical_count` / `active_physical_by_type`, not nameplate counts. Don't regress to scoring against `interfaces.total` or `by_type`.
4. **Explicit secret patterns** — no generic `\bpassword\b` / `\bkey\b` fallback in the sanitizer. Every secret-bearing command has its own three-group `(prefix, secret, suffix)` regex.

### Work-tracking split

- **Code-level dev work** → GitHub Issues (`gh issue list`, https://github.com/rmsumida/netfit/issues)
- **Governance / project-level items** → `governance/ACTION_ITEMS.md`
