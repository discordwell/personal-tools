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


def test_classification_on_fixture():
    fixture = [
        {"type": "user", "timestamp": "t1", "sessionId": "s", "message": {"content": "regular question"}},
        {"type": "user", "timestamp": "t2", "sessionId": "s", "message": {"content": "[Request interrupted by user]"}},
        {"type": "user", "timestamp": "t3", "sessionId": "s", "isMeta": True,
         "message": {"content": "<local-command-caveat>caveat injection text</local-command-caveat>"}},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for line in fixture:
            f.write(json.dumps(line) + "\n")
        path = f.name
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "summarize_session.py"), path],
            capture_output=True, text=True, check=True,
        )
        lines = [json.loads(l) for l in proc.stdout.strip().splitlines()]
        assert lines[0]["kind"] == "header"
        events = lines[1:]
        kinds = [e["kind"] for e in events]
        assert kinds == ["user_msg", "user_interrupt", "user_msg"], kinds
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_is_interrupt_text()
    test_classification_on_fixture()
    print("OK")
