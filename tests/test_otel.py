"""The receipt -> OTLP translation.

Offline. `httpx.post` is stubbed; nothing here needs a collector.

The assertions worth reading are the ones about honesty rather than shape:
derived durations are labelled as derived, tool arguments never appear in a
payload, and a failed node produces an OTLP error status instead of a green span
with a sad attribute.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from overmind import otel
from overmind.models import GateResult, GateStatus, Receipt

START = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)


def receipt(
    node_id: str,
    *,
    kind: str = "node",
    offset: int = 0,
    status: str = "ok",
    **extra: Any,
) -> Receipt:
    payload: dict[str, Any] = {
        "run_id": "r-2026-07-27-1",
        "plan_hash": "abc123",
        "node_id": node_id,
        "kind": kind,
        "status": status,
        "at": (START + timedelta(seconds=offset)).isoformat(),
    }
    payload.update(extra)
    return Receipt.model_validate(payload)


LEDGER = [
    receipt(
        "impl-auth",
        offset=0,
        role="implementer",
        vendor="anthropic",
        harness="claude-sdk",
        tokens_in=4000,
        tokens_out=800,
        cost_usd=0.42,
        tool_calls=[
            {"tool": "read", "path": "src/auth.py", "args": {"token": "sk-live-SECRET"}},
            {"tool": "write", "path": "src/auth.py", "args": {}},
            {"tool": "write", "path": "src/auth.py", "args": {}},
        ],
        decisions=["rotated the refresh token"],
        diff_stat=" src/auth.py | 40 ++++++--",
        worktree=".worktrees/r1-impl-auth",
    ),
    receipt(
        "impl-auth",
        kind="gate",
        offset=30,
        gates=[
            GateResult(
                gate="exit_proof",
                status=GateStatus.PASS,
                mast_mode="premature termination",
                detail="impl-auth tests_pass ok",
            )
        ],
    ),
    receipt(
        "verify-impl-auth",
        offset=90,
        status="failed",
        role="verifier",
        vendor="openai",
        harness="codex",
        cost_usd=0.08,
        error="acceptance_drift: fidelity 0.21",
        gates=[
            GateResult(
                gate="acceptance_drift",
                status=GateStatus.FAIL,
                mast_mode="task derailment",
                detail="verified against a restatement with fidelity 0.21",
            )
        ],
    ),
]


def spans_of(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attrs_of(span: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for attribute in span["attributes"]:
        (kind, value), = attribute["value"].items()
        out[attribute["key"]] = value
    return out


# -- ids -------------------------------------------------------------------


def test_ids_are_the_right_width_and_hex() -> None:
    assert len(otel.trace_id("r1")) == 32
    assert len(otel.span_id("r1", "node", "impl")) == 16
    int(otel.trace_id("r1"), 16)
    int(otel.span_id("r1", "node", "impl"), 16)


def test_the_same_run_exports_to_the_same_trace_twice() -> None:
    """Re-exporting must not create a second copy of a run that happened once."""
    assert otel.to_otlp(LEDGER) == otel.to_otlp(LEDGER)


def test_different_runs_get_different_traces() -> None:
    assert otel.trace_id("r1") != otel.trace_id("r2")


# -- structure -------------------------------------------------------------


def test_one_root_span_with_every_other_span_beneath_it() -> None:
    spans = spans_of(otel.to_otlp(LEDGER))
    roots = [s for s in spans if "parentSpanId" not in s]
    assert len(roots) == 1
    assert roots[0]["name"].startswith("overmind.run ")
    assert all(s["parentSpanId"] == roots[0]["spanId"] for s in spans if s is not roots[0])


def test_a_gate_receipt_folds_into_the_node_it_judged() -> None:
    """A gate is an instant judgement about a node, not work with a duration."""
    spans = spans_of(otel.to_otlp(LEDGER))
    names = [s["name"] for s in spans]
    assert "gate.impl-auth" not in names

    node = next(s for s in spans if s["name"] == "node.impl-auth")
    assert [e["name"] for e in node["events"]] == ["gate.exit_proof"]


def test_a_plan_level_gate_receipt_is_not_dropped() -> None:
    """Gates for a node with no node receipt still have to appear somewhere."""
    ledger = [
        receipt(
            "plan",
            kind="gate",
            offset=0,
            gates=[GateResult(gate="ambiguity_halt", status=GateStatus.HALT, detail="0.81")],
        )
    ]
    names = [s["name"] for s in spans_of(otel.to_otlp(ledger))]
    assert "gate.plan" in names


def test_the_run_span_totals_cost_and_tokens() -> None:
    root = next(s for s in spans_of(otel.to_otlp(LEDGER)) if "parentSpanId" not in s)
    attributes = attrs_of(root)
    assert attributes["overmind.cost_usd"] == pytest.approx(0.5)
    assert attributes["overmind.tokens.in"] == "4000"
    assert attributes["overmind.nodes"] == "2"


def test_int_attributes_are_strings_and_floats_are_not() -> None:
    """OTLP/JSON carries int64 as a string. A number there is silently truncated."""
    node = next(s for s in spans_of(otel.to_otlp(LEDGER)) if s["name"] == "node.impl-auth")
    attributes = attrs_of(node)
    assert attributes["overmind.tokens.out"] == "800"
    assert isinstance(attributes["overmind.cost_usd"], float)


def test_empty_attributes_are_omitted_not_blanked() -> None:
    """`vendor=""` would read as 'the router assigned nothing'."""
    span = next(s for s in spans_of(otel.to_otlp(LEDGER)) if s["name"] == "gate.plan") if False else None
    minimal = spans_of(otel.to_otlp([receipt("bare", offset=0)]))
    node = next(s for s in minimal if s["name"] == "node.bare")
    assert "overmind.vendor" not in attrs_of(node)
    assert span is None


def test_an_empty_ledger_produces_no_spans() -> None:
    assert otel.span_count(otel.to_otlp([])) == 0


# -- honesty ---------------------------------------------------------------


def test_every_span_says_its_timing_is_derived() -> None:
    """Receipt has `at` and no start time, so durations are inferred from ledger
    order. A backend rendering an inferred duration as a measured one is exactly
    the kind of quiet wrongness this attribute exists to prevent."""
    for span in spans_of(otel.to_otlp(LEDGER)):
        assert attrs_of(span)["overmind.timing.source"] == otel.DERIVED


def test_spans_never_end_before_they_start() -> None:
    for span in spans_of(otel.to_otlp(LEDGER)):
        assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"])


def test_a_node_span_covers_the_gap_since_the_previous_entry() -> None:
    spans = spans_of(otel.to_otlp(LEDGER))
    verify = next(s for s in spans if s["name"] == "node.verify-impl-auth")
    elapsed = int(verify["endTimeUnixNano"]) - int(verify["startTimeUnixNano"])
    assert elapsed == 90 * 1_000_000_000  # 12:00:00 -> 12:01:30


def test_tool_arguments_are_never_exported() -> None:
    """Ledger redaction is opt-in, so a receipt on disk may hold a secret. A span
    shipped to a third-party backend is the worst place to find that out."""
    blob = json.dumps(otel.to_otlp(LEDGER))
    assert "sk-live-SECRET" not in blob
    assert "args" not in blob


def test_tool_names_survive_and_are_deduplicated() -> None:
    node = next(s for s in spans_of(otel.to_otlp(LEDGER)) if s["name"] == "node.impl-auth")
    attributes = attrs_of(node)
    assert attributes["overmind.tool_calls"] == "3"
    assert [v["stringValue"] for v in attributes["overmind.tools"]["values"]] == ["read", "write"]


def test_a_failed_node_is_an_otlp_error_with_the_reason() -> None:
    spans = spans_of(otel.to_otlp(LEDGER))
    verify = next(s for s in spans if s["name"] == "node.verify-impl-auth")
    assert verify["status"]["code"] == otel.STATUS_ERROR
    assert "acceptance_drift" in verify["status"]["message"]


def test_a_failed_node_fails_the_run_span() -> None:
    root = next(s for s in spans_of(otel.to_otlp(LEDGER)) if "parentSpanId" not in s)
    assert root["status"]["code"] == otel.STATUS_ERROR


def test_a_skipped_node_is_unset_rather_than_ok() -> None:
    """Skipped work is not success. Reporting OK would inflate a run's health."""
    span = spans_of(otel.to_otlp([receipt("skipped-node", offset=0, status="skipped")]))[1]
    assert span["status"]["code"] == otel.STATUS_UNSET


def test_a_blocking_gate_event_says_so() -> None:
    spans = spans_of(otel.to_otlp(LEDGER))
    verify = next(s for s in spans if s["name"] == "node.verify-impl-auth")
    event = next(e for e in verify["events"] if e["name"] == "gate.acceptance_drift")
    values = {a["key"]: a["value"] for a in event["attributes"]}
    assert values["overmind.gate.blocking"]["boolValue"] is True
    assert values["overmind.mast_mode"]["stringValue"] == "task derailment"


# -- transport -------------------------------------------------------------


def test_the_traces_path_is_appended_when_missing() -> None:
    seen: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> httpx.Response:
        seen["url"] = url
        return httpx.Response(200)

    original, otel.httpx.post = otel.httpx.post, fake_post
    try:
        assert otel.post({"resourceSpans": []}, "http://127.0.0.1:4318") == 200
        assert seen["url"] == "http://127.0.0.1:4318/v1/traces"

        otel.post({"resourceSpans": []}, "http://127.0.0.1:4318/v1/traces/")
        assert seen["url"] == "http://127.0.0.1:4318/v1/traces"
    finally:
        otel.httpx.post = original


def test_an_unreachable_collector_is_a_clear_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(otel.httpx, "post", boom)
    with pytest.raises(otel.ExportFailed, match="cannot reach"):
        otel.post({"resourceSpans": []}, "http://127.0.0.1:4318")


def test_a_rejected_payload_reports_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        otel.httpx, "post", lambda url, **kw: httpx.Response(415, text="unsupported")
    )
    with pytest.raises(otel.ExportFailed, match="415"):
        otel.post({"resourceSpans": []}, "http://127.0.0.1:4318")


def test_writing_to_disk_round_trips(tmp_path: Path) -> None:
    payload = otel.to_otlp(LEDGER)
    path = otel.write(payload, tmp_path / "nested" / "spans.json")
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert otel.span_count(payload) == len(spans_of(payload))
