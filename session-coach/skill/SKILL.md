---
name: session-coach
description: Use when the user asks to "analyze my Claude Code sessions", "review my prompting", "find my prompting weaknesses", "build me Anki cards from my sessions", or runs /session-coach. Reads recent JSONL transcripts under ~/.claude/projects/, identifies thrashing, tool confusion, user pushback, and knowledge gaps across sessions, then writes prompting-journal entries, behavior-shaping memory entries, and an Anki .apkg deck.
argument-hint: "[days=N | count=N]"
---

# Session Coach

Analyze the user's recent Claude Code session transcripts and produce three artifacts: a dated prompting-journal entry, behavior-shaping `feedback`-type memory entries, and an Anki deck targeting the most prominent knowledge gaps.

This skill is invoked manually. Do all reading from local files only — no network.

## Inputs

Parse the user's argument string for `days=N` or `count=N`. Defaults: `days=7`. If both are present, prefer `count=N`. If neither parses, default to `days=7`.

Resolve absolute paths up front. The skill lives at `/Users/discordwell/Projects/personal-tools/session-coach/skill/` (symlinked into `~/.claude/skills/session-coach/`). Use the source path for invoking helpers — both resolve identically. The helpers are:

- `lib/list_sessions.py` — enumerate session JSONL files filtered by mtime, newest-first. Flags: `--days N`, `--count N`.
- `lib/summarize_session.py` — distill a JSONL into a compact NDJSON event list (header + one event per line: `user_msg`, `user_interrupt`, `assistant_text`, `tool_use`, `tool_result`). Flags: `--max-text N` (per-event char cap, default 240), `--max-events N` (total cap, default 2000; events beyond the cap are elided from the middle).

## Procedure

### Step 1 — Enumerate sessions

Run `python3 /Users/discordwell/Projects/personal-tools/session-coach/skill/lib/list_sessions.py --days N` (or `--count N`) and capture the list of paths. If the list is empty, stop and tell the user no sessions matched.

Cap the number of sessions analyzed at 25. If more than 25 are returned, take the first 25 (newest) and note the cap in the journal entry.

### Step 2 — Summarize each session

For each path, run `python3 /Users/discordwell/Projects/personal-tools/session-coach/skill/lib/summarize_session.py <path>` and read the NDJSON output. The first line is a header (`{"kind":"header", "sessionId", "title", "lastPrompt", "eventCount"}`); the rest are events.

Use the header's `title` (and short `sessionId` slug — first 8 chars) as the citation handle: e.g. `[973673b5: "Ask about Claude Code session logging"]`.

Do not dump the raw output back to the user. Use it only to derive the patterns below.

### Step 3 — Detect patterns across all sessions

Scan events for these signals. Build evidence lists keyed by pattern, each entry citing one session slug + a short paraphrase or quote.

- **Thrashing** — same `tool_use.name` + near-identical `input_brief` repeated within ~10 events; Edit-then-Edit cycles on the same `file_path`; Bash retry loops where `tool_result.ok` is false multiple times in a row; the same `file_path` Read more than 3 times in a session.
- **Tool confusion** — `tool_result.ok=false` followed by a different tool succeeding; Bash `cat`/`head`/`tail`/`sed`/`awk`/`echo > file` when Read/Edit/Write would have worked; multiple Glob/Grep calls before finding a file Claude could have Read directly given the user's hint.
- **User corrections / pain points** — `user_msg` containing "no", "stop", "don't", "wrong", "that's not", "I said", "again", "still", or `user_interrupt` events; `user_msg` shorter than ~80 chars that immediately follows an assistant tool sequence (often a curt redirect).
- **Knowledge gaps** — `user_msg` matching "how does X work", "what's the difference", "what is", "why does", "explain", "I don't understand"; topics where the user asked the same kind of question across multiple sessions.

A pattern is reportable only if it appears across ≥2 distinct sessions OR is a single high-severity instance (e.g. a clear pushback with explicit frustration).

### Step 4 — Produce outputs

#### Output A: Prompting journal entry

Append (do not overwrite) a new section to `~/.claude/prompting-journal.md`. Create the file with a top-level `# Prompting Journal` header if it doesn't exist.

Format:

    ## YYYY-MM-DD (UTC) — N sessions analyzed (window: <days=7|count=N>)

    ### Tip 1: <one-line actionable directive>
    **Pattern:** <thrashing | tool-confusion | pushback | knowledge-gap>
    **Evidence:** [<slug1>: "<brief>"], [<slug2>: "<brief>"]
    **Try:** <concrete prompting change the user can make>

    ### Tip 2: ...

Aim for 3–5 tips. Each tip must cite ≥1 session slug. Tips should be directives the user can apply to their next prompt, not Claude-side fixes ("Be more specific about X" not "Claude should do X").

Use UTC date. Get it via `date -u +%Y-%m-%d` if needed.

#### Output B: Memory entries (only when warranted)

Only write memories for patterns that should *change future Claude behavior* and recur across sessions. Do not write a memory for a single-session blip.

Determine the target memory directories — write each memory to **both** of:

1. **Anchor directory** — `/Users/discordwell/.claude/projects/-Users-discordwell-Projects-bad-idea-discord/memory/`. This is the user's primary project; memories land here regardless of cwd so insights aggregate in one place.
2. **Cwd directory** — `~/.claude/projects/<cwd-slug>/memory/`, where `<cwd-slug>` is the absolute cwd with `/` → `-` (e.g. `/Users/discordwell/Projects/foo` → `-Users-discordwell-Projects-foo`).

If both paths resolve to the same directory (skill invoked from bad-idea-discord), write once. Otherwise write the same content to both. Create either directory if it doesn't exist. Mention all destination paths to the user so they know where memories landed.

For each memory, write a file named `feedback_<short_topic>.md` (lowercase, underscores) with this exact format:

    ---
    name: <Short title, ~5–10 words>
    description: <One-sentence summary, what this changes about Claude's behavior>
    type: feedback
    ---

    <Lead paragraph: what to do or not do, in 1–3 sentences.>

    **Why:** <Why this matters — cite the recurring pattern observed, e.g. "Across sessions <slug1>, <slug2> Claude repeatedly Xed when Y was needed.">

    **How to apply:**
    - <Concrete rule 1>
    - <Concrete rule 2>
    - <Concrete rule N>

After writing memory files, update `MEMORY.md` in **each** destination directory you wrote to. If `MEMORY.md` exists, append new bullet rows; do not duplicate entries that already point to the same filename. If absent, create with `# Memory Index` header. Use one of these styles (match whichever is already present in the file):

    - [filename.md](filename.md) — one-line summary

or a markdown table with `| File | Summary |` header rows.

Aim for 0–3 memory entries — quality over quantity. If nothing rises to the bar, write none and say so.

#### Output C: Anki deck

Cover the top knowledge gaps (and optionally a few key tool-usage facts the user clearly hasn't internalized). Aim for **5–15 cards**. Quality over quantity — skip the deck entirely if fewer than 3 strong cards emerge, and tell the user.

1. Write `cards.json` to a tmp path, e.g. `/tmp/session-coach-cards-<YYYYMMDD>.json`. Schema:

       [
         { "front": "...", "back": "...", "tags": ["claude-code"] }
       ]

   - `front` is a question/prompt; `back` is the concise answer (≤4 sentences or a short code snippet). No prose recap of the session.
   - Tag each card with the topic. Use kebab-case lowercase tags. Common tags: `claude-code`, `python`, `git`, `bash`, `tool-use`, `prompting`. The first tag becomes the Anki subdeck under "Session Coach".

2. Build the deck:

       python3 /Users/discordwell/Projects/personal-tools/session-coach/anki/build_deck.py \
         --input /tmp/session-coach-cards-<YYYYMMDD>.json \
         --output /Users/discordwell/Projects/personal-tools/session-coach/decks/session-coach-YYYY-MM-DD.apkg \
         --deck-name "Session Coach"

   The `decks/` directory should already exist (created by the project layout). If it doesn't, `mkdir -p` it first. Use today's UTC date for both the filename and the cards-tmp path.

   If `build_deck.py` exits non-zero, report the stderr to the user and continue — the journal and memory outputs are still valuable.

### Step 5 — Report to user

Print a short summary:

- N sessions analyzed, window used.
- Path to the journal entry and the journal section heading.
- List of memory files written and the destination directories (or "none — no patterns reached the bar").
- Path to the Anki deck (or skipped reason).

Do not dump the full journal entry or card contents into the chat — the user will read the files. A 2-bullet headline of the most important tip is fine.

## Constraints

- All paths are absolute. Never write to `~/.claude/prompting-journal.md` or any memory file outside the procedure above.
- Do not include the user's source code or transcript content verbatim in cards or memory entries beyond short quoted phrases used as evidence.
- Bound output: each tip's evidence ≤2 entries; each card's `back` ≤4 sentences; memory `How to apply` ≤6 bullets.
- Deterministic: same inputs → same patterns surfaced (don't randomize tip order; sort by pattern severity then session count).
- Read-only with respect to source JSONL files.

## Install

Symlink this directory into the Claude skills folder once:

    mkdir -p ~/.claude/skills
    ln -s /Users/discordwell/Projects/personal-tools/session-coach/skill ~/.claude/skills/session-coach
