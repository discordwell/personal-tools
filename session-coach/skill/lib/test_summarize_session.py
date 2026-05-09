#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from summarize_session import _is_interrupt_text


def test_is_interrupt_text():
    assert _is_interrupt_text("[Request interrupted by user]")
    assert _is_interrupt_text("[Request interrupted by user] additional text")
    assert _is_interrupt_text("[Request interrupted by user for tool use]")
    assert _is_interrupt_text("  [Request interrupted by user]")
    assert not _is_interrupt_text("Hello, world")
    assert not _is_interrupt_text("<local-command-caveat>Some caveat...")
    assert not _is_interrupt_text("")
    assert not _is_interrupt_text("[Some other bracket text]")


def _run(path: str, *extra_args: str) -> list[dict]:
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "summarize_session.py"), path, *extra_args],
        capture_output=True, text=True, check=True,
    )
    return [json.loads(l) for l in proc.stdout.strip().splitlines()]


def _write_jsonl(records: list[dict], extra_lines: list[str] | None = None) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for rec in records:
        f.write(json.dumps(rec) + "\n")
    for line in extra_lines or []:
        f.write(line + "\n")
    f.close()
    return f.name


def test_classification_on_fixture():
    path = _write_jsonl([
        {"type": "user", "timestamp": "t1", "sessionId": "s", "message": {"content": "regular question"}},
        {"type": "user", "timestamp": "t2", "sessionId": "s", "message": {"content": "[Request interrupted by user]"}},
        {"type": "user", "timestamp": "t3", "sessionId": "s", "isMeta": True,
         "message": {"content": "<local-command-caveat>caveat injection text</local-command-caveat>"}},
    ])
    try:
        lines = _run(path)
        assert lines[0]["kind"] == "header"
        kinds = [e["kind"] for e in lines[1:]]
        assert kinds == ["user_msg", "user_interrupt", "user_msg"], kinds
    finally:
        os.unlink(path)


def test_malformed_count_in_header():
    # Two valid user messages, two malformed lines (junk + non-JSON), and a blank line
    # that should not be counted as malformed.
    path = _write_jsonl(
        [
            {"type": "user", "timestamp": "t1", "sessionId": "s", "message": {"content": "valid one"}},
            {"type": "user", "timestamp": "t2", "sessionId": "s", "message": {"content": "valid two"}},
        ],
        extra_lines=["this is not JSON", "{not: closed", ""],
    )
    try:
        lines = _run(path)
        header = lines[0]
        assert header["kind"] == "header"
        assert header["malformed"] == 2, f"expected malformed=2, got {header['malformed']}"
        assert header["eventCount"] == 2, f"expected eventCount=2, got {header['eventCount']}"
    finally:
        os.unlink(path)


def test_elision_bounds_output():
    # Generate enough events to trigger elision. user content -> user_msg events.
    n = 50
    cap = 10
    records = [
        {"type": "user", "timestamp": f"t{i}", "sessionId": "s", "message": {"content": f"msg {i}"}}
        for i in range(n)
    ]
    path = _write_jsonl(records)
    try:
        lines = _run(path, "--max-events", str(cap))
        events = lines[1:]  # skip header
        assert len(events) == cap, f"expected {cap} event lines, got {len(events)}"
        elided = [e for e in events if e["kind"] == "elided"]
        assert len(elided) == 1, "expected exactly one elided marker"
        # Skipped + kept events (non-elided) should equal n
        kept = sum(1 for e in events if e["kind"] != "elided")
        assert elided[0]["skipped"] + kept == n, (
            f"skipped({elided[0]['skipped']}) + kept({kept}) != n({n})"
        )
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_is_interrupt_text()
    test_classification_on_fixture()
    test_malformed_count_in_header()
    test_elision_bounds_output()
    print("OK")
