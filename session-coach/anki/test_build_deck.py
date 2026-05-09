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


def run_cli(output: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(BUILD),
         "--input", str(SAMPLE),
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


if __name__ == "__main__":
    test_build_smoke()
    test_overwrite_warning()
    print("OK")
