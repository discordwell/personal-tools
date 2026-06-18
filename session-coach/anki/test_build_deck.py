"""Smoke test: run the CLI on sample_cards.json and validate the .apkg output."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


HERE = Path(__file__).parent
SAMPLE = HERE / "sample_cards.json"
BUILD = HERE / "build_deck.py"
DECK_NAME = "Session Coach Test"


def count_expected_notes(sample: Path) -> int:
    cards = json.loads(sample.read_text(encoding="utf-8"))
    return sum(
        1
        for c in cards
        if isinstance(c, dict)
        and isinstance(c.get("front"), str) and c["front"].strip()
        and isinstance(c.get("back"), str) and c["back"].strip()
    )


def run_cli(output: Path, input_path: Path = SAMPLE) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(BUILD),
         "--input", str(input_path),
         "--output", str(output),
         "--deck-name", DECK_NAME],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"CLI exited {result.returncode}: {result.stderr}"
    return result


def assert_valid_apkg(apkg: Path, expected_notes: int) -> None:
    assert apkg.exists(), "output .apkg was not created"
    assert apkg.stat().st_size > 0, "output .apkg is empty"
    assert zipfile.is_zipfile(apkg), "output .apkg is not a valid zip"

    with zipfile.ZipFile(apkg) as zf:
        names = zf.namelist()
        # genanki writes either collection.anki2 or collection.anki21 depending
        # on the package format; accept either.
        db_name = next(
            (n for n in names if n in ("collection.anki2", "collection.anki21")),
            None,
        )
        assert db_name is not None, f"no anki collection db found in apkg, got: {names}"
        assert "media" in names, f"no media manifest in apkg, got: {names}"

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / db_name
            db_path.write_bytes(zf.read(db_name))
            con = sqlite3.connect(str(db_path))
            try:
                (count,) = con.execute("SELECT COUNT(*) FROM notes").fetchone()
            finally:
                con.close()
            assert count == expected_notes, (
                f"expected {expected_notes} notes, found {count}"
            )


def test_build_smoke() -> None:
    expected = count_expected_notes(SAMPLE)
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "deck.apkg"
        first = run_cli(out)
        assert "overwriting" not in first.stderr, "first build should not warn"
        assert_valid_apkg(out, expected)


def test_overwrite_warning() -> None:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "deck.apkg"
        run_cli(out)
        second = run_cli(out)
        assert "overwriting" in second.stderr, (
            f"expected overwrite warning in stderr, got: {second.stderr!r}"
        )


def test_note_id_is_injective_across_boundary() -> None:
    # Two distinct cards whose front/back differ only by where a newline sits
    # must NOT share a GUID (else one clobbers the other on import).
    sys.path.insert(0, str(HERE))
    from build_deck import note_id

    assert note_id("a\nb", "c") != note_id("a", "b\nc")
    # Identical content still maps to one stable GUID (re-import updates, not dupes).
    assert note_id("same", "card") == note_id("same", "card")


def test_render_field_escapes_and_preserves_whitespace() -> None:
    # render_field is the field-HTML contract: escape HTML special chars, but
    # keep newlines and indentation verbatim (the model CSS uses white-space:
    # pre-wrap to render them), so code-snippet backs survive intact.
    sys.path.insert(0, str(HERE))
    from build_deck import render_field

    # HTML metacharacters are entity-encoded so they don't break the card markup.
    assert render_field("<em>x</em> & y") == "&lt;em&gt;x&lt;/em&gt; &amp; y"
    # Newlines are NOT collapsed to spaces or <br> — kept literal for pre-wrap.
    assert render_field("line one\nline two") == "line one\nline two"
    # Leading indentation (e.g. a code block) is preserved character-for-character.
    assert render_field("def f():\n    return 1") == "def f():\n    return 1"
    # Quotes stay intact: fields are HTML content, not attribute values.
    assert render_field('say "hi"') == 'say "hi"'


def test_duplicate_cards_are_deduped() -> None:
    # Same front+back in two cards collapse to one note on import; the builder
    # should drop the duplicate, warn, and report the true count.
    cards = [
        {"front": "Q", "back": "A", "tags": ["x"]},
        {"front": "Q", "back": "A", "tags": ["y"]},  # duplicate of the first
        {"front": "Unique", "back": "Z", "tags": []},
    ]
    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "cards.json"
        inp.write_text(json.dumps(cards), encoding="utf-8")
        out = Path(td) / "deck.apkg"
        result = run_cli(out, input_path=inp)
        assert "duplicates an earlier card" in result.stderr, result.stderr
        assert "wrote 2 note(s)" in result.stdout, result.stdout
        assert "1 duplicate(s) skipped" in result.stdout, result.stdout
        assert_valid_apkg(out, expected_notes=2)


if __name__ == "__main__":
    test_build_smoke()
    test_overwrite_warning()
    test_note_id_is_injective_across_boundary()
    test_render_field_escapes_and_preserves_whitespace()
    test_duplicate_cards_are_deduped()
    print("OK")
