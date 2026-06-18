#!/usr/bin/env python3
"""Distill a Claude Code JSONL transcript into a compact event list for analysis.

Reads one .jsonl session file and emits NDJSON to stdout. The first line is
always a `header` (path, sessionId, title, lastPrompt, eventCount, malformed);
each subsequent line is one structured event with: idx, ts, kind, and
kind-specific fields. The output is bounded (long strings truncated) so a Claude
reading it can fit many sessions in context. The skill (Claude) does the actual
pattern detection; this script is a parser only.

Event kinds:
    user_msg            text=...               (from user role, typed text)
    user_interrupt      text=...               (interruption / corrections)
    meta                text=...               (harness-injected user record: caveats,
                                                command output, reminders — not typed)
    assistant_text      text=...               (assistant prose only, no tools)
    tool_use            name, input_brief, id  (tool call)
    tool_result         id, ok, brief          (tool output, ok=False if the call errored)
    elided              skipped=N              (marks events dropped to satisfy --max-events)

Usage:
    summarize_session.py path/to/session.jsonl [--max-text N] [--max-events N]
"""

import argparse
import json
import os
import sys
from typing import Any


def _positive_int(value: str) -> int:
    # Both caps must be >= 1: max-text feeds _truncate (n<1 degenerates), and
    # max-events feeds the elision split `(n-1)//3` (n<1 would over/under-keep).
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    if n < 1:
        # Degenerate cap; "s[:n-1] + …" would wrap to a negative index and leak
        # most of the string (e.g. n=0 -> s[:-1]). Emit nothing instead.
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _is_interrupt_text(text: str) -> bool:
    return text.lstrip().startswith("[Request interrupted by user")


def _user_text_kind(text: str, is_meta: bool) -> str:
    """Classify a user-role text block. Harness-injected records (isMeta) are
    things the user never typed — command output, caveats, system reminders —
    so they get their own ``meta`` kind and are excluded from the skill's
    pushback / knowledge-gap scans, which only look at real ``user_msg`` text."""
    if is_meta:
        return "meta"
    return "user_interrupt" if _is_interrupt_text(text) else "user_msg"


def _input_brief(name: str, inp: Any, max_text: int) -> str:
    """Render a tool-use input compactly. For Bash/Edit/Write, surface key fields;
    otherwise dump truncated JSON."""
    if not isinstance(inp, dict):
        return _truncate(str(inp), max_text)
    if name == "Bash":
        return _truncate(str(inp.get("command", "")), max_text)
    if name == "Edit":
        # Coerce via str() before slicing: a malformed record with a null
        # old_string/new_string would otherwise raise on None[:60] and abort the
        # whole session summary over one bad line.
        old = str(inp.get("old_string") or "")[:60]
        new = str(inp.get("new_string") or "")[:60]
        return _truncate(f"{inp.get('file_path', '?')} :: {old} -> {new}", max_text)
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


def _result_brief(content: Any, max_text: int, block_is_error: Any = False) -> tuple[bool, str]:
    """Return (ok, brief) for a tool_result block.

    ``block_is_error`` is the tool_result block's own ``is_error`` flag. In real
    Claude Code transcripts this is where errors live — the block carries
    ``is_error: true`` alongside a *string* ``content`` (a failed Bash run, a
    file-not-found, etc.). Earlier this function only saw ``content`` and only
    scanned inner list items for a marker, so every string-content error was
    reported as ok=True, silently defeating the skill's tool-confusion scan.
    We OR the block-level flag with any inner-list marker (a fallback for
    structured content) so both shapes are caught."""
    is_error = bool(block_is_error)
    if isinstance(content, str):
        return (not is_error), _truncate(content, max_text)
    text_parts: list[str] = []
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
    p.add_argument("--max-text", type=_positive_int, default=240, help="Per-event text length cap (>=1).")
    p.add_argument("--max-events", type=_positive_int, default=2000, help="Total events cap, oldest first (>=1).")
    args = p.parse_args()

    if not os.path.exists(args.path):
        print(f"error: not found: {args.path}", file=sys.stderr)
        return 1

    out: list[dict] = []
    session_id = None
    title = None
    last_prompt = None
    malformed = 0

    with open(args.path, encoding="utf-8", errors="replace") as f:
        for idx, raw in enumerate(f):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                malformed += 1
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
                is_meta = bool(obj.get("isMeta"))
                if isinstance(content, str):
                    kind = _user_text_kind(content, is_meta)
                    out.append({"idx": idx, "ts": ts, "kind": kind, "text": _truncate(content, args.max_text)})
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            ok, brief = _result_brief(
                                block.get("content"), args.max_text, block.get("is_error")
                            )
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
                            kind = _user_text_kind(text, is_meta)
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
        "malformed": malformed,
    }))

    # Cap total events; keep oldest first so flow is readable. Reserve one slot
    # for the elided marker so total output stays bounded by max_events.
    if len(out) > args.max_events:
        head = (args.max_events - 1) // 3
        tail = (args.max_events - 1) - head
        skipped = len(out) - head - tail
        # Guard tail==0: out[-0:] is out[0:] (the whole list), so an empty tail
        # would leak every event. Use an explicit empty slice instead.
        tail_events = out[-tail:] if tail else []
        out = out[:head] + [{"kind": "elided", "skipped": skipped}] + tail_events

    for e in out:
        print(json.dumps(e, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
