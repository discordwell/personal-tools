#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile
import time

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


def _mk(root: str, rel: str, *, size: int = 4096, mtime: float | None = None) -> str:
    """Create a session file at <root>/<rel> with `size` bytes, optionally
    stamping its mtime so the time/count tests are independent of write order."""
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x" * size)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


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


def test_sorted_newest_first():
    # Distinct mtimes must come out strictly newest-first, regardless of the
    # order the files were created in.
    with tempfile.TemporaryDirectory() as td:
        old = _mk(td, "-p/old.jsonl", mtime=1000)
        new = _mk(td, "-p/new.jsonl", mtime=3000)
        mid = _mk(td, "-p/mid.jsonl", mtime=2000)
        proc = _run("--root", td, "--days", "100000")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [new, mid, old]


def test_count_returns_newest_n():
    # --count keeps only the N most-recently-modified files, newest-first, and
    # drops the rest (here the oldest).
    with tempfile.TemporaryDirectory() as td:
        old = _mk(td, "-p/old.jsonl", mtime=1000)
        new = _mk(td, "-p/new.jsonl", mtime=3000)
        mid = _mk(td, "-p/mid.jsonl", mtime=2000)
        proc = _run("--root", td, "--count", "2")
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.splitlines()
        assert out == [new, mid], out
        assert old not in out


def test_count_zero_returns_nothing():
    # --count 0 is valid (nonneg) and selects no files, rather than slicing oddly.
    with tempfile.TemporaryDirectory() as td:
        _mk(td, "-p/a.jsonl", mtime=1000)
        proc = _run("--root", td, "--count", "0")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == ""


def test_days_excludes_old_files():
    # The --days cutoff is mtime-based: a file modified within the window is kept,
    # one modified before it is dropped.
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        recent = _mk(td, "-p/recent.jsonl", mtime=now - 3600)        # 1h ago
        stale = _mk(td, "-p/stale.jsonl", mtime=now - 10 * 86400)    # 10d ago
        proc = _run("--root", td, "--days", "7")
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.splitlines()
        assert recent in out, out
        assert stale not in out, out


def test_default_window_is_seven_days():
    # With no flag the tool defaults to a 7-day window (not "everything").
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        recent = _mk(td, "-p/recent.jsonl", mtime=now - 2 * 86400)   # 2d ago
        stale = _mk(td, "-p/stale.jsonl", mtime=now - 30 * 86400)    # 30d ago
        proc = _run("--root", td)
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.splitlines()
        assert out == [recent], out


def test_mtime_ties_break_on_path_deterministically():
    # Regression: sorting on mtime alone left ties to iterdir() order, which is
    # filesystem-dependent — so --count near a tie could include different
    # sessions run to run. Equal mtimes must order by path (ascending) so the
    # result is reproducible. Create them in reverse path order to ensure the
    # tie-break, not creation order, decides.
    with tempfile.TemporaryDirectory() as td:
        z = _mk(td, "-p/zzz.jsonl", mtime=2000)
        a = _mk(td, "-p/aaa.jsonl", mtime=2000)
        m = _mk(td, "-p/mmm.jsonl", mtime=2000)
        proc = _run("--root", td, "--days", "100000")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [a, m, z]


def test_subagent_subdir_files_are_skipped():
    # Only top-level <project>/*.jsonl are session transcripts; subagent files
    # live one level deeper and must not be enumerated (glob is non-recursive).
    with tempfile.TemporaryDirectory() as td:
        top = _mk(td, "-p/session.jsonl", mtime=2000)
        sub = _mk(td, "-p/subagent-dir/agent.jsonl", mtime=2000)
        proc = _run("--root", td, "--days", "100000")
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout.splitlines()
        assert out == [top], out
        assert sub not in out


def test_count_and_days_are_mutually_exclusive():
    # The skill picks one window mode and passes a single flag; argparse must
    # reject both at once so the contract can't be violated silently.
    with tempfile.TemporaryDirectory() as td:
        _mk(td, "-p/a.jsonl", mtime=2000)
        proc = _run("--root", td, "--count", "2", "--days", "7")
        assert proc.returncode != 0, "passing both --count and --days must error"
        assert "not allowed with" in proc.stderr, f"got: {proc.stderr!r}"


if __name__ == "__main__":
    test_filters_small_files_and_warns()
    test_no_warning_when_nothing_skipped()
    test_missing_root_errors()
    test_rejects_negative_count_and_days()
    test_sorted_newest_first()
    test_count_returns_newest_n()
    test_count_zero_returns_nothing()
    test_days_excludes_old_files()
    test_default_window_is_seven_days()
    test_mtime_ties_break_on_path_deterministically()
    test_subagent_subdir_files_are_skipped()
    test_count_and_days_are_mutually_exclusive()
    print("OK")
