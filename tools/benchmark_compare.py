#!/usr/bin/env python3
"""Compare two ``tools/benchmark.py`` outputs and print the deltas.

Usage::

    python tools/benchmark_compare.py before.json after.json

Each measurement is one path's latency percentiles. The table shows the p50 and
p90 of each path in both runs and the change, so a claim of "faster" is backed
by a number rather than asserted. A path that only exists in one run is shown
as new or removed.
"""
from __future__ import annotations

import argparse
import json


def _row(name: str, before: dict | None, after: dict | None) -> str:
    if before is None and after is None:
        return ""
    b50 = before.get("p50") if before else None
    a50 = after.get("p50") if after else None
    b90 = before.get("p90") if before else None
    a90 = after.get("p90") if after else None

    def cell(value):
        return "   —  " if value is None else f"{value:8.2f}"

    delta = ""
    if b50 is not None and a50 is not None and b50:
        pct = (a50 - b50) / b50 * 100.0
        sign = "+" if pct >= 0 else ""
        delta = f"{sign}{pct:6.1f}%"
    return (f"{name:32s} {cell(b50)} {cell(b90)} {cell(a50)} {cell(a90)} {delta:>8s}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    args = ap.parse_args()
    before = json.loads(open(args.before, encoding="utf-8").read())["measurements"]
    after = json.loads(open(args.after, encoding="utf-8").read())["measurements"]
    names = list(before) + [n for n in after if n not in before]
    header = (f"{'path':32s} {'p50 B':>8s} {'p90 B':>8s} {'p50 A':>8s} {'p90 A':>8s} "
              f"{'Δ p50':>8s}")
    print(header)
    print("-" * len(header))
    for name in names:
        line = _row(name, before.get(name), after.get(name))
        if line:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
