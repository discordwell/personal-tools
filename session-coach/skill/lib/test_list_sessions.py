#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _make_tree(root: str) -> tuple[str, str]:
    proj = os.path.join(root, "-project-a")
    os.makedirs(proj)
    big = os.path.join(proj, "session_big.jsonl")
    small = os.path.join(proj, "session_small.jsonl")
    with open(big, "w") as f:
        f.write("x" * 4096)
    with open(small, "w") as f:
        f.write("x" * 100)
    return big, small


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "list_sessions.py"), *args],
        capture_output=True, text=True,
    )


def test_filters_small_files_and_warns():
    with tempfile.TemporaryDirectory() as td:
        big, small = _make_tree(td)
        proc = _run("--root", td, "--days", "365")
        assert proc.returncode == 0, proc.stderr
        out_paths = proc.stdout.strip().splitlines()
        assert big in out_paths, f"expected big file in output, got {out_paths}"
        assert small not in out_paths, f"small file should be filtered, got {out_paths}"
        assert "skipped 1 file" in proc.stderr, (
            f"expected stderr note about skipped file, got: {proc.stderr!r}"
        )


def test_no_warning_when_nothing_skipped():
    with tempfile.TemporaryDirectory() as td:
        proj = os.path.join(td, "-only-big")
        os.makedirs(proj)
        with open(os.path.join(proj, "s.jsonl"), "w") as f:
            f.write("x" * 4096)
        proc = _run("--root", td, "--days", "365")
        assert proc.returncode == 0, proc.stderr
        assert "skipped" not in proc.stderr, (
            f"unexpected stderr note: {proc.stderr!r}"
        )


def test_missing_root_errors():
    proc = _run("--root", "/no/such/path/probably", "--days", "1")
    assert proc.returncode != 0
    assert "not found" in proc.stderr


def test_rejects_negative_count_and_days():
    # A negative --count would slice jsonls[:-1] and drop the OLDEST file instead
    # of returning nothing; a negative --days yields a future cutoff. Reject both.
    with tempfile.TemporaryDirectory() as td:
        _make_tree(td)
        for flag in ("--count", "--days"):
            proc = _run("--root", td, flag, "-1")
            assert proc.returncode != 0, f"{flag} -1 should be rejected"
            assert "must be >= 0" in proc.stderr, f"got: {proc.stderr!r}"


if __name__ == "__main__":
    test_filters_small_files_and_warns()
    test_no_warning_when_nothing_skipped()
    test_missing_root_errors()
    test_rejects_negative_count_and_days()
    print("OK")
