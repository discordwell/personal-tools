# Session Coach — Claudepad

## Session Summaries

### 2026-05-07 (UTC) — initial scaffold + dual-memory wiring

Built session-coach as the first tool under the new `personal-tools` monorepo. Two components, scaffolded in parallel by subagents:

- `anki/` — Python CLI (`build_deck.py`) using `genanki==0.13.1` to produce stable-ID `.apkg` files from a JSON card list. Subdeck routing by first tag, HTML-escaped fields, smoke test verifies the output is a valid zip + SQLite db with the expected note count.
- `skill/` — Claude Code skill (`SKILL.md` + `lib/list_sessions.py` + `lib/summarize_session.py`) that reads `~/.claude/projects/*.jsonl` transcripts, detects thrashing / tool-confusion / pushback / knowledge-gaps across the last N days, and produces three outputs: a prompting-journal entry, `feedback`-type auto-memories, and an Anki deck.

Refined the skill so memory entries are written to **both** `~/.claude/projects/-Users-discordwell-Projects-bad-idea-discord/memory/` (anchor) and the cwd's project memory dir (de-duped if they coincide). Insights aggregate in bad-idea-discord regardless of where `/session-coach` is invoked from.

Project moved from `~/Projects/session-coach/` into `~/Projects/personal-tools/session-coach/` to live in a shared personal-tools repo. SKILL.md absolute paths updated to match.

## Key Findings

- **Stable Anki IDs**: `sha1(front + "\n" + back) mod 2^31` for note GUIDs; deck and model IDs use separate domain prefixes (`note:`, `deck:`, `model:`) to prevent collisions across the three ID spaces. Re-imports update existing cards rather than duplicating.
- **Anchor memory dir**: `~/.claude/projects/-Users-discordwell-Projects-bad-idea-discord/memory/`. Session-coach always writes here, plus the cwd's project memory dir as a secondary target.
- **JSONL schema** (observed during recon): each line has top-level `type` field (`user`/`assistant`/`system`/`ai-title`/`last-prompt`/`attachment`); user `message.content` may be a string OR a list with `tool_result` blocks; tool errors marked via `is_error: true` or nested `type: "tool_use_error"`. Subagent transcripts live in subdirs of the project slug — `list_sessions.py` skips them.
- **Skill install**: not yet symlinked. To activate: `ln -s /Users/discordwell/Projects/personal-tools/session-coach/skill ~/.claude/skills/session-coach`.
- **Anki venv**: `anki/.venv/` (Python 3.12.12). Activate with `.venv/bin/python` for the smoke test or CLI runs.
