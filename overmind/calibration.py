"""Choosing `ambiguity_threshold` from evidence instead of taste.

`ambiguity_halt` refuses to spend money on a goal the planner reports as
underspecified. The refusal is only as good as the number it compares against,
and `ambiguity_threshold = 0.65` was a guess -- a plausible-looking constant
with nothing behind it. A threshold that is too low halts every real run until
someone turns the gate off; too high and the gate never fires at all.

This module does the part that can be done without a model: it defines the
labelled corpus format, loads it, and picks the cut that best separates the two
labels. What it deliberately does not do is invent scores. The score comes from
the planning model's own self-report, so the corpus ships with `score: null`
and a recorded procedure for filling it in:

    for each goal in tests/calibration/ambiguity.jsonl:
        overmind plan "<goal>" --dry-run   # send the printed request to the bridge
        record the `ambiguity` field the coordinator returns as `score`
    python tools/calibrate_ambiguity.py --corpus tests/calibration/ambiguity.jsonl

Until that is run against a live planner, `PROVISIONAL_DEFAULT` is what ships,
and it is labelled provisional everywhere it appears. Fabricating scores here
would produce a threshold with a calibration story attached, which is worse
than an honest guess: it would be a guess nobody re-examines.

The separation measure is Youden's J (TPR - FPR). It weighs a missed halt and
a spurious halt equally, which is the right default when the cost of each is
unknown, and it is one line to re-read -- an argument for it over anything
fancier that would need its own justification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# What overmind.toml ships. Named here so the config and the calibration story
# cannot drift apart silently; tests assert they agree.
PROVISIONAL_DEFAULT = 0.65

DEFAULT_CORPUS = Path("tests/calibration/ambiguity.jsonl")


@dataclass(frozen=True)
class Observation:
    """One goal, one human label, and the planner's score if it was recorded."""

    goal: str
    ambiguous: bool
    why: str
    score: float | None = None


class CorpusError(ValueError):
    """The corpus is malformed. Raised with a line number, because a silently
    skipped line would shift a threshold without anyone noticing."""


def parse(text: str, *, origin: str = "<corpus>") -> list[Observation]:
    rows: list[Observation] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CorpusError(f"{origin}:{line_no}: not valid json: {exc}") from exc
        if not isinstance(raw, dict):
            raise CorpusError(f"{origin}:{line_no}: expected an object, got {type(raw).__name__}")

        missing = sorted({"goal", "ambiguous", "why"} - set(raw))
        if missing:
            raise CorpusError(f"{origin}:{line_no}: missing {missing}")
        if not isinstance(raw["ambiguous"], bool):
            raise CorpusError(f"{origin}:{line_no}: 'ambiguous' must be true or false")

        score = raw.get("score")
        if score is not None and not isinstance(score, int | float):
            raise CorpusError(f"{origin}:{line_no}: 'score' must be a number or null")
        if isinstance(score, int | float) and not 0.0 <= float(score) <= 1.0:
            raise CorpusError(f"{origin}:{line_no}: 'score' {score} is outside [0, 1]")

        rows.append(
            Observation(
                goal=str(raw["goal"]),
                ambiguous=bool(raw["ambiguous"]),
                why=str(raw["why"]),
                score=None if score is None else float(score),
            )
        )
    return rows


def load(path: Path = DEFAULT_CORPUS) -> list[Observation]:
    return parse(path.read_text(encoding="utf-8"), origin=str(path))


@dataclass(frozen=True)
class Calibration:
    threshold: float
    true_positive_rate: float
    false_positive_rate: float
    youden_j: float
    scored: int
    total: int

    def report(self) -> str:
        return (
            f"threshold {self.threshold:.2f} from {self.scored}/{self.total} scored goal(s): "
            f"halts {self.true_positive_rate:.0%} of ambiguous goals and "
            f"{self.false_positive_rate:.0%} of clear ones (J={self.youden_j:.2f})"
        )


def calibrate(rows: list[Observation]) -> Calibration | None:
    """Pick the cut with the best separation, or None if nothing can be said.

    Returns None rather than a default when the corpus has no scores, or has
    only one label among the scored rows. A threshold derived from one-sided
    evidence is not a calibration, and returning the guess from here would
    launder it into a measurement.
    """
    scored = [r for r in rows if r.score is not None]
    positives = [r.score for r in scored if r.ambiguous and r.score is not None]
    negatives = [r.score for r in scored if not r.ambiguous and r.score is not None]
    if not positives or not negatives:
        return None

    best: Calibration | None = None
    for cut in sorted({round(score, 3) for score in positives + negatives}):
        tpr = sum(1 for s in positives if s >= cut) / len(positives)
        fpr = sum(1 for s in negatives if s >= cut) / len(negatives)
        j = round(tpr - fpr, 6)
        if best is None or j > best.youden_j:
            best = Calibration(
                threshold=cut,
                true_positive_rate=tpr,
                false_positive_rate=fpr,
                youden_j=j,
                scored=len(scored),
                total=len(rows),
            )
    return best
