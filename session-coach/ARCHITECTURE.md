# Session Coach — Architecture

## Purpose
Analyze past Claude Code sessions to surface prompting weaknesses and personal knowledge gaps. Outputs: prompting journal entries, behavior-shaping memory entries, and Anki flashcards.

Inputs are read-only from `~/.claude/projects/<project-slug>/<session-id>.jsonl`.

## Components

### 1. `anki/` — Anki Deck Builder
A standalone Python CLI that converts a JSON list of cards into a `.apkg` file using `genanki`.

**CLI contract**

    python anki/build_deck.py \
      --input cards.json \
      --output deck.apkg \
      --deck-name "Session Coach"

**Card schema** (`cards.json`):

    [
      {
        "front": "What is X?",
        "back": "X is Y.",
        "tags": ["claude-code", "tool-use"]
      }
    ]

**Requirements**
- Stable note IDs: deterministic hash of `front + back` so re-imports update existing cards rather than duplicating.
- Tags map to Anki tags on the note.
- Subdeck routing: if a card has tags, it goes in `<deck_name>::<first_tag>`; untagged cards go in the parent deck.
- Pin `genanki` version in `requirements.txt`.
- Ship a `sample_cards.json` and a smoke test that builds and validates a `.apkg`.

### 2. `skill/` — Extraction Skill
A Claude Code skill (`SKILL.md` + optional helper scripts) invoked manually by the user. When invoked, it instructs Claude to:

1. Enumerate JSONL session files in `~/.claude/projects/`.
2. Filter to last 7 days (default), or last N sessions, configurable via skill args.
3. Read transcripts and identify:
   - **Thrashing** — repeated tool calls with similar inputs, edit/revert/edit cycles, retry loops.
   - **Tool confusion** — failed tool calls, fallback to Bash when a dedicated tool exists, wrong-tool patterns.
   - **User corrections / pain points** — restatements, pushback, frustration.
   - **Knowledge gaps** — concepts the user asked about, suggesting unfamiliarity.
4. Produce three outputs:
   - **Journal entry** — append a dated section to `~/.claude/prompting-journal.md` with 3–5 actionable prompting tips, each citing session evidence.
   - **Memory entries** — write `feedback`-type auto-memories per the format documented in `~/CLAUDE.md`, and update the `MEMORY.md` index.
   - **Anki cards** — write a `cards.json`, then invoke `anki/build_deck.py` to produce `decks/session-coach-YYYY-MM-DD.apkg`.

## Data flow

    ~/.claude/projects/*.jsonl
            │
            ▼
       skill (Claude analyzes)
            │
            ├──► ~/.claude/prompting-journal.md   (append)
            ├──► memory/*.md + MEMORY.md           (write/update)
            └──► cards.json ──► anki/build_deck.py ──► decks/*.apkg

## Installation
- Anki tool: `pip install -r anki/requirements.txt` (or use a venv at `anki/.venv`).
- Skill: symlink `skill/` to `~/.claude/skills/session-coach/`.

## Out of scope (v1)
- AnkiConnect live-add.
- Single-session analysis mode (only batch).
- Cross-session deduplication beyond stable note IDs.
