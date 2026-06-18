#!/usr/bin/env python3
"""Build an Anki .apkg deck from a JSON list of flashcards."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from pathlib import Path

import genanki


# Stable-ID strategy: hash a domain-tagged string and mod into the 31-bit
# range Anki accepts. Domain prefixes prevent collisions between deck/model/note
# spaces that would otherwise share the same input string.
ID_MOD = 2**31


def stable_id(kind: str, key: str) -> int:
    h = hashlib.sha1(f"{kind}:{key}".encode("utf-8")).hexdigest()
    return int(h, 16) % ID_MOD


def note_id(front: str, back: str) -> int:
    # Spec calls out front+back (not tags) so editing tags doesn't reshuffle IDs.
    # Length-frame the front so (front, back) maps injectively to the key string.
    # A plain "{front}\n{back}" join collides whenever a newline can migrate
    # across the boundary, e.g. ("a\nb", "c") and ("a", "b\nc") — two distinct
    # cards that would then share a GUID and clobber each other on import.
    return stable_id("note", f"{len(front)}:{front}{back}")


def deck_id(deck_path: str) -> int:
    return stable_id("deck", deck_path)


def model_id() -> int:
    # Single model shared by all cards; bump the key string to migrate.
    return stable_id("model", "session-coach-basic-v1")


def build_model() -> genanki.Model:
    return genanki.Model(
        model_id=model_id(),
        name="Session Coach Basic",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "{{Front}}",
                "afmt": '{{FrontSide}}<hr id="answer">{{Back}}',
            }
        ],
        css=(
            ".card { font-family: -apple-system, system-ui, sans-serif; "
            "font-size: 18px; color: #222; background: #fff; "
            "text-align: left; padding: 16px; line-height: 1.5; "
            # pre-wrap is load-bearing: it preserves the literal newlines and
            # leading whitespace that render_field leaves in the field, so
            # multi-line backs and indented code snippets show as authored.
            # Without it the browser collapses runs of whitespace and indentation
            # is lost.
            "white-space: pre-wrap; } "
            "hr#answer { margin: 12px 0; border: 0; border-top: 1px solid #ccc; }"
        ),
    )


def load_cards(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"error: input file not found: {path}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"error: invalid JSON in {path}: {e}")
    if not isinstance(data, list):
        raise SystemExit("error: input must be a JSON array of cards")
    if not data:
        raise SystemExit("error: input card list is empty")
    return data


def normalize_card(raw, idx: int) -> dict | None:
    if not isinstance(raw, dict):
        print(f"warning: card #{idx} is not an object, skipping", file=sys.stderr)
        return None
    front = raw.get("front")
    back = raw.get("back")
    if not isinstance(front, str) or not front.strip():
        print(f"warning: card #{idx} missing/empty 'front', skipping", file=sys.stderr)
        return None
    if not isinstance(back, str) or not back.strip():
        print(f"warning: card #{idx} missing/empty 'back', skipping", file=sys.stderr)
        return None
    tags_raw = raw.get("tags") or []
    if not isinstance(tags_raw, list):
        print(f"warning: card #{idx} 'tags' is not a list, ignoring tags", file=sys.stderr)
        tags_raw = []
    # Anki tags can't contain whitespace; replace with underscores.
    tags = [str(t).strip().replace(" ", "_") for t in tags_raw if str(t).strip()]
    return {"front": front, "back": back, "tags": tags}


def render_field(text: str) -> str:
    # Anki fields are HTML. Escape special chars but otherwise keep the text
    # verbatim — newlines and leading whitespace included. The model's CSS sets
    # `white-space: pre-wrap`, so the field renders exactly as authored (multi-
    # line backs and indented code snippets keep their shape). quote=False leaves
    # quotes intact since fields are HTML *content*, not attribute values.
    return html.escape(text, quote=False)


def build(input_path: Path, output_path: Path, deck_name: str) -> int:
    raw_cards = load_cards(input_path)
    model = build_model()

    # Group cards by destination deck path, creating Deck objects lazily so we
    # only emit subdecks that actually contain cards.
    decks: dict[str, genanki.Deck] = {}

    def get_deck(path: str) -> genanki.Deck:
        if path not in decks:
            decks[path] = genanki.Deck(deck_id(path), path)
        return decks[path]

    note_count = 0
    duplicate_count = 0
    seen_guids: set[str] = set()
    for idx, raw in enumerate(raw_cards):
        card = normalize_card(raw, idx)
        if card is None:
            continue
        guid = str(note_id(card["front"], card["back"]))
        # Cards with identical front+back share a GUID, so Anki would collapse
        # them to one note on import regardless of which deck they land in.
        # Skip the duplicate here so the reported count matches what imports.
        if guid in seen_guids:
            print(
                f"warning: card #{idx} duplicates an earlier card (same front/back), skipping",
                file=sys.stderr,
            )
            duplicate_count += 1
            continue
        seen_guids.add(guid)
        if card["tags"]:
            target = f"{deck_name}::{card['tags'][0]}"
        else:
            target = deck_name
        note = genanki.Note(
            model=model,
            fields=[render_field(card["front"]), render_field(card["back"])],
            tags=card["tags"],
            guid=guid,
        )
        get_deck(target).add_note(note)
        note_count += 1

    if note_count == 0:
        raise SystemExit("error: no valid cards to write")

    # Ensure top-level deck exists even if every card landed in a subdeck —
    # Anki shows an empty parent otherwise, which is fine, but this keeps the
    # apkg self-consistent.
    get_deck(deck_name)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    genanki.Package(list(decks.values())).write_to_file(str(output_path))
    summary = f"wrote {note_count} note(s) across {len(decks)} deck(s) to {output_path}"
    if duplicate_count:
        summary += f" ({duplicate_count} duplicate(s) skipped)"
    print(summary)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Build an Anki .apkg from cards.json")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--deck-name", required=True)
    args = p.parse_args()
    if args.output.exists():
        print(f"note: overwriting existing {args.output}", file=sys.stderr)
    return build(args.input, args.output, args.deck_name)


if __name__ == "__main__":
    sys.exit(main())
