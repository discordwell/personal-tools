# Session Coach — Claudepad

## Session Summaries

### 2026-07-08 (UTC) — mechanize cross-session aggregation (`aggregate_sessions.py`)

Maintenance pass. Closed the last "counted by eye" gap in the determinism guarantee (52 tests total, was 38):

- **Grounded the parser against reality first.** Scanned all 94 real transcripts: assistant `message.content` is *always* a list (5274/5274 — the `if not isinstance(content, list): continue` guard never trips in practice); `is_error==True` tool_results *always* carry string content (46/46) — reconfirming the 2026-06-18 block-level fix. No schema bug; the parser is correct.
- **Refactored `summarize_session.py`** to expose a reusable `parse_session(path, max_text) -> (header, events, stats)`. Pure, side-effect-free core; `main()` now layers elision + printing on top. CLI output is byte-identical (verified by the existing 21 tests + a smoke check). +1 unit test locking the contract.
- **Added `skill/lib/aggregate_sessions.py`** — rolls the per-session `--stats` counts up across all analyzed sessions into one deterministic `aggregate` line: `totals`, summed `toolUseByName`/`toolErrorsByName`, `sessionsWith{BackToBackFailure,RetryLoop,ReReadThrash,EditChurn}`, `reReadFiles`/`editChurnFiles` (ranked, with per-file session-count + intensity), and `sessionsByErrors` (worst sessions, for evidence). Reuses `parse_session` (no parser drift). Reads paths positionally or on stdin, so it composes as `list_sessions.py | aggregate_sessions.py`. Skips empty/missing sessions with a stderr note. 13 new tests.
- **Wet-tested on the real 94-session corpus:** coherent output (Edit is the most error-prone tool cross-session — 21 errs/522 uses vs Bash 17/831; `scp.py` re-read across 2 sessions; `decks.py`/`config.sh`/`test-classify.sh` churned across 2 sessions each). **Determinism proven on real data:** byte-identical run-to-run *and* across a fully reversed 94-path input.
- Wired into SKILL.md Step 2/3 (aggregate is now the source for cross-session counts; footers for per-session detail), ARCHITECTURE.md (component + data flow + division-of-labor), and the ≥2-session reportability test is now mechanical.
- **Code-review subagent caught a real determinism bug I'd introduced** (54 tests total after the fix): `sessionsByErrors` tie-broke on the full sessionId, which collapses to `""` for any transcript lacking a `sessionId` — so two such distinct sessions tying on error counts fell back to *input order*, flipping the whole aggregate line on reversed input (the exact guarantee under review; real transcripts always carry a sessionId, so the wet test missed it). Fixed by tie-breaking on the input **file path** (unique per distinct session file, folded once), which is a genuine total order. Proved the old key was order-dependent (`[alpha,bravo]` vs `[bravo,alpha]`) and the new one isn't; added a regression test with two no-sessionId sessions. Also deduped input paths so a repeated path can't double-count (makes "pure function of the session *set*" literally true).

Committed on `main`; not pushed (orchestrator handles push).

### 2026-06-24 (UTC) — fix nondeterministic session ordering in `list_sessions.py`

Maintenance pass. One real defect fixed, with a verified regression test plus a broader coverage backfill (38 tests total, was 30):

- **Nondeterministic mtime tie-break** (`skill/lib/list_sessions.py`). The sort was `key=lambda x: x[0], reverse=True` — mtime only. Equal-mtime transcripts (batch-created files, or coarse-resolution filesystems) were then ordered by `iterdir()`, which is filesystem-dependent. With `--count N`, a tie at the cutoff boundary would include *different* sessions run to run — directly violating SKILL.md's "same inputs → same patterns surfaced" guarantee. Fix: sort on `(-x[0], str(x[1]))` — newest first, ties broken by path ascending. Deterministic regardless of directory-iteration order. Verified the new regression test genuinely *fails* against the old sort (old returned creation/iterdir order `[zzz, mmm, aaa]`; fixed returns `[aaa, mmm, zzz]`).
- **Backfilled `list_sessions.py` core-logic tests** (`skill/lib/test_list_sessions.py`). The file's whole reason for existing — time/count filtering + newest-first sorting — was previously untested (existing tests only covered min-bytes, missing-root, negative-arg rejection). Added 8: newest-first sort on distinct mtimes, `--count` keeps newest N, `--count 0` returns nothing, `--days` excludes old files, default 7-day window, the tie-break regression, subagent-subdir files skipped (glob is non-recursive), `--count`/`--days` mutual exclusivity. mtimes are stamped via `os.utime` so the tests are write-order-independent; `--days` margins are days-wide so subprocess-startup drift can't flip a result.

Reviewed via a code-review subagent: confirmed the `(-mtime, path)` key is sound (float negation is exact; path strings compare lexicographically), the regression test is a genuine guard (not a coincidental pass), and the time-based tests are non-flaky. No findings. Committed on `main`; not pushed (orchestrator handles push).

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
- **Deterministic session ordering** (`list_sessions.py`): files are sorted `(-mtime, path)` — newest first, mtime ties broken by path ascending. The tie-break is load-bearing for the determinism guarantee: `iterdir()` order is filesystem-dependent, so sorting on mtime alone let equal-mtime transcripts order by filesystem luck, and `--count N` could then pick different sessions at a tie boundary (fixed 2026-06-24). Same principle as `summarize_session._ranked`, which already breaks count ties on key name.
- **Cross-session aggregation** (`skill/lib/aggregate_sessions.py`, added 2026-07-08): the parser's exact per-session counts are only reproducible *per session*; the skill still had to sum ≤25 footers by eye to answer "does this pattern span ≥2 sessions?" — the error-prone counting the whole `--stats` effort exists to kill. `aggregate_sessions.py` reuses `summarize_session.parse_session` (extracted the same day; the parsing core is now shared, no drift) and folds every session's `SessionStats.to_dict()` into one `aggregate` line. It is a **pure function of the session set**: every roll-up is order-independent (sums / Counters / per-file maxima) and every emitted order is a total sort on count-then-name, so it's byte-identical run-to-run and across reversed input (verified on the real 94-session corpus). This makes SKILL.md's ≥2-session reportability test mechanical (`sessionsWith*` counts, per-file `sessions` fields).
- **Real-transcript schema (re-verified 2026-07-08, 94 files)**: assistant `message.content` is *always* a list (5274/5274) — the string-content branch never occurs, so the assistant `if not isinstance(content, list): continue` guard is safe; `is_error==True` tool_results *always* have string content (46/46), reconfirming that the block-level `is_error` read (not inner-list scan) is load-bearing.
- **Skill install**: not yet symlinked. To activate: `ln -s /Users/discordwell/Projects/personal-tools/session-coach/skill ~/.claude/skills/session-coach`.
- **Anki venv**: `anki/.venv/` (Python 3.12.12). Activate with `.venv/bin/python` for the smoke test or CLI runs.
