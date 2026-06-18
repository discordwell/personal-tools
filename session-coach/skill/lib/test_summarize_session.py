#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from summarize_session import (
    _input_brief,
    _is_interrupt_text,
    _result_brief,
    _truncate,
    _user_text_kind,
)


def test_is_interrupt_text():
    assert _is_interrupt_text("[Request interrupted by user]")
    assert _is_interrupt_text("[Request interrupted by user] additional text")
    assert _is_interrupt_text("[Request interrupted by user for tool use]")
    assert _is_interrupt_text("  [Request interrupted by user]")
    assert not _is_interrupt_text("Hello, world")
    assert not _is_interrupt_text("<local-command-caveat>Some caveat...")
    assert not _is_interrupt_text("")
    assert not _is_interrupt_text("[Some other bracket text]")


def test_user_text_kind():
    assert _user_text_kind("a normal question", is_meta=False) == "user_msg"
    assert _user_text_kind("[Request interrupted by user]", is_meta=False) == "user_interrupt"
    # isMeta wins regardless of text: harness-injected records are never the user
    # typing, so they must not be scored as user_msg or user_interrupt.
    assert _user_text_kind("a normal question", is_meta=True) == "meta"
    assert _user_text_kind("[Request interrupted by user]", is_meta=True) == "meta"


def test_truncate_handles_small_caps():
    # Normal case: cap leaves room for the ellipsis (result length == n).
    assert _truncate("hello world", 5) == "hell…"
    assert len(_truncate("hello world", 5)) == 5
    # Short string under the cap is returned verbatim.
    assert _truncate("hi", 240) == "hi"
    # Newlines collapse to spaces.
    assert _truncate("a\nb", 240) == "a b"
    # Degenerate caps must not leak the string via negative-index slicing.
    assert _truncate("hello", 0) == ""
    assert _truncate("hello", -3) == ""
    assert _truncate("hello", 1) == "…"


def test_result_brief_honors_block_level_is_error():
    # The real Claude Code schema puts is_error on the tool_result block itself,
    # alongside *string* content (failed Bash, file-not-found, ...). The parser
    # must report ok=False for these, or the skill's tool-confusion scan — which
    # keys off tool_result.ok — never fires.
    ok, brief = _result_brief("bash: command not found", 240, True)
    assert ok is False, "block-level is_error on string content must yield ok=False"
    assert brief == "bash: command not found"

    # No error flag -> ok=True.
    ok, brief = _result_brief("all good", 240, False)
    assert ok is True
    # Missing flag defaults to ok=True (the common success case).
    ok, _ = _result_brief("all good", 240)
    assert ok is True


def test_result_brief_list_content_and_inner_markers():
    # List content with a block-level error flag, text + image parts joined.
    ok, brief = _result_brief(
        [{"type": "text", "text": "line one"}, {"type": "image"}], 240, True
    )
    assert ok is False
    assert brief == "line one | [image]"

    # Inner-list marker (no block-level flag) is still caught as a fallback.
    ok, _ = _result_brief([{"type": "text", "text": "boom", "is_error": True}], 240)
    assert ok is False
    ok, _ = _result_brief([{"type": "tool_use_error", "text": "x"}], 240)
    assert ok is False

    # Clean list content -> ok=True.
    ok, brief = _result_brief([{"type": "text", "text": "fine"}], 240)
    assert ok is True
    assert brief == "fine"


def test_input_brief_renders_known_tools():
    assert _input_brief("Bash", {"command": "ls -la"}, 240) == "ls -la"
    assert _input_brief("Read", {"file_path": "/tmp/a.py"}, 240) == "/tmp/a.py"
    assert "/tmp/a.py" in _input_brief("Edit", {"file_path": "/tmp/a.py", "old_string": "x", "new_string": "y"}, 240)
    assert "5 chars" in _input_brief("Write", {"file_path": "/tmp/a.py", "content": "hello"}, 240)
    # Grep/Glob keep only the search-relevant keys.
    g = _input_brief("Grep", {"pattern": "TODO", "path": "src", "output_mode": "content"}, 240)
    assert "TODO" in g and "src" in g and "output_mode" not in g
    # Unknown tool falls back to a compact JSON dump.
    assert _input_brief("Task", {"a": 1}, 240) == '{"a":1}'
    # Non-dict input is stringified, not crashed.
    assert _input_brief("Whatever", "raw", 240) == "raw"


def test_input_brief_edit_survives_null_fields():
    # A malformed Edit record with null old/new_string must not crash (None[:60]).
    out = _input_brief("Edit", {"file_path": "/tmp/a.py", "old_string": None, "new_string": None}, 240)
    assert "/tmp/a.py" in out


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
        # The third record is isMeta -> classified as "meta", not "user_msg",
        # so the skill's pushback/knowledge-gap scans skip harness injections.
        assert kinds == ["user_msg", "user_interrupt", "meta"], kinds
    finally:
        os.unlink(path)


def test_tool_result_error_detected_end_to_end():
    # Regression for the headline bug: a tool_result with block-level is_error
    # and STRING content (the dominant real-world shape) must surface ok=False so
    # the skill can see failed tool calls. A clean result stays ok=True.
    path = _write_jsonl([
        {"type": "assistant", "timestamp": "t1", "sessionId": "s",
         "message": {"content": [
             {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "nope"}},
         ]}},
        {"type": "user", "timestamp": "t2", "sessionId": "s",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "u1",
              "content": "bash: nope: command not found", "is_error": True},
         ]}},
        {"type": "assistant", "timestamp": "t3", "sessionId": "s",
         "message": {"content": [
             {"type": "tool_use", "id": "u2", "name": "Bash", "input": {"command": "echo hi"}},
         ]}},
        {"type": "user", "timestamp": "t4", "sessionId": "s",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "u2", "content": "hi"},
         ]}},
    ])
    try:
        events = _run(path)[1:]  # skip header
        results = {e["id"]: e["ok"] for e in events if e["kind"] == "tool_result"}
        assert results == {"u1": False, "u2": True}, results
    finally:
        os.unlink(path)


def test_rejects_nonpositive_caps():
    # End-to-end: argparse must reject caps < 1 (exit 2), not silently misbehave.
    path = _write_jsonl([
        {"type": "user", "timestamp": "t1", "sessionId": "s", "message": {"content": "hi"}},
    ])
    try:
        for bad in ("--max-text", "--max-events"):
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, "summarize_session.py"), path, bad, "0"],
                capture_output=True, text=True,
            )
            assert proc.returncode != 0, f"{bad} 0 should be rejected"
            assert "must be >= 1" in proc.stderr, f"got: {proc.stderr!r}"
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


def test_elision_small_caps_stay_bounded():
    # Regression: --max-events 1 made tail==0, and out[-0:] is the WHOLE list,
    # so every event leaked past the cap. Verify the smallest caps stay bounded.
    n = 12
    records = [
        {"type": "user", "timestamp": f"t{i}", "sessionId": "s", "message": {"content": f"msg {i}"}}
        for i in range(n)
    ]
    path = _write_jsonl(records)
    try:
        for cap in (1, 2, 3):
            events = _run(path, "--max-events", str(cap))[1:]  # skip header
            assert len(events) == cap, f"cap={cap}: expected {cap} lines, got {len(events)}"
            elided = [e for e in events if e["kind"] == "elided"]
            assert len(elided) == 1, f"cap={cap}: expected exactly one elided marker"
            kept = sum(1 for e in events if e["kind"] != "elided")
            assert elided[0]["skipped"] + kept == n, (
                f"cap={cap}: skipped({elided[0]['skipped']}) + kept({kept}) != n({n})"
            )
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_is_interrupt_text()
    test_user_text_kind()
    test_truncate_handles_small_caps()
    test_result_brief_honors_block_level_is_error()
    test_result_brief_list_content_and_inner_markers()
    test_input_brief_renders_known_tools()
    test_input_brief_edit_survives_null_fields()
    test_classification_on_fixture()
    test_tool_result_error_detected_end_to_end()
    test_rejects_nonpositive_caps()
    test_malformed_count_in_header()
    test_elision_bounds_output()
    test_elision_small_caps_stay_bounded()
    print("OK")
