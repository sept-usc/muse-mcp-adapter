"""Run the lab:  python -m trustlab run [--out DIR] [--scenario ID ...] [--arm ID ...]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .report import terminal, write_report
from .runner import ALL_ARMS, ARMS, arms_by_id, run_all
from .scenarios import SCENARIOS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trustlab", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the scenarios and paths")
    run = sub.add_parser("run", help="play the scenarios on each path and write the report")
    run.add_argument("--out", type=Path, default=Path("trust-demo"), help="where to write")
    run.add_argument("--scenario", action="append", help="only this scenario (repeatable)")
    run.add_argument("--arm", action="append", help="only this path (repeatable)")
    args = parser.parse_args(argv)

    if args.command == "list":
        for scenario in SCENARIOS:
            print(f"{scenario.id:20} {scenario.title}")
        print()
        for arm in ALL_ARMS:
            print(f"{arm.id:24} {arm.label}")
        return 0

    chosen = [s for s in SCENARIOS if not args.scenario or s.id in args.scenario]
    arms = arms_by_id(args.arm) if args.arm else list(ARMS)
    unknown = set(args.scenario or []) - {s.id for s in SCENARIOS}
    unknown |= set(args.arm or []) - {a.id for a in ALL_ARMS}
    if unknown or not chosen or not arms:
        print(f"Nothing to run; unknown names: {sorted(unknown)}", file=sys.stderr)
        return 2
    outcomes = run_all(arms, chosen, progress=lambda step: print(f"  running {step}", flush=True))
    print()
    print(terminal(outcomes, arms))
    page = write_report(outcomes, args.out, arms)
    print(f"\nReport: {page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
