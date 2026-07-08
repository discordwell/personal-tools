#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from summarize_session import (
    SessionStats,
    _input_brief,
    _is_interrupt_text,
    _result_brief,
    _truncate,
    _user_text_kind,
    parse_session,
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


def test_session_stats_counts_are_exact():
    # Drive the accumulator directly: it must count the mechanical signals the
    # skill keys off (errors-by-tool, re-reads, edit churn) exactly.
    s = SessionStats()
    s.observe_user_text("user_msg")
    s.observe_user_text("user_interrupt")
    s.observe_user_text("meta")  # harness injection: must NOT be counted
    s.observe_assistant_text()

    # Read file A four times (-> filesReadGt3) and B once (below threshold).
    for i in range(4):
        s.observe_tool_use("Read", {"file_path": "/A"}, f"r{i}")
    s.observe_tool_use("Read", {"file_path": "/B"}, "rb")

    # Edit A, A, A (two consecutive repeats -> run 2), then B (resets).
    s.observe_tool_use("Edit", {"file_path": "/A"}, "e1")
    s.observe_tool_use("Edit", {"file_path": "/A"}, "e2")
    s.observe_tool_use("Edit", {"file_path": "/A"}, "e3")
    s.observe_tool_use("Edit", {"file_path": "/B"}, "e4")

    # One failing Bash (error attributed by id) and one clean Bash.
    s.observe_tool_use("Bash", {"command": "boom"}, "b1")
    s.observe_tool_result("b1", ok=False)
    s.observe_tool_use("Bash", {"command": "ok"}, "b2")
    s.observe_tool_result("b2", ok=True)

    d = s.to_dict()
    assert d["userMsgs"] == 1
    assert d["interrupts"] == 1
    assert d["assistantTexts"] == 1
    assert d["toolCalls"] == 5 + 4 + 2  # Read*5 + Edit*4 + Bash*2
    assert d["toolErrors"] == 1
    assert d["maxErrorRun"] == 1               # one isolated failure, no streak
    assert d["toolUseByName"] == {"Read": 5, "Edit": 4, "Bash": 2}
    assert d["toolErrorsByName"] == {"Bash": 1}
    assert d["filesReadGt3"] == {"/A": 4}      # B (1 read) is below the >3 bar
    assert d["editRunsByFile"] == {"/A": 2}    # B never repeated consecutively


def test_session_stats_ranking_is_deterministic():
    # Equal counts must tie-break on key name so output is reproducible.
    s = SessionStats()
    for name in ("Zebra", "Apple", "Mango"):
        s.observe_tool_use(name, {}, None)
    assert list(s.to_dict()["toolUseByName"]) == ["Apple", "Mango", "Zebra"]


def test_session_stats_max_error_run_is_longest_failure_streak():
    # maxErrorRun = the longest run of consecutive failing tool_results (a retry
    # loop). A *successful* result resets the streak; an intervening tool_use
    # (the "let me try again" turn) does not. The use->fail pairs below are
    # interleaved on purpose, so this fails if observe_tool_use reset the streak.
    s = SessionStats()
    for i in range(3):
        s.observe_tool_use("Bash", {"command": f"c{i}"}, f"t{i}")
        s.observe_tool_result(f"t{i}", ok=False)  # streak grows to 3 across turns
    # A success ends the streak; a later lone failure starts a shorter one.
    s.observe_tool_use("Read", {"file_path": "/f"}, "ok")
    s.observe_tool_result("ok", ok=True)
    s.observe_tool_use("Bash", {"command": "last"}, "t4")
    s.observe_tool_result("t4", ok=False)  # streak 1 again (doesn't beat 3)
    d = s.to_dict()
    assert d["maxErrorRun"] == 3
    assert d["toolErrors"] == 4  # total failures, regardless of streaks


def test_session_stats_max_error_run_zero_without_failures():
    # No failing result -> no retry loop, even with many successful calls.
    s = SessionStats()
    s.observe_tool_use("Read", {"file_path": "/f"}, "r1")
    s.observe_tool_result("r1", ok=True)
    assert s.to_dict()["maxErrorRun"] == 0


def test_parse_session_returns_header_events_and_full_stats():
    # parse_session is the reusable core the aggregator builds on: it must return
    # a well-formed header, the FULL (un-elided) event list, and a live
    # SessionStats accumulated over the whole session.
    path = _write_jsonl([
        {"type": "ai-title", "aiTitle": "My Session"},
        {"type": "user", "timestamp": "t1", "sessionId": "sid12345", "message": {"content": "hello"}},
        {"type": "assistant", "timestamp": "t2", "sessionId": "sid12345",
         "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "x"}}]}},
        {"type": "user", "timestamp": "t3", "sessionId": "sid12345",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "b1", "content": "boom", "is_error": True}]}},
    ])
    try:
        header, events, stats = parse_session(path)
        assert header["kind"] == "header"
        assert header["sessionId"] == "sid12345"
        assert header["title"] == "My Session"
        assert header["eventCount"] == len(events) == 3  # user_msg, tool_use, tool_result
        sd = stats.to_dict()
        assert sd["userMsgs"] == 1
        assert sd["toolCalls"] == 1
        assert sd["toolErrors"] == 1
        assert sd["toolErrorsByName"] == {"Bash": 1}
    finally:
        os.unlink(path)


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


def test_stats_footer_end_to_end():
    # The --stats line is emitted LAST, reflects the full session, and attributes
    # the error to the failing tool by pairing tool_use id -> tool_result id.
    path = _write_jsonl([
        {"type": "user", "timestamp": "t1", "sessionId": "s", "message": {"content": "a question"}},
        {"type": "assistant", "timestamp": "t2", "sessionId": "s",
         "message": {"content": [
             {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "nope"}},
         ]}},
        {"type": "user", "timestamp": "t3", "sessionId": "s",
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "b1", "content": "not found", "is_error": True},
         ]}},
        {"type": "assistant", "timestamp": "t4", "sessionId": "s",
         "message": {"content": [
             {"type": "tool_use", "id": "e1", "name": "Edit",
              "input": {"file_path": "/f", "old_string": "x", "new_string": "y"}},
             {"type": "tool_use", "id": "e2", "name": "Edit",
              "input": {"file_path": "/f", "old_string": "y", "new_string": "z"}},
         ]}},
    ])
    try:
        lines = _run(path, "--stats")
        assert lines[0]["kind"] == "header"
        assert lines[-1]["kind"] == "stats", f"stats must be the final line, got {lines[-1]['kind']}"
        stats = lines[-1]
        assert stats["userMsgs"] == 1
        assert stats["toolCalls"] == 3  # 1 Bash + 2 Edits
        assert stats["toolErrors"] == 1
        assert stats["maxErrorRun"] == 1  # one isolated failure, no streak
        assert stats["toolErrorsByName"] == {"Bash": 1}
        assert stats["editRunsByFile"] == {"/f": 1}  # two consecutive edits to /f
        # Exactly one stats line, and no event before it carries kind=stats.
        assert sum(1 for e in lines if e["kind"] == "stats") == 1
    finally:
        os.unlink(path)


def test_no_stats_line_without_flag():
    # Backward-compat: default output is unchanged — no stats footer at all.
    path = _write_jsonl([
        {"type": "user", "timestamp": "t1", "sessionId": "s", "message": {"content": "hi"}},
    ])
    try:
        lines = _run(path)  # no --stats
        assert all(e["kind"] != "stats" for e in lines), "default output must not include a stats line"
    finally:
        os.unlink(path)


def test_stats_counts_full_session_despite_elision():
    # Stats are accumulated during the parse (before elision), so they reflect
    # the whole session even when most events are dropped from the output.
    n = 40
    records = [
        {"type": "user", "timestamp": f"t{i}", "sessionId": "s", "message": {"content": f"msg {i}"}}
        for i in range(n)
    ]
    path = _write_jsonl(records)
    try:
        lines = _run(path, "--max-events", "5", "--stats")
        events = lines[1:-1]  # drop header and trailing stats
        assert any(e["kind"] == "elided" for e in events), "expected elision at this cap"
        stats = lines[-1]
        assert stats["kind"] == "stats"
        # All 40 user messages counted, even though only a few events were emitted.
        assert stats["userMsgs"] == n, f"expected {n} userMsgs, got {stats['userMsgs']}"
    finally:
        os.unlink(path)


def test_max_error_run_end_to_end():
    # Three Bash calls fail back-to-back (each tool_use in its own assistant
    # record, each result in a user record). The intervening tool_use turns are
    # part of the retry loop, so the footer must report a streak of 3.
    records = []
    for i in range(3):
        records.append({
            "type": "assistant", "timestamp": f"a{i}", "sessionId": "s",
            "message": {"content": [
                {"type": "tool_use", "id": f"b{i}", "name": "Bash", "input": {"command": f"try {i}"}},
            ]},
        })
        records.append({
            "type": "user", "timestamp": f"u{i}", "sessionId": "s",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": f"b{i}", "content": "still failing", "is_error": True},
            ]},
        })
    path = _write_jsonl(records)
    try:
        stats = _run(path, "--stats")[-1]
        assert stats["kind"] == "stats"
        assert stats["maxErrorRun"] == 3, f"expected streak of 3, got {stats['maxErrorRun']}"
        assert stats["toolErrorsByName"] == {"Bash": 3}
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_is_interrupt_text()
    test_parse_session_returns_header_events_and_full_stats()
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
    test_session_stats_counts_are_exact()
    test_session_stats_ranking_is_deterministic()
    test_session_stats_max_error_run_is_longest_failure_streak()
    test_session_stats_max_error_run_zero_without_failures()
    test_stats_footer_end_to_end()
    test_no_stats_line_without_flag()
    test_stats_counts_full_session_despite_elision()
    test_max_error_run_end_to_end()
    print("OK")
