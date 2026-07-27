"""Receipts -> OpenTelemetry spans.

Receipts are the source of truth for gates, cost, and replay (ADR-005), and they
are a poor way to *look* at a run. A ledger of 40 JSONL lines does not show the
shape of the DAG, which level was slow, or where the budget went. OTLP is what
every tracing backend already ingests, so the useful move is a translation, not
another bespoke viewer.

No dependency on the opentelemetry SDK. OTLP/JSON is a documented wire shape and
`httpx` is already required; pulling in a tracing SDK -- tracer provider, span
processor, exporter, context propagation -- to emit one POST would add a
substantial dependency to serialize data that is already complete and at rest.

Two things this deliberately does not pretend:

1. **Durations are derived.** A `Receipt` has `at`, the moment the entry was
   appended, and no start timestamp. A node's span therefore runs from the
   previous ledger entry to its own, which approximates that node's wall time
   and is not a measurement of it. Every such span carries
   `overmind.timing.source="derived-from-ledger-order"`. The real fix is a
   `started_at` field on `Receipt`; until then the attribute is the honest
   signal, and a `0` duration would be a lie in a different font.

2. **Tool arguments never leave.** Ledger redaction is opt-in
   (`ReceiptConfig.redact_tool_args`), so a receipt on disk may contain secrets
   an operator chose not to scrub locally. A span shipped to a third-party
   backend is the worst place for that to surface, so only tool names and counts
   are exported.

Attributes use the `overmind.` prefix throughout. OMA reserves `oma.` for its
own emitter and mirroring its keys would make two tools' spans
indistinguishable in one backend.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .models import Receipt

SCHEMA_URL = "https://opentelemetry.io/schemas/1.27.0"
SCOPE_NAME = "overmind"
SCOPE_VERSION = "0.1.0"

# OTLP span kinds. Every span here is INTERNAL: a node is orchestration work,
# not an inbound request or an outbound client call.
SPAN_KIND_INTERNAL = 1

# OTLP status codes.
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2

DERIVED = "derived-from-ledger-order"


class ExportFailed(Exception):
    """The collector was unreachable, or refused the payload."""


def _hex(seed: str, length: int) -> str:
    """Deterministic id from a seed.

    Deterministic on purpose: re-exporting a run must land on the same trace
    rather than creating a second copy of a run that only happened once.
    """
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def trace_id(run_id: str) -> str:
    return _hex(f"overmind/run/{run_id}", 32)


def span_id(run_id: str, *parts: str) -> str:
    return _hex("/".join(("overmind/span", run_id, *parts)), 16)


def nanos(when: datetime) -> int:
    return int(when.timestamp() * 1_000_000_000)


def _value(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, bool):
        return {"boolValue": raw}
    if isinstance(raw, int):
        return {"intValue": str(raw)}  # OTLP/JSON carries int64 as a string
    if isinstance(raw, float):
        return {"doubleValue": raw}
    if isinstance(raw, str):
        return {"stringValue": raw} if raw else None
    if isinstance(raw, list):
        rendered = [v for v in (_value(item) for item in raw) if v]
        return {"arrayValue": {"values": rendered}} if rendered else None
    return None


def attributes(pairs: dict[str, object]) -> list[dict[str, Any]]:
    """Drop empty values rather than emitting empty strings.

    A backend showing `overmind.vendor=""` invites the reader to conclude the
    router assigned nothing, when the truth is that this receipt kind has no
    vendor.
    """
    out = []
    for key, raw in pairs.items():
        if raw is None:
            continue
        value = _value(raw)
        if value is not None:
            out.append({"key": key, "value": value})
    return out


def tool_names(receipt: Receipt) -> list[str]:
    """Names only. Arguments are never exported; see the module docstring."""
    seen: list[str] = []
    for call in receipt.tool_calls:
        name = str(call.get("tool") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _status(receipt: Receipt) -> dict[str, Any]:
    if receipt.status in ("failed", "halted"):
        return {"code": STATUS_ERROR, "message": receipt.error or receipt.status}
    if receipt.status == "skipped":
        return {"code": STATUS_UNSET}
    return {"code": STATUS_OK}


def gate_events(receipt: Receipt) -> list[dict[str, Any]]:
    """Gate results become span events.

    Events rather than child spans: a gate is an instant judgement about the
    node, with no duration of its own, and child spans would imply otherwise.
    """
    return [
        {
            "timeUnixNano": str(nanos(receipt.at)),
            "name": f"gate.{result.gate}",
            "attributes": attributes(
                {
                    "overmind.gate": result.gate,
                    "overmind.gate.status": str(result.status),
                    "overmind.gate.blocking": result.blocking,
                    "overmind.mast_mode": result.mast_mode,
                    "overmind.gate.detail": result.detail[:1024],
                }
            ),
        }
        for result in receipt.gates
    ]


def _ordered(receipts: list[Receipt]) -> list[Receipt]:
    """Ledger order, stabilised by timestamp.

    `sorted` is stable, so entries appended in the same second keep the order
    they were written in, which is the order they happened in.
    """
    return sorted(receipts, key=lambda r: r.at)


def build_spans(receipts: list[Receipt]) -> list[dict[str, Any]]:
    """One root span for the run, one child per node, gates as events.

    Gate receipts are folded into the span of the node they judged. A gate
    receipt whose node has no span of its own -- a plan-level gate, which is
    normal -- becomes its own span under the run so it is not silently dropped.
    """
    ordered = _ordered(receipts)
    if not ordered:
        return []

    run_id = ordered[0].run_id
    root_id = span_id(run_id, "run")
    tid = trace_id(run_id)
    started, ended = nanos(ordered[0].at), nanos(ordered[-1].at)

    nodes = [r for r in ordered if r.kind == "node"]
    node_ids = {r.node_id for r in nodes}

    spans: list[dict[str, Any]] = [
        {
            "traceId": tid,
            "spanId": root_id,
            "name": f"overmind.run {run_id}",
            "kind": SPAN_KIND_INTERNAL,
            "startTimeUnixNano": str(started),
            "endTimeUnixNano": str(max(ended, started)),
            "attributes": attributes(
                {
                    "overmind.run_id": run_id,
                    "overmind.plan_hash": ordered[0].plan_hash,
                    "overmind.nodes": len(nodes),
                    "overmind.cost_usd": round(sum(r.cost_usd for r in ordered), 4),
                    "overmind.tokens.in": sum(r.tokens_in for r in ordered),
                    "overmind.tokens.out": sum(r.tokens_out for r in ordered),
                    "overmind.failed_nodes": [
                        r.node_id for r in nodes if r.status == "failed"
                    ],
                    "overmind.timing.source": DERIVED,
                }
            ),
            "status": {"code": STATUS_ERROR if any(r.status == "failed" for r in ordered) else STATUS_OK},
        }
    ]

    # Gate receipts, keyed by the node they judged, so they can ride along.
    riders: dict[str, list[Receipt]] = {}
    for receipt in ordered:
        if receipt.kind == "gate" and receipt.node_id in node_ids:
            riders.setdefault(receipt.node_id, []).append(receipt)

    previous = started
    for index, receipt in enumerate(ordered):
        if receipt.kind == "gate" and receipt.node_id in node_ids:
            continue  # folded into the node span below

        end = nanos(receipt.at)
        events = gate_events(receipt)
        for rider in riders.get(receipt.node_id, []) if receipt.kind == "node" else []:
            events.extend(gate_events(rider))

        spans.append(
            {
                "traceId": tid,
                "spanId": span_id(run_id, receipt.kind, receipt.node_id, str(index)),
                "parentSpanId": root_id,
                "name": f"{receipt.kind}.{receipt.node_id}",
                "kind": SPAN_KIND_INTERNAL,
                "startTimeUnixNano": str(min(previous, end)),
                "endTimeUnixNano": str(end),
                "attributes": attributes(
                    {
                        "overmind.run_id": receipt.run_id,
                        "overmind.plan_hash": receipt.plan_hash,
                        "overmind.node_id": receipt.node_id,
                        "overmind.receipt.kind": receipt.kind,
                        "overmind.role": str(receipt.role) if receipt.role else None,
                        "overmind.vendor": receipt.vendor,
                        "overmind.harness": receipt.harness,
                        "overmind.tokens.in": receipt.tokens_in,
                        "overmind.tokens.out": receipt.tokens_out,
                        "overmind.cost_usd": receipt.cost_usd,
                        "overmind.tool_calls": len(receipt.tool_calls),
                        "overmind.tools": tool_names(receipt),
                        "overmind.decisions": len(receipt.decisions),
                        "overmind.diff_stat": receipt.diff_stat,
                        "overmind.worktree": receipt.worktree,
                        "overmind.status": receipt.status,
                        "overmind.timing.source": DERIVED,
                    }
                ),
                "events": events,
                "status": _status(receipt),
            }
        )
        previous = end

    return spans


def to_otlp(receipts: list[Receipt], service_name: str = "overmind") -> dict[str, Any]:
    """A complete OTLP/JSON ExportTraceServiceRequest."""
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": attributes(
                        {
                            "service.name": service_name,
                            "service.version": SCOPE_VERSION,
                            "telemetry.sdk.name": SCOPE_NAME,
                            "telemetry.sdk.language": "python",
                        }
                    )
                },
                "scopeSpans": [
                    {
                        "scope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
                        "spans": build_spans(receipts),
                        "schemaUrl": SCHEMA_URL,
                    }
                ],
            }
        ]
    }


def write(payload: dict[str, Any], path: Path) -> Path:
    """Write the payload to disk. Useful without a collector, and in CI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def post(payload: dict[str, Any], endpoint: str, timeout: float = 30.0) -> int:
    """POST to an OTLP/HTTP collector. Returns the status code.

    The endpoint is the traces path (`/v1/traces`), appended when the caller
    passes a bare collector URL, because getting that wrong produces a 404 that
    reads like a network problem.
    """
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url = f"{url}/v1/traces"

    try:
        response = httpx.post(url, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ExportFailed(f"cannot reach the OTLP collector at {url} ({exc})") from exc

    if response.status_code >= 400:
        raise ExportFailed(
            f"collector rejected the export: {response.status_code} {response.text[:300]}"
        )
    return response.status_code


def span_count(payload: dict[str, Any]) -> int:
    """How many spans a payload carries, for reporting after an export."""
    return sum(
        len(scope.get("spans", []))
        for resource in payload.get("resourceSpans", [])
        for scope in resource.get("scopeSpans", [])
    )
