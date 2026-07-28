#!/usr/bin/env python3
"""Print the ambiguity threshold implied by a labelled corpus.

    python tools/calibrate_ambiguity.py
    python tools/calibrate_ambiguity.py --corpus tests/mast/ambiguity.jsonl

Exits non-zero when the corpus carries no planner scores, so that wiring this
into CI before the scores exist fails loudly instead of printing a number that
came from nowhere. The logic lives in `overmind.calibration` -- this file is a
command line and nothing else, so the chooser stays under mypy strict and
under test.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from overmind.calibration import DEFAULT_CORPUS, PROVISIONAL_DEFAULT, calibrate, load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args(argv)

    rows = load(args.corpus)
    result = calibrate(rows)

    if result is None:
        scored = sum(1 for r in rows if r.score is not None)
        print(
            f"{args.corpus}: {len(rows)} labelled goal(s), {scored} scored. "
            "a threshold needs planner scores for goals of both labels; "
            f"until then overmind.toml ships the provisional {PROVISIONAL_DEFAULT:.2f}.",
            file=sys.stderr,
        )
        return 1

    print(result.report())
    if abs(result.threshold - PROVISIONAL_DEFAULT) > 0.05:
        print(
            f"note: this differs from the shipped {PROVISIONAL_DEFAULT:.2f}; "
            "update overmind.toml and calibration.PROVISIONAL_DEFAULT together."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
