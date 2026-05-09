#!/usr/bin/env python3
"""Distill a Claude Code JSONL transcript into a compact event list for analysis.

Reads one .jsonl session file and emits NDJSON to stdout — one structured event
per line. Each event has: idx, ts, kind, and kind-specific fields. The output
is bounded (long strings truncated) so a Claude reading it can fit many sessions
in context. The skill (Claude) does the actual pattern detection; this script
is a parser only.

Event kinds:
    user_msg            text=...               (from user role, text content)
    user_interrupt      text=...               (interruption / corrections)
    assistant_text      text=...               (assistant prose only, no tools)
    tool_use            name, input_brief, id  (tool call)
    tool_result         id, ok, brief          (tool output, ok=False if error)
    summary             title                  (ai-title or last-prompt)

Usage:
    summarize_session.py path/to/session.jsonl [--max-text N] [--max-events N]
"""

import argparse
import json
import os
import sys
from typing import Any


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _is_interrupt_text(text: str) -> bool:
    return text.lstrip().startswith("[Request interrupted by user")


def _input_brief(name: str, inp: Any, max_text: int) -> str:
    """Render a tool-use input compactly. For Bash/Edit/Write, surface key fields;
    otherwise dump truncated JSON."""
    if not isinstance(inp, dict):
        return _truncate(str(inp), max_text)
    if name == "Bash":
        return _truncate(str(inp.get("command", "")), max_text)
    if name == "Edit":
        return _truncate(
            f"{inp.get('file_path', '?')} :: {inp.get('old_string', '')[:60]} -> {inp.get('new_string', '')[:60]}",
            max_text,
        )
    if name == "Write":
        return _truncate(f"{inp.get('file_path', '?')} ({len(str(inp.get('content', '')))} chars)", max_text)
    if name == "Read":
        return _truncate(str(inp.get("file_path", "?")), max_text)
    if name in ("Grep", "Glob"):
        return _truncate(json.dumps({k: v for k, v in inp.items() if k in ("pattern", "path", "glob")}), max_text)
    # Default: small JSON dump
    try:
        return _truncate(json.dumps(inp, separators=(",", ":")), max_text)
    except Exception:
        return _truncate(str(inp), max_text)


def _result_brief(content: Any, max_text: int) -> tuple[bool, str]:
    """Return (ok, brief) for a tool_result content array."""
    if isinstance(content, str):
        return True, _truncate(content, max_text)
    text_parts: list[str] = []
    is_error = False
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("is_error") or block.get("type") == "tool_use_error":
                is_error = True
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "image":
                text_parts.append("[image]")
    return (not is_error), _truncate(" | ".join(text_parts), max_text)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("path", help="Path to session .jsonl")
    p.add_argument("--max-text", type=int, default=240, help="Per-event text length cap.")
    p.add_argument("--max-events", type=int, default=2000, help="Total events cap (oldest first).")
    args = p.parse_args()

    if not os.path.exists(args.path):
        print(f"error: not found: {args.path}", file=sys.stderr)
        return 1

    out: list[dict] = []
    session_id = None
    title = None
    last_prompt = None

    with open(args.path, encoding="utf-8", errors="replace") as f:
        for idx, raw in enumerate(f):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            t = obj.get("type")
            ts = obj.get("timestamp")
            if session_id is None:
                session_id = obj.get("sessionId")

            if t == "ai-title":
                title = obj.get("aiTitle")
                continue
            if t == "last-prompt":
                last_prompt = obj.get("lastPrompt")
                continue

            if t == "user":
                msg = obj.get("message", {})
                content = msg.get("content")
                if isinstance(content, str):
                    kind = "user_interrupt" if _is_interrupt_text(content) else "user_msg"
                    out.append({"idx": idx, "ts": ts, "kind": kind, "text": _truncate(content, args.max_text)})
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            ok, brief = _result_brief(block.get("content"), args.max_text)
                            out.append({
                                "idx": idx,
                                "ts": ts,
                                "kind": "tool_result",
                                "id": block.get("tool_use_id"),
                                "ok": ok,
                                "brief": brief,
                            })
                        elif block.get("type") == "text":
                            text = str(block.get("text", ""))
                            kind = "user_interrupt" if _is_interrupt_text(text) else "user_msg"
                            out.append({"idx": idx, "ts": ts, "kind": kind, "text": _truncate(text, args.max_text)})
                continue

            if t == "assistant":
                msg = obj.get("message", {})
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = str(block.get("text", "")).strip()
                        if text:
                            out.append({"idx": idx, "ts": ts, "kind": "assistant_text", "text": _truncate(text, args.max_text)})
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        out.append({
                            "idx": idx,
                            "ts": ts,
                            "kind": "tool_use",
                            "name": name,
                            "id": block.get("id"),
                            "input_brief": _input_brief(name, block.get("input"), args.max_text),
                        })
                continue
            # Skip permission-mode / file-history-snapshot / system / queue-operation / attachment.

    # Header summary first.
    print(json.dumps({
        "kind": "header",
        "path": args.path,
        "sessionId": session_id,
        "title": title,
        "lastPrompt": _truncate(last_prompt or "", args.max_text),
        "eventCount": len(out),
    }))

    # Cap total events; keep oldest first so flow is readable.
    if len(out) > args.max_events:
        # Keep first 1/3 and last 2/3 — ends usually richer in pain points.
        head = args.max_events // 3
        tail = args.max_events - head
        out = out[:head] + [{"kind": "elided", "skipped": len(out) - args.max_events}] + out[-tail:]

    for e in out:
        print(json.dumps(e, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
