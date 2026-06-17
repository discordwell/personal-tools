# Session Coach — Claudepad

## Session Summaries

### 2026-06-17 (UTC) — hardening pass: bug fixes, tests, repo-level tooling

Maintenance pass over the scaffold. Fixed five real defects, each with a test (16 tests total, was 9):

- **GUID collision (data loss)** in `anki/build_deck.py`: `note_id` joined `f"{front}\n{back}"`, so `("a\nb","c")` and `("a","b\nc")` hashed identically → one card silently clobbered the other on Anki import. Now length-framed: `f"{len(front)}:{front}{back}"` (injective).
- **Duplicate cards over-counted**: two cards with identical front+back produced two notes with the same GUID; CLI claimed "wrote N" but Anki collapses them on import. Builder now dedups (keeps first), warns on stderr, reports the true count.
- **`summarize_session.py` ignored `isMeta`**: harness-injected user records (caveats, command output, reminders) were scored as real `user_msg` → false pushback/knowledge-gap signal. New `meta` event kind; SKILL.md Step 3 says to ignore them.
- **`--max-events 1` leaked the whole transcript**: `tail==0` made `out[-tail:]` == `out[0:]` (Python `-0==0`). Guarded with an explicit empty slice.
- **Arg validation**: `--count`/`--days` rejected negatives (a negative `--count` sliced `jsonls[:-1]`, dropping the oldest file); `--max-text`/`--max-events` require ≥1; `_truncate` guards n<1.

Added repo-level tooling: root `README.md`, `Makefile` (`make venv`/`make test`/`make clean`), `pyproject.toml` (pytest config, `--import-mode=importlib` so future tools' same-named test files don't break collection), `requirements-dev.txt`. Single command now runs everything: `make test`.

Reviewed via 3 parallel code-review agents; both surfaced findings (max-events=1 elision, duplicate-basename fragility) are fixed and verified. Committed; not pushed (orchestrator handles push).

### 2026-05-07 (UTC) — initial scaffold + dual-memory wiring

Built session-coach as the first tool under the new `personal-tools` monorepo. Two components, scaffolded in parallel by subagents:

- `anki/` — Python CLI (`build_deck.py`) using `genanki==0.13.1` to produce stable-ID `.apkg` files from a JSON card list. Subdeck routing by first tag, HTML-escaped fields, smoke test verifies the output is a valid zip + SQLite db with the expected note count.
- `skill/` — Claude Code skill (`SKILL.md` + `lib/list_sessions.py` + `lib/summarize_session.py`) that reads `~/.claude/projects/*.jsonl` transcripts, detects thrashing / tool-confusion / pushback / knowledge-gaps across the last N days, and produces three outputs: a prompting-journal entry, `feedback`-type auto-memories, and an Anki deck.

Refined the skill so memory entries are written to **both** `~/.claude/projects/-Users-discordwell-Projects-bad-idea-discord/memory/` (anchor) and the cwd's project memory dir (de-duped if they coincide). Insights aggregate in bad-idea-discord regardless of where `/session-coach` is invoked from.

Project moved from `~/Projects/session-coach/` into `~/Projects/personal-tools/session-coach/` to live in a shared personal-tools repo. SKILL.md absolute paths updated to match.

## Key Findings

- **Stable Anki IDs**: note GUID = `sha1("note:" + f"{len(front)}:{front}{back}") mod 2^31`. The length-frame makes the key injective over `(front, back)` — the earlier `front + "\n" + back` join collided when a newline could migrate across the boundary (fixed 2026-06-17). Deck and model IDs use separate domain prefixes (`note:`, `deck:`, `model:`) to prevent collisions across the three ID spaces. Re-imports update existing cards rather than duplicating; identical cards within one build are deduped (first wins).
- **Anchor memory dir**: `~/.claude/projects/-Users-discordwell-Projects-bad-idea-discord/memory/`. Session-coach always writes here, plus the cwd's project memory dir as a secondary target.
- **JSONL schema** (observed during recon): each line has top-level `type` field (`user`/`assistant`/`system`/`ai-title`/`last-prompt`/`attachment`); user `message.content` may be a string OR a list with `tool_result` blocks; tool errors marked via `is_error: true` or nested `type: "tool_use_error"`. Subagent transcripts live in subdirs of the project slug — `list_sessions.py` skips them.
- **Skill install**: not yet symlinked. To activate: `ln -s /Users/discordwell/Projects/personal-tools/session-coach/skill ~/.claude/skills/session-coach`.
- **Anki venv**: `anki/.venv/` (Python 3.12.12). Activate with `.venv/bin/python` for the smoke test or CLI runs.
