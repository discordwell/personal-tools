#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from aggregate_sessions import CrossSessionStats


def _sd(**overrides) -> dict:
    """A full per-session stats dict (SessionStats.to_dict shape) with zero
    defaults, so each test states only the fields it exercises."""
    base = {
        "kind": "stats",
        "userMsgs": 0,
        "interrupts": 0,
        "assistantTexts": 0,
        "toolCalls": 0,
        "toolErrors": 0,
        "maxErrorRun": 0,
        "toolUseByName": {},
        "toolErrorsByName": {},
        "filesReadGt3": {},
        "editRunsByFile": {},
    }
    base.update(overrides)
    return base


def _hdr(sid="s", title=None):
    return {"kind": "header", "sessionId": sid, "title": title}


def test_rollup_sums_totals_and_tool_counters():
    agg = CrossSessionStats()
    agg.observe(_hdr("aaaaaaaa"), _sd(userMsgs=2, interrupts=1, toolCalls=3, toolErrors=1,
                                      toolUseByName={"Bash": 2, "Read": 1}, toolErrorsByName={"Bash": 1}))
    agg.observe(_hdr("bbbbbbbb"), _sd(userMsgs=1, assistantTexts=4, toolCalls=2,
                                      toolUseByName={"Read": 1, "Edit": 1}))
    d = agg.to_dict()
    assert d["sessions"] == 2
    assert d["totals"] == {
        "userMsgs": 3, "interrupts": 1, "assistantTexts": 4, "toolCalls": 5, "toolErrors": 1,
    }
    # Counters sum across sessions; ties (Bash=2, Read=2) break on key asc.
    assert d["toolUseByName"] == {"Bash": 2, "Read": 2, "Edit": 1}
    assert d["toolErrorsByName"] == {"Bash": 1}


def test_rollup_counts_thrash_sessions_by_threshold():
    agg = CrossSessionStats()
    agg.observe(_hdr("s1"), _sd(maxErrorRun=1))   # neither: an isolated failure
    agg.observe(_hdr("s2"), _sd(maxErrorRun=2))   # back-to-back only
    agg.observe(_hdr("s3"), _sd(maxErrorRun=3))   # back-to-back AND retry loop
    agg.observe(_hdr("s4"), _sd(maxErrorRun=5))   # both
    d = agg.to_dict()
    assert d["sessionsWithBackToBackFailure"] == 3   # maxErrorRun >= 2: s2, s3, s4
    assert d["sessionsWithRetryLoop"] == 2           # maxErrorRun >= 3: s3, s4


def test_rollup_aggregates_reread_and_edit_files():
    agg = CrossSessionStats()
    # /a re-read >3x in both sessions (recurring); /b in one only.
    agg.observe(_hdr("s1"), _sd(filesReadGt3={"/a": 4, "/b": 5}))
    agg.observe(_hdr("s2"), _sd(filesReadGt3={"/a": 7}))
    # /y churned in both (runs 2 and 3 -> total 5); /z once (run 1).
    agg.observe(_hdr("s1"), _sd(editRunsByFile={"/y": 2, "/z": 1}))
    agg.observe(_hdr("s2"), _sd(editRunsByFile={"/y": 3}))
    d = agg.to_dict()
    assert d["sessionsWithReReadThrash"] == 2   # s1 and s2 both had a re-read
    assert d["sessionsWithEditChurn"] == 2
    assert d["reReadFilesTotal"] == 2
    # /a recurs in 2 sessions (rank first), max reads = max(4,7)=7; /b in 1.
    assert list(d["reReadFiles"]) == ["/a", "/b"]
    assert d["reReadFiles"]["/a"] == {"sessions": 2, "maxReads": 7}
    assert d["reReadFiles"]["/b"] == {"sessions": 1, "maxReads": 5}
    # /y ranks first by total churn (2+3=5); maxRun = max(2,3)=3.
    assert list(d["editChurnFiles"]) == ["/y", "/z"]
    assert d["editChurnFiles"]["/y"] == {"sessions": 2, "maxRun": 3, "totalRun": 5}
    assert d["editChurnFiles"]["/z"] == {"sessions": 1, "maxRun": 1, "totalRun": 1}


def test_sessions_by_errors_ranked_and_filtered():
    agg = CrossSessionStats()
    agg.observe(_hdr("clean111", "no errors"), _sd(toolErrors=0))          # excluded (no errors)
    agg.observe(_hdr("low22222", "few"), _sd(toolErrors=2, maxErrorRun=1))
    agg.observe(_hdr("high3333", "many"), _sd(toolErrors=9, maxErrorRun=3))
    ranked = agg.to_dict()["sessionsByErrors"]
    assert [s["slug"] for s in ranked] == ["high3333", "low22222"]  # worst first, clean dropped
    assert ranked[0] == {"slug": "high3333", "title": "many", "toolErrors": 9, "maxErrorRun": 3}


def test_top_cap_bounds_evidence_lists():
    agg = CrossSessionStats()
    # 30 distinct re-read files in one session; --top must bound the emitted list
    # while reReadFilesTotal still reports the true cardinality.
    agg.observe(_hdr("s1"), _sd(filesReadGt3={f"/f{i:02d}": 4 for i in range(30)}))
    d = agg.to_dict(top=10)
    assert d["reReadFilesTotal"] == 30
    assert len(d["reReadFiles"]) == 10
    # Deterministic top-10: all tie on sessions=1 & maxReads=4, so key asc wins.
    assert list(d["reReadFiles"]) == [f"/f{i:02d}" for i in range(10)]


def test_empty_aggregate_is_wellformed():
    d = CrossSessionStats().to_dict()
    assert d["sessions"] == 0
    assert d["totals"]["toolCalls"] == 0
    assert d["toolUseByName"] == {}
    assert d["reReadFiles"] == {} and d["editChurnFiles"] == {}
    assert d["sessionsByErrors"] == []


def test_rollup_is_order_independent():
    # The aggregate must be a pure function of the session *set*, not input order
    # (the cross-session analogue of the per-session determinism guarantee).
    a = (_hdr("aaa"), _sd(toolCalls=3, toolErrors=1, toolUseByName={"Bash": 3},
                          toolErrorsByName={"Bash": 1}, filesReadGt3={"/a": 4}))
    b = (_hdr("bbb"), _sd(toolCalls=2, toolUseByName={"Read": 2}, editRunsByFile={"/y": 2}))
    c = (_hdr("ccc"), _sd(toolCalls=1, maxErrorRun=3, toolUseByName={"Edit": 1}))

    def run(order):
        agg = CrossSessionStats()
        for h, s in order:
            agg.observe(h, s)
        return agg.to_dict()

    assert run([a, b, c]) == run([c, a, b]) == run([b, c, a])


# --- End-to-end (subprocess) tests over real JSONL fixtures -----------------

def _write_jsonl(records: list) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for rec in records:
        f.write(json.dumps(rec) + "\n")
    f.close()
    return f.name


def _run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "aggregate_sessions.py"), *args],
        capture_output=True, text=True, input=stdin,
    )


def _session_a() -> str:
    # 2 user msgs, 1 interrupt, one failing Bash + one clean Bash.
    return _write_jsonl([
        {"type": "user", "sessionId": "aaaaaaaa1", "message": {"content": "q1"}},
        {"type": "user", "sessionId": "aaaaaaaa1", "message": {"content": "[Request interrupted by user]"}},
        {"type": "assistant", "sessionId": "aaaaaaaa1",
         "message": {"content": [{"type": "tool_use", "id": "a1", "name": "Bash", "input": {"command": "nope"}}]}},
        {"type": "user", "sessionId": "aaaaaaaa1",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "a1", "content": "not found", "is_error": True}]}},
        {"type": "assistant", "sessionId": "aaaaaaaa1",
         "message": {"content": [{"type": "tool_use", "id": "a2", "name": "Bash", "input": {"command": "echo hi"}}]}},
        {"type": "user", "sessionId": "aaaaaaaa1",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "a2", "content": "hi"}]}},
        {"type": "user", "sessionId": "aaaaaaaa1", "message": {"content": "q2"}},
    ])


def _session_b() -> str:
    # 1 user msg, Read /x four times (re-read thrash), two consecutive Edits to /y (churn).
    recs = [{"type": "user", "sessionId": "bbbbbbbb1", "message": {"content": "b1"}}]
    for i in range(4):
        recs.append({"type": "assistant", "sessionId": "bbbbbbbb1",
                     "message": {"content": [{"type": "tool_use", "id": f"r{i}", "name": "Read", "input": {"file_path": "/x"}}]}})
        recs.append({"type": "user", "sessionId": "bbbbbbbb1",
                     "message": {"content": [{"type": "tool_result", "tool_use_id": f"r{i}", "content": "ok"}]}})
    for i in range(2):
        recs.append({"type": "assistant", "sessionId": "bbbbbbbb1",
                     "message": {"content": [{"type": "tool_use", "id": f"e{i}", "name": "Edit",
                                              "input": {"file_path": "/y", "old_string": "a", "new_string": "b"}}]}})
    return _write_jsonl(recs)


def test_end_to_end_from_paths():
    a, b = _session_a(), _session_b()
    try:
        proc = _run(a, b)
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
        assert d["kind"] == "aggregate"
        assert d["sessions"] == 2
        assert d["totals"] == {
            "userMsgs": 3, "interrupts": 1, "assistantTexts": 0, "toolCalls": 8, "toolErrors": 1,
        }
        assert d["toolUseByName"] == {"Read": 4, "Bash": 2, "Edit": 2}
        assert d["toolErrorsByName"] == {"Bash": 1}
        assert d["sessionsWithReReadThrash"] == 1
        assert d["sessionsWithEditChurn"] == 1
        assert d["reReadFiles"] == {"/x": {"sessions": 1, "maxReads": 4}}
        assert d["editChurnFiles"] == {"/y": {"sessions": 1, "maxRun": 1, "totalRun": 1}}
        assert d["sessionsByErrors"] == [
            {"slug": "aaaaaaaa", "title": "", "toolErrors": 1, "maxErrorRun": 1}
        ]
        assert "aggregated 2 session(s)" in proc.stderr
    finally:
        os.unlink(a)
        os.unlink(b)


def test_end_to_end_from_stdin_matches_positional():
    a, b = _session_a(), _session_b()
    try:
        from_args = json.loads(_run(a, b).stdout)
        from_stdin = json.loads(_run(stdin=f"{a}\n{b}\n").stdout)
        assert from_args == from_stdin
    finally:
        os.unlink(a)
        os.unlink(b)


def test_end_to_end_is_order_independent():
    a, b = _session_a(), _session_b()
    try:
        assert json.loads(_run(a, b).stdout) == json.loads(_run(b, a).stdout)
    finally:
        os.unlink(a)
        os.unlink(b)


def _no_sid_session(title: str, cmd: str) -> str:
    # A distinct session that carries NO sessionId (so slug falls back to
    # "????????") and has exactly one failing tool call (toolErrors=1).
    return _write_jsonl([
        {"type": "ai-title", "aiTitle": title},
        {"type": "assistant",
         "message": {"content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": cmd}}]}},
        {"type": "user",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "b1", "content": "boom", "is_error": True}]}},
    ])


def test_order_independent_when_sessionids_missing():
    # Regression: two DISTINCT sessions with no sessionId tie on (toolErrors=1,
    # maxErrorRun=1), so their slug ("????????") is identical. Without a unique
    # final tie-break the sort falls back to input order and the whole aggregate
    # line flips when the args are reversed — violating the determinism guarantee.
    # The input file path is the tie-break, so the two runs must be identical.
    x = _no_sid_session("alpha", "nope-a")
    y = _no_sid_session("bravo", "nope-b")
    try:
        d1 = json.loads(_run(x, y).stdout)
        d2 = json.loads(_run(y, x).stdout)
        assert d1 == d2, "aggregate must not depend on input order even without sessionIds"
        errs = d1["sessionsByErrors"]
        assert len(errs) == 2 and all(e["toolErrors"] == 1 for e in errs)
        assert all(e["slug"] == "????????" for e in errs)
    finally:
        os.unlink(x)
        os.unlink(y)


def test_duplicate_paths_folded_once():
    # A repeated path must be folded once (not double-counted), keeping the
    # aggregate a pure function of the session *set*; the note reports the drop.
    a = _session_a()
    try:
        proc = _run(a, a)
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
        assert d["sessions"] == 1, "duplicate path must not inflate the session count"
        assert d["totals"]["toolCalls"] == 2, "duplicate path must not double-count tool calls"
        assert "1 duplicate path(s) ignored" in proc.stderr
    finally:
        os.unlink(a)


def test_skips_empty_and_missing_sessions():
    a = _session_a()
    empty = _write_jsonl([{"type": "system", "sessionId": "e", "content": "boot"}])  # no events
    try:
        proc = _run(a, empty, "/no/such/session.jsonl")
        assert proc.returncode == 0, proc.stderr
        d = json.loads(proc.stdout)
        assert d["sessions"] == 1, "only the non-empty, existing session counts"
        assert "1 empty skipped" in proc.stderr
        assert "1 missing/unreadable skipped" in proc.stderr
    finally:
        os.unlink(a)
        os.unlink(empty)


def test_no_paths_errors():
    # No positional paths and nothing on stdin -> a clear error, not an empty run.
    proc = _run(stdin="")
    assert proc.returncode == 1
    assert "no session paths" in proc.stderr


def test_rejects_nonpositive_top():
    proc = _run("--top", "0", "/tmp/whatever.jsonl")
    assert proc.returncode == 2
    assert "--top must be >= 1" in proc.stderr


if __name__ == "__main__":
    test_rollup_sums_totals_and_tool_counters()
    test_rollup_counts_thrash_sessions_by_threshold()
    test_rollup_aggregates_reread_and_edit_files()
    test_sessions_by_errors_ranked_and_filtered()
    test_top_cap_bounds_evidence_lists()
    test_empty_aggregate_is_wellformed()
    test_rollup_is_order_independent()
    test_end_to_end_from_paths()
    test_end_to_end_from_stdin_matches_positional()
    test_end_to_end_is_order_independent()
    test_order_independent_when_sessionids_missing()
    test_duplicate_paths_folded_once()
    test_skips_empty_and_missing_sessions()
    test_no_paths_errors()
    test_rejects_nonpositive_top()
    print("OK")
