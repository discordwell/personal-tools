# Session Coach — Claudepad

## Session Summaries

### 2026-06-23 (UTC) — land `--stats` footer + mechanize retry-loop detection

Maintenance pass. Two commits (30 tests total, was 27):

- **Landed the WIP `--stats` footer** (`skill/lib/summarize_session.py`). New `SessionStats` accumulates deterministic, full-session counts (before event elision) into a final `stats` line: `userMsgs`, `interrupts`, `assistantTexts`, `toolCalls`, `toolErrors`, `toolUseByName`, `toolErrorsByName` (errors paired to the failing tool via tool_use id), `filesReadGt3` (re-read thrash), `editRunsByFile` (edit churn). This lets the skill count thrashing/tool-confusion signals from ground truth instead of eyeballing NDJSON — the basis for "same inputs → same patterns". Default output unchanged without the flag. SKILL.md/ARCHITECTURE.md document the parser-vs-skill division of labor. Removed one dead attribute (`_edit_run_len`) the WIP left behind.
- **Mechanized the third thrashing signal — retry loops** (`maxErrorRun`). The WIP mechanized edit-churn and re-read loops but still told the skill to *eyeball* "tool_result.ok false repeatedly", contradicting its own "don't re-count by eye" principle. `maxErrorRun` = longest run of consecutive failing tool_results; a *successful* result resets the streak, intervening tool_use turns (the "try again" turn) do not. `≥2` = a back-to-back failure, `≥3` = a clear loop. SKILL.md Thrashing bullet now reads it from the footer. 3 new tests (unit interleaves tool_use between failures to prove it doesn't reset the streak; e2e drives it through the CLI).

Reviewed via a code-review subagent: core logic confirmed correct/deterministic; one NIT (a unit-test comment overstated its coverage) fixed by interleaving tool_use between the failing results. Committed in two commits; not pushed (orchestrator handles push).

### 2026-06-18 (UTC) — parser correctness: tool-error detection + card rendering

Maintenance pass. Two real defects fixed, each with tests (22 tests total, was 16):

- **Tool errors were 100% invisible to the skill** (`skill/lib/summarize_session.py`). The real Claude Code schema puts `is_error: true` on the tool_result *block*, alongside *string* content — but `_result_brief` only received `content` and only scanned *inner list items* for a marker. Verified against 40 real transcripts: **35/35 errored results were reported as `ok=True`**, and string content dominates (1063 str vs 110 list). This silently defeated the skill's "tool confusion" detector (one of its 4 core patterns), which keys off `tool_result.ok=false`. Fix: `_result_brief` now takes the block-level `is_error` and ORs it with the inner-list fallback. Confirmed on a real session: 4 genuine errors now surface as `ok=False`.
- **Code-snippet cards lost their indentation** (`anki/build_deck.py`). `render_field` converted `\n`→`<br>` but HTML still collapsed leading whitespace, so the documented code-snippet card back (e.g. the `genanki.Note` sample) rendered un-indented. Fix: model CSS now sets `white-space: pre-wrap` and `render_field` keeps newlines/indentation verbatim (escape only). Wet-tested: stored field preserves the literal `\n` + 4-space indent.

Also: hardened `_input_brief` Edit branch against null `old_string`/`new_string` (`None[:60]` would abort a whole summary over one bad line); corrected the `summarize_session.py` module docstring (the listed `summary` event kind was never emitted; added the real `header`/`elided` lines); added direct unit tests for the previously-untested `_input_brief`/`_result_brief` and for `render_field`.

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
- **JSONL schema** (observed during recon, re-verified 2026-06-18 against 40 real transcripts): each line has top-level `type` field (`user`/`assistant`/`system`/`ai-title`/`last-prompt`/`attachment`); user `message.content` may be a string OR a list with `tool_result` blocks. **Tool errors are flagged by `is_error: true` on the tool_result block itself (a sibling of `content`/`tool_use_id`/`type`), and the block's `content` is usually a plain string** (1063 str vs 110 list across the sample; every observed error had string content). So `_result_brief` must read the block-level `is_error` — scanning only inner list items misses ~all errors (the 2026-06-18 fix). Assistant `message.content` is a list of `text`/`tool_use`/`thinking`/`fallback` blocks; the parser keeps `text`+`tool_use`, ignores `thinking`/`fallback`. Subagent transcripts live in subdirs of the project slug — `list_sessions.py` skips them.
- **Skill install**: not yet symlinked. To activate: `ln -s /Users/discordwell/Projects/personal-tools/session-coach/skill ~/.claude/skills/session-coach`.
- **Anki venv**: `anki/.venv/` (Python 3.12.12). Activate with `.venv/bin/python` for the smoke test or CLI runs.
