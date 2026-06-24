#!/usr/bin/env python3
"""Distill a Claude Code JSONL transcript into a compact event list for analysis.

Reads one .jsonl session file and emits NDJSON to stdout. The first line is
always a `header` (path, sessionId, title, lastPrompt, eventCount, malformed);
each subsequent line is one structured event with: idx, ts, kind, and
kind-specific fields. With --stats, a single `stats` line is emitted last. The
output is bounded (long strings truncated) so a Claude reading it can fit many
sessions in context. The skill (Claude) interprets the output; this script
parses and computes mechanical counts only — it does no pattern judgement.

Event kinds:
    user_msg            text=...               (from user role, typed text)
    user_interrupt      text=...               (interruption / corrections)
    meta                text=...               (harness-injected user record: caveats,
                                                command output, reminders — not typed)
    assistant_text      text=...               (assistant prose only, no tools)
    tool_use            name, input_brief, id  (tool call)
    tool_result         id, ok, brief          (tool output, ok=False if the call errored)
    elided              skipped=N              (marks events dropped to satisfy --max-events)
    stats               <see SessionStats>     (last line, only with --stats; counts the
                                                FULL session, not the elided subset)

Usage:
    summarize_session.py path/to/session.jsonl [--max-text N] [--max-events N] [--stats]
"""

import argparse
import json
import os
import sys
from collections import Counter
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


class SessionStats:
    """Deterministic, mechanical tool-usage counts for one session.

    The skill's pattern detection (SKILL.md Step 3) leans on counts that an LLM
    reading many sessions of NDJSON tends to get wrong: how many tool calls
    errored (and which tools), which files were Read more than 3 times, where
    Edits churned on a single file, and the longest back-to-back tool-failure
    streak (a retry loop). Those are exact and unambiguous, so we compute them
    here and let the skill *interpret* them.

    We deliberately do NOT flag the "Bash cat/head/tail/sed/awk instead of Read"
    tool-confusion pattern mechanically. On real transcripts a regex for it
    matches ~79% of Bash commands — pipes into ``head``/``tail``, ``cat <<EOF``
    heredocs, ``echo ... > /dev/null`` — so it is not reliably countable. That
    judgement stays with the skill, which can read the command and decide.
    """

    def __init__(self) -> None:
        self.user_msgs = 0
        self.interrupts = 0
        self.assistant_texts = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.max_error_run = 0                   # longest streak of consecutive failing tool_results
        self.tool_use_by_name: Counter = Counter()
        self.tool_errors_by_name: Counter = Counter()
        self.read_counts: Counter = Counter()  # Read file_path -> times read
        self.edit_runs: Counter = Counter()     # file_path -> back-to-back edits (run len >=2)
        self._name_by_id: dict = {}             # tool_use_id -> tool name
        self._last_edit_file = None
        self._cur_error_run = 0                  # running streak length; reset by any ok result

    def observe_user_text(self, kind: str) -> None:
        if kind == "user_msg":
            self.user_msgs += 1
        elif kind == "user_interrupt":
            self.interrupts += 1
        # `meta` is a harness injection, not a user turn — not counted.

    def observe_assistant_text(self) -> None:
        self.assistant_texts += 1

    def observe_tool_use(self, name: str, inp: Any, tool_id: Any) -> None:
        self.tool_calls += 1
        self.tool_use_by_name[name] += 1
        if isinstance(tool_id, str):
            self._name_by_id[tool_id] = name
        fp = inp.get("file_path") if isinstance(inp, dict) else None
        fp = fp if isinstance(fp, str) and fp else None
        if name == "Read":
            if fp:
                self.read_counts[fp] += 1
        elif name == "Edit":
            # Count an Edit whose target equals the immediately preceding Edit's
            # target. Intervening non-Edit tools don't break the run, so the
            # classic edit/test/edit churn still shows; an Edit to a *different*
            # file resets it. N consecutive edits to one file -> N-1 here.
            if fp and fp == self._last_edit_file:
                self.edit_runs[fp] += 1
            self._last_edit_file = fp

    def observe_tool_result(self, tool_id: Any, ok: bool) -> None:
        if ok:
            # A success ends any retry streak (the call finally worked).
            self._cur_error_run = 0
            return
        self.tool_errors += 1
        name = self._name_by_id.get(tool_id, "?") if isinstance(tool_id, str) else "?"
        self.tool_errors_by_name[name] += 1
        # Track the longest run of back-to-back failures. Only tool_results move
        # the streak — intervening assistant text / tool_use (the "let me try
        # again" turn) are part of the same retry loop, so they don't reset it;
        # a *successful* result does. maxErrorRun >= 2 means a real retry loop.
        self._cur_error_run += 1
        if self._cur_error_run > self.max_error_run:
            self.max_error_run = self._cur_error_run

    @staticmethod
    def _ranked(counter: Counter) -> dict:
        # Count desc, then key asc: deterministic order (SKILL.md requires
        # same inputs -> same surfaced patterns).
        return {k: v for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))}

    def to_dict(self) -> dict:
        return {
            "kind": "stats",
            "userMsgs": self.user_msgs,
            "interrupts": self.interrupts,
            "assistantTexts": self.assistant_texts,
            "toolCalls": self.tool_calls,
            "toolErrors": self.tool_errors,
            # Longest run of consecutive failing tool_results — a retry loop.
            "maxErrorRun": self.max_error_run,
            "toolUseByName": self._ranked(self.tool_use_by_name),
            "toolErrorsByName": self._ranked(self.tool_errors_by_name),
            # Only files read >3 times — the SKILL.md re-read thrash threshold.
            "filesReadGt3": {k: v for k, v in self._ranked(self.read_counts).items() if v > 3},
            # Any entry means >=2 consecutive edits to that file.
            "editRunsByFile": self._ranked(self.edit_runs),
        }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("path", help="Path to session .jsonl")
    p.add_argument("--max-text", type=_positive_int, default=240, help="Per-event text length cap (>=1).")
    p.add_argument("--max-events", type=_positive_int, default=2000, help="Total events cap, oldest first (>=1).")
    p.add_argument(
        "--stats",
        action="store_true",
        help="Emit a final `stats` line with deterministic tool-usage counts for the full session.",
    )
    args = p.parse_args()

    if not os.path.exists(args.path):
        print(f"error: not found: {args.path}", file=sys.stderr)
        return 1

    out: list[dict] = []
    stats = SessionStats()
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
                    stats.observe_user_text(kind)
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
                            stats.observe_tool_result(block.get("tool_use_id"), ok)
                        elif block.get("type") == "text":
                            text = str(block.get("text", ""))
                            kind = _user_text_kind(text, is_meta)
                            out.append({"idx": idx, "ts": ts, "kind": kind, "text": _truncate(text, args.max_text)})
                            stats.observe_user_text(kind)
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
                            stats.observe_assistant_text()
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
                        stats.observe_tool_use(name, block.get("input"), block.get("id"))
                continue
            # Skip the non-conversational record types observed in real
            # transcripts: permission-mode / mode / file-history-snapshot /
            # system / queue-operation / attachment / agent-name.

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

    # Stats footer reflects the FULL session (accumulated during the parse,
    # before elision), so the counts are accurate even when events were dropped.
    if args.stats:
        print(json.dumps(stats.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
