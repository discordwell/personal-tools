#!/usr/bin/env python3
"""Roll the per-session `--stats` counts up across many sessions, deterministically.

`summarize_session.py --stats` gives exact, mechanical counts for *one* session.
The skill, however, reports patterns that hold *across* sessions ("reportable only
if it appears across >=2 distinct sessions") and cites cross-session counts in the
journal. Summing a dozen-plus per-session footers by eye is exactly the kind of
counting an LLM gets wrong — so this tool does it from ground truth, reusing
``summarize_session.parse_session`` (no re-implementation of the parser) and
emitting a single deterministic ``aggregate`` object.

The output rolls up only the mechanical signals; the interpretive judgements
(pushback language, knowledge-gap questions, Bash-instead-of-Read) stay with the
skill, which reads the per-session events for those.

Input: session .jsonl paths as positional args, or newline-separated on stdin
(so it composes with ``list_sessions.py | aggregate_sessions.py``). Empty,
missing, and unreadable sessions are skipped with a stderr note; the aggregate
counts only sessions that had at least one event.

Usage:
    list_sessions.py --days 7 | aggregate_sessions.py [--top N]
    aggregate_sessions.py session1.jsonl session2.jsonl ... [--top N]
"""

import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from summarize_session import parse_session  # noqa: E402


# Default cap on the per-file / per-session evidence lists so the aggregate stays
# bounded no matter how many sessions are fed in. Rankings are deterministic, so
# the top-N is stable; the accompanying *Total fields report the true cardinality.
DEFAULT_TOP = 25


def _ranked(counter: Counter) -> dict:
    # Count desc, then key asc — the repo-wide determinism contract (same as
    # SessionStats._ranked and list_sessions' path tie-break): equal counts must
    # order by key name so the same inputs always surface in the same order.
    return {k: v for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))}


class CrossSessionStats:
    """Accumulate the exact per-session stats into one cross-session view.

    Every roll-up is order-independent (sums, Counters, per-file maxima) and every
    emitted ordering is a total sort on counts-then-name, so the aggregate is a
    pure function of the *set* of session contents — the cross-session analogue of
    the per-session determinism guarantee.
    """

    def __init__(self) -> None:
        self.sessions = 0                      # sessions with >=1 event (contributing)
        self.user_msgs = 0
        self.interrupts = 0
        self.assistant_texts = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.tool_use_by_name: Counter = Counter()
        self.tool_errors_by_name: Counter = Counter()
        self.sessions_back_to_back = 0         # maxErrorRun >= 2 (a back-to-back failure)
        self.sessions_retry_loop = 0           # maxErrorRun >= 3 (a clear retry loop)
        self.sessions_reread = 0               # any file Read >3x
        self.sessions_edit_churn = 0           # any file with >=2 consecutive Edits
        self.reread_sessions: Counter = Counter()  # file -> # sessions it was re-read in
        self.reread_max: dict = {}             # file -> max reads in a single session
        self.edit_sessions: Counter = Counter()    # file -> # sessions it was churned in
        self.edit_max: dict = {}               # file -> max consecutive-edit run in one session
        self.edit_total: Counter = Counter()   # file -> summed run length across sessions
        self.per_session: list = []            # [{slug, title, toolErrors, maxErrorRun}]

    def observe(self, header: dict, sd: dict) -> None:
        """Fold one session's header + ``SessionStats.to_dict()`` into the totals."""
        self.sessions += 1
        self.user_msgs += sd["userMsgs"]
        self.interrupts += sd["interrupts"]
        self.assistant_texts += sd["assistantTexts"]
        self.tool_calls += sd["toolCalls"]
        self.tool_errors += sd["toolErrors"]
        self.tool_use_by_name.update(sd["toolUseByName"])
        self.tool_errors_by_name.update(sd["toolErrorsByName"])

        mer = sd["maxErrorRun"]
        if mer >= 2:
            self.sessions_back_to_back += 1
        if mer >= 3:
            self.sessions_retry_loop += 1

        reread = sd["filesReadGt3"]
        if reread:
            self.sessions_reread += 1
        for fp, n in reread.items():
            self.reread_sessions[fp] += 1
            self.reread_max[fp] = max(self.reread_max.get(fp, 0), n)

        churn = sd["editRunsByFile"]
        if churn:
            self.sessions_edit_churn += 1
        for fp, n in churn.items():
            self.edit_sessions[fp] += 1
            self.edit_max[fp] = max(self.edit_max.get(fp, 0), n)
            self.edit_total[fp] += n

        sid = header.get("sessionId") or ""
        self.per_session.append({
            "slug": sid[:8] or "????????",
            "path": header.get("path") or "",   # unique per distinct session file: the airtight tie-break
            "title": header.get("title") or "",
            "toolErrors": sd["toolErrors"],
            "maxErrorRun": mer,
        })

    def _reread_files(self, top: int) -> dict:
        # Recurrence first: a file re-read >3x in many sessions is the strongest
        # thrash signal, so rank by session-count, then intensity, then name.
        items = sorted(
            self.reread_sessions.items(),
            key=lambda kv: (-kv[1], -self.reread_max[kv[0]], kv[0]),
        )
        return {fp: {"sessions": s, "maxReads": self.reread_max[fp]} for fp, s in items[:top]}

    def _edit_churn_files(self, top: int) -> dict:
        # Total churn first: summed run length across sessions surfaces the file
        # fought with most overall; ties break on session-count, then name.
        items = sorted(
            self.edit_sessions.items(),
            key=lambda kv: (-self.edit_total[kv[0]], -kv[1], kv[0]),
        )
        return {
            fp: {"sessions": s, "maxRun": self.edit_max[fp], "totalRun": self.edit_total[fp]}
            for fp, s in items[:top]
        }

    def _sessions_by_errors(self, top: int) -> list:
        # Worst sessions first, so the skill can cite concrete evidence. The input
        # file path is the final tie-break: it is unique per distinct session file
        # (main folds each path once), so the ordering is a genuine total order —
        # order-independent even when a sessionId is absent (slug "????????") or
        # two 8-char slugs collide on identical error counts. Path is not emitted.
        with_err = [s for s in self.per_session if s["toolErrors"] > 0]
        with_err.sort(key=lambda s: (-s["toolErrors"], -s["maxErrorRun"], s["slug"], s["path"]))
        return [
            {"slug": s["slug"], "title": s["title"],
             "toolErrors": s["toolErrors"], "maxErrorRun": s["maxErrorRun"]}
            for s in with_err[:top]
        ]

    def to_dict(self, top: int = DEFAULT_TOP) -> dict:
        return {
            "kind": "aggregate",
            "sessions": self.sessions,
            "totals": {
                "userMsgs": self.user_msgs,
                "interrupts": self.interrupts,
                "assistantTexts": self.assistant_texts,
                "toolCalls": self.tool_calls,
                "toolErrors": self.tool_errors,
            },
            "toolUseByName": _ranked(self.tool_use_by_name),
            "toolErrorsByName": _ranked(self.tool_errors_by_name),
            "sessionsWithBackToBackFailure": self.sessions_back_to_back,
            "sessionsWithRetryLoop": self.sessions_retry_loop,
            "sessionsWithReReadThrash": self.sessions_reread,
            "sessionsWithEditChurn": self.sessions_edit_churn,
            "reReadFilesTotal": len(self.reread_sessions),
            "reReadFiles": self._reread_files(top),
            "editChurnFilesTotal": len(self.edit_sessions),
            "editChurnFiles": self._edit_churn_files(top),
            "sessionsByErrors": self._sessions_by_errors(top),
        }


def _read_paths(positional: list) -> list:
    """Positional paths win; otherwise read newline-separated paths from stdin."""
    if positional:
        return positional
    if sys.stdin.isatty():
        return []
    return [line.strip() for line in sys.stdin if line.strip()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("paths", nargs="*", help="Session .jsonl paths (else read from stdin).")
    p.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Cap on per-file / per-session evidence lists (default: {DEFAULT_TOP}).",
    )
    args = p.parse_args()
    if args.top < 1:
        print("error: --top must be >= 1", file=sys.stderr)
        return 2

    paths = _read_paths(args.paths)
    if not paths:
        print("error: no session paths given (pass paths or pipe them on stdin)", file=sys.stderr)
        return 1

    # Fold each distinct session once. A repeated path would double-count every
    # total and inflate `sessions`, breaking the "pure function of the session
    # set" contract; drop repeats (first occurrence wins, order preserved).
    seen: set = set()
    unique_paths = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique_paths.append(path)
    duplicate_paths = len(paths) - len(unique_paths)
    paths = unique_paths

    agg = CrossSessionStats()
    skipped_missing = 0
    skipped_empty = 0
    for path in paths:
        if not os.path.exists(path):
            print(f"warning: not found, skipping: {path}", file=sys.stderr)
            skipped_missing += 1
            continue
        try:
            header, _events, stats = parse_session(path)
        except OSError as e:
            print(f"warning: unreadable, skipping: {path} ({e})", file=sys.stderr)
            skipped_missing += 1
            continue
        # An event-free session (empty/whitespace/only non-conversational records)
        # contributes nothing but would inflate the session count, so drop it.
        if header["eventCount"] == 0:
            skipped_empty += 1
            continue
        agg.observe(header, stats.to_dict())

    print(json.dumps(agg.to_dict(args.top), ensure_ascii=False))

    notes = [f"aggregated {agg.sessions} session(s)"]
    if skipped_empty:
        notes.append(f"{skipped_empty} empty skipped")
    if skipped_missing:
        notes.append(f"{skipped_missing} missing/unreadable skipped")
    if duplicate_paths:
        notes.append(f"{duplicate_paths} duplicate path(s) ignored")
    print("note: " + ", ".join(notes), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
