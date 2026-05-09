# Session Coach

Meta-tool that analyzes past Claude Code sessions and turns recurring weaknesses into actionable prompting tips, behavior-shaping memory entries, and Anki flashcards.

See `ARCHITECTURE.md` for the design and component contract.

## Layout
- `anki/` — Standalone `.apkg` builder (Python + `genanki`).
- `skill/` — Claude Code skill that reads JSONL transcripts and emits outputs.
- `decks/` — Generated `.apkg` files (gitignored once a repo is initialized).

## Build / dev
- Anki tool: `pip install -r anki/requirements.txt`
- Smoke test: `python anki/build_deck.py --input anki/sample_cards.json --output /tmp/test.apkg --deck-name "Smoke Test"`

## Install skill
    ln -s "$(pwd)/skill" ~/.claude/skills/session-coach
