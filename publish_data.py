#!/usr/bin/env python3
"""Rebuild a sanitized paper-desk data.json from local state.

The trading state directory is read-only input. This tool never places
orders and never edits trading configuration. If the state files are
missing it exits without writing, so an absent source cannot become a
fake book.

    python3 publish_data.py --state /workspace/trading-paper/state --out data.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from desk_snapshot import StateMissing, build_snapshot, dump, parse_time, validate_public_snapshot


def _now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("--now must be an ISO-8601 timestamp with a timezone")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a sanitized paper-desk snapshot.")
    parser.add_argument(
        "--state",
        default=os.environ.get("PAPER_DESK_STATE", "/workspace/trading-paper/state"),
        help="directory containing equity.json, risk_status.json, and positions.json",
    )
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "data.json"),
        help="output snapshot path (default: data.json next to this script)",
    )
    parser.add_argument("--now", help="override the publish timestamp (ISO-8601)")
    parser.add_argument("--check", help="validate an existing snapshot and do not read state")
    args = parser.parse_args(argv)

    try:
        if args.check:
            doc = json.loads(Path(args.check).read_text(encoding="utf-8"))
            validate_public_snapshot(doc)
            print(f"publish_data: {Path(args.check).name} ok hash={doc['content_hash'][:12]}")
            return 0
        doc = build_snapshot(Path(args.state), now=_now(args.now))
    except StateMissing as exc:
        print(f"publish_data: state unavailable ({exc}). no file written.", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"publish_data: {exc}", file=sys.stderr)
        return 1

    destination = Path(args.out)
    destination.write_text(dump(doc), encoding="utf-8")
    print(
        "publish_data: wrote {name} equity={equity:.2f} pnl_since_start={pnl:.2f} "
        "open={open_count} live={live} published_at={published}".format(
            name=destination.name,
            equity=doc["equity"],
            pnl=doc["pnl_since_start"],
            open_count=doc["open_count"],
            live=str(doc["live"]).lower(),
            published=doc["published_at"],
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        sys.exit(1)
