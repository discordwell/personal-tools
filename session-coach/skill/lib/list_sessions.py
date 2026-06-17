#!/usr/bin/env python3
"""Enumerate Claude Code session JSONL files filtered by mtime, sorted newest-first.

Usage:
    list_sessions.py [--days N | --count N] [--root PATH] [--min-bytes N]

Default: --days 7. Output: one absolute path per line.
"""

import argparse
import os
import sys
import time
from pathlib import Path


def nonneg_int(value: str) -> int:
    # Guard against negatives: a negative --count would slice with Python's
    # negative-index semantics (e.g. jsonls[:-1] drops the OLDEST file instead
    # of returning nothing), and a negative --days yields a future cutoff.
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {n}")
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--days", type=nonneg_int, help="Include files modified within last N days.")
    g.add_argument("--count", type=nonneg_int, help="Include the most-recently-modified N files.")
    p.add_argument(
        "--root",
        default=os.path.expanduser("~/.claude/projects"),
        help="Claude Code projects root (default: ~/.claude/projects)",
    )
    p.add_argument(
        "--min-bytes",
        type=int,
        default=1024,
        help="Skip files smaller than this many bytes (default: 1024).",
    )
    args = p.parse_args()

    # Default to 7-day window if nothing specified.
    if args.days is None and args.count is None:
        args.days = 7

    root = Path(args.root)
    if not root.exists():
        print(f"error: root not found: {root}", file=sys.stderr)
        return 1

    # Top-level .jsonl files only (the per-session transcripts), not subagent files.
    jsonls = []
    skipped_small = 0
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_size < args.min_bytes:
                skipped_small += 1
                continue
            jsonls.append((st.st_mtime, f))

    jsonls.sort(key=lambda x: x[0], reverse=True)

    if args.count is not None:
        jsonls = jsonls[: args.count]
    elif args.days is not None:
        cutoff = time.time() - args.days * 86400
        jsonls = [(m, f) for (m, f) in jsonls if m >= cutoff]

    if skipped_small:
        print(
            f"note: skipped {skipped_small} file(s) under {args.min_bytes} bytes",
            file=sys.stderr,
        )

    for _, f in jsonls:
        print(str(f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
