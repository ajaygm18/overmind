"""Tests for the ambiguity threshold corpus and chooser.

The gate these support, `ambiguity_halt`, is the only one whose input is a
number a model made up about itself. That makes the threshold the whole gate,
and an untested threshold-picker would be a calibration story with no
calibration in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from overmind import calibration
from overmind.config import load as load_config

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "calibration" / "ambiguity.jsonl"


def obs(score: float | None, ambiguous: bool) -> calibration.Observation:
    return calibration.Observation(
        goal="g", ambiguous=ambiguous, why="fixture", score=score
    )


# -- the corpus -------------------------------------------------------------


def test_the_shipped_corpus_parses() -> None:
    rows = calibration.load(CORPUS)
    assert len(rows) >= 20


def test_the_corpus_has_both_labels_in_quantity() -> None:
    """A corpus that is 90% one label produces a threshold that looks excellent
    and predicts nothing."""
    rows = calibration.load(CORPUS)
    ambiguous = [r for r in rows if r.ambiguous]
    clear = [r for r in rows if not r.ambiguous]
    assert len(ambiguous) >= 8
    assert len(clear) >= 8


def test_every_label_carries_its_reason() -> None:
    """The reason is what makes a label reviewable by someone who disagrees."""
    for row in calibration.load(CORPUS):
        assert row.why.strip(), row.goal
        assert len(row.why.split()) >= 4, row.goal


def test_the_corpus_is_still_unscored_and_that_is_visible() -> None:
    """Fails the day someone records scores, which is the day LIMITATIONS and
    this test both need updating. That is the intended alarm, not a nuisance."""
    rows = calibration.load(CORPUS)
    assert all(r.score is None for r in rows)
    assert calibration.calibrate(rows) is None


# -- malformed input --------------------------------------------------------


def test_a_bad_line_names_its_line_number() -> None:
    text = '{"goal": "a", "ambiguous": true, "why": "reason enough here"}\nnot json\n'
    with pytest.raises(calibration.CorpusError, match=":2:"):
        calibration.parse(text)


def test_a_missing_field_is_refused_rather_than_defaulted() -> None:
    with pytest.raises(calibration.CorpusError, match="ambiguous"):
        calibration.parse('{"goal": "a", "why": "reason enough here"}')


def test_a_string_label_is_not_accepted_as_a_boolean() -> None:
    with pytest.raises(calibration.CorpusError, match="true or false"):
        calibration.parse('{"goal": "a", "ambiguous": "yes", "why": "reason enough here"}')


def test_a_score_outside_the_unit_interval_is_refused() -> None:
    with pytest.raises(calibration.CorpusError, match=r"\[0, 1\]"):
        calibration.parse(
            '{"goal": "a", "ambiguous": true, "why": "reason enough here", "score": 1.4}'
        )


def test_comments_and_blank_lines_are_ignored() -> None:
    text = (
        "// a note\n\n"
        '{"goal": "a", "ambiguous": true, "why": "reason enough here"}\n'
    )
    assert len(calibration.parse(text)) == 1


# -- the chooser ------------------------------------------------------------


def test_separable_scores_produce_a_perfect_cut() -> None:
    rows = [obs(0.9, True), obs(0.8, True), obs(0.2, False), obs(0.1, False)]
    result = calibration.calibrate(rows)
    assert result is not None
    assert result.youden_j == 1.0
    assert 0.2 < result.threshold <= 0.8


def test_overlapping_scores_produce_the_best_available_compromise() -> None:
    """Real self-reports overlap. The chooser must still return the cut that
    separates best, not refuse because separation is imperfect."""
    rows = [
        obs(0.9, True),
        obs(0.7, True),
        obs(0.4, True),
        obs(0.5, False),
        obs(0.2, False),
        obs(0.1, False),
    ]
    result = calibration.calibrate(rows)
    assert result is not None
    assert result.threshold == 0.7
    assert result.true_positive_rate == pytest.approx(2 / 3)
    assert result.false_positive_rate == 0.0


def test_one_sided_evidence_yields_no_threshold() -> None:
    """Scores for ambiguous goals only cannot say where the boundary is, and
    returning the shipped default here would launder a guess into a
    measurement."""
    assert calibration.calibrate([obs(0.9, True), obs(0.8, True)]) is None
    assert calibration.calibrate([obs(0.1, False), obs(0.2, False)]) is None


def test_unscored_rows_are_counted_but_not_used() -> None:
    rows = [obs(0.9, True), obs(0.1, False), obs(None, True), obs(None, False)]
    result = calibration.calibrate(rows)
    assert result is not None
    assert result.scored == 2
    assert result.total == 4


def test_the_report_states_both_error_rates() -> None:
    """A threshold quoted without its false-positive rate is the number that
    gets the gate switched off in month two."""
    result = calibration.calibrate([obs(0.9, True), obs(0.1, False)])
    assert result is not None
    text = result.report()
    assert "ambiguous" in text
    assert "clear" in text


# -- agreement with what ships ---------------------------------------------


def test_the_shipped_config_matches_the_documented_provisional_value() -> None:
    """If overmind.toml drifts from calibration.PROVISIONAL_DEFAULT, the docs
    describing where 0.65 came from start describing a number nobody uses."""
    cfg = load_config(ROOT / "overmind.toml")
    assert cfg.run.ambiguity_threshold == calibration.PROVISIONAL_DEFAULT
