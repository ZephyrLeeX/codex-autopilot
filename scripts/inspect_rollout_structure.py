#!/usr/bin/env python3
"""Read-only structural probe for Codex rollout JSONL files (L2 diagnostic tool).

Prints ONLY structural information: event type distribution, top-level key
names, and nested key names of selected event payloads. Never prints message
content, file paths of other sessions, or any user data.

Usage:
    python3 scripts/inspect_rollout_structure.py <rollout.jsonl> [--limit N]

Part of the codex-autopilot L2 acceptance tooling (see docs/testing.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def walk_keys(obj: object, prefix: str = "", depth: int = 0, max_depth: int = 3) -> set[str]:
    """Collect nested key names (not values) up to max_depth."""
    keys: set[str] = set()
    if depth >= max_depth or not isinstance(obj, dict):
        return keys
    for k, v in obj.items():
        keys.add(f"{prefix}{k}")
        keys |= walk_keys(v, f"{prefix}{k}.", depth + 1, max_depth)
    return keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rollout", type=Path, help="path to a rollout-*.jsonl file")
    parser.add_argument("--limit", type=int, default=5000, help="max lines to scan")
    args = parser.parse_args()

    path: Path = args.rollout
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    type_counter: Counter[str] = Counter()
    payload_type_counter: Counter[str] = Counter()
    keys_by_type: dict[str, set[str]] = {}
    torn_lines = 0
    total = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= args.limit:
                break
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                torn_lines += 1
                continue
            t = record.get("type", "<missing>")
            type_counter[t] += 1
            keys_by_type.setdefault(t, set()).update(walk_keys(record))
            payload = record.get("payload")
            if isinstance(payload, dict):
                pt = payload.get("type", "<missing>")
                payload_type_counter[pt] += 1
                keys_by_type.setdefault(f"{t}.payload[{pt}]", set()).update(
                    walk_keys(payload, max_depth=2)
                )

    print(f"file lines scanned: {total}, torn/unparseable tail lines: {torn_lines}")
    print("\ntop-level 'type' distribution:")
    for t, n in type_counter.most_common():
        print(f"  {t:40s} {n}")
    print("\npayload 'type' distribution (top 30):")
    for t, n in payload_type_counter.most_common(30):
        print(f"  {t:40s} {n}")
    print("\nkey names per type (values are NEVER printed):")
    for t in sorted(keys_by_type):
        ks = sorted(keys_by_type[t])
        shown = ", ".join(ks[:25]) + (" ..." if len(ks) > 25 else "")
        print(f"  {t}: {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
