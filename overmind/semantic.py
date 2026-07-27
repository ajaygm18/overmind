"""Semantic repetition detection.

The original `loop_detect` gate hashed tool calls and flagged exact repeats.
Documented as a known weakness, and it is a bad one: agents rarely loop by
issuing byte-identical calls. They loop by rephrasing. `grep "device_code"`,
then `grep "device code"`, then `grep "device-code"` is a stuck agent burning
budget, and exact-match detection sees three distinct productive actions.

This module compares meaning instead of bytes. Embeddings come from Ruflo's
`embeddings_generate`, which is already one of the four allowlisted memory
tools (ADR-002) and is one of the ~10 tools the v3.5.51 audit found actually
execute -- so this uses upstream for what upstream demonstrably does.

When Ruflo is unavailable there is a pure-Python fallback over character
n-grams. It is weaker, and it is deliberately weaker in the safe direction:
surface-similar rephrasings still get caught, and CI runs the whole gate with
no model, no network, and no upstream process.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass

from .config import MemoryConfig
from .memory import MemoryUnavailable, RufloMemory
from .models import GateResult, GateStatus, Receipt

Vector = list[float]


def describe(call: dict[str, object]) -> str:
    """Flatten one tool call into comparable text.

    Argument values carry the intent -- the query string, the path -- so they
    are included. Keys are not: two calls differing only in argument order are
    the same action.
    """
    tool = str(call.get("tool") or "")
    path = str(call.get("path") or "")
    args = call.get("args")
    if isinstance(args, dict):
        rendered = " ".join(str(v) for _, v in sorted(args.items()))
    elif args is None:
        rendered = ""
    else:
        rendered = str(args)
    return " ".join(part for part in (tool, path, rendered) if part).strip().lower()


def _ngrams(text: str, n: int = 4) -> Counter[str]:
    squeezed = " ".join(text.split())
    if len(squeezed) <= n:
        return Counter([squeezed] if squeezed else [])
    return Counter(squeezed[i : i + n] for i in range(len(squeezed) - n + 1))


def cosine(a: Vector, b: Vector) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


def ngram_similarity(left: str, right: str) -> float:
    """Offline cosine over character n-gram counts."""
    a, b = _ngrams(left), _ngrams(right)
    if not a or not b:
        return 1.0 if left.strip() == right.strip() else 0.0
    shared = set(a) & set(b)
    dot = sum(a[g] * b[g] for g in shared)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


def _parse_vectors(raw: object, expected: int) -> list[Vector] | None:
    """Pull embedding vectors out of a tool response without assuming a shape.

    Ruflo ships constantly (1,488 releases). A changed response shape must cost
    this gate its precision, not the run -- so an unrecognised payload returns
    None and the caller falls back.
    """
    payload = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None

    if isinstance(payload, dict):
        for key in ("embeddings", "vectors", "data", "result"):
            if key in payload:
                payload = payload[key]
                break

    if not isinstance(payload, list) or len(payload) != expected:
        return None

    vectors: list[Vector] = []
    for item in payload:
        vec = item.get("embedding", item.get("vector")) if isinstance(item, dict) else item
        if not isinstance(vec, list) or not all(isinstance(x, int | float) for x in vec):
            return None
        vectors.append([float(x) for x in vec])
    return vectors


@dataclass
class Similarity:
    """A similarity function plus how it was obtained, so reports stay honest."""

    source: str

    def __post_init__(self) -> None:
        self._vectors: list[Vector] | None = None

    @classmethod
    def build(cls, texts: list[str], cfg: MemoryConfig | None) -> Similarity:
        sim = cls(source="ngram")
        if not cfg or not cfg.enabled or len(texts) < 2:
            return sim
        try:
            with RufloMemory(cfg) as mem:
                raw = mem.call("embeddings_generate", {"texts": texts})
            vectors = _parse_vectors(raw, len(texts))
        except (MemoryUnavailable, OSError, ValueError, TypeError, KeyError):
            # Degrade to the offline measure. A memory outage must never be
            # able to fail a run that is otherwise healthy.
            vectors = None
        if vectors:
            sim.source = "ruflo-embeddings"
            sim._vectors = vectors
        return sim

    def between(self, i: int, j: int, texts: list[str]) -> float:
        if self._vectors is not None:
            return cosine(self._vectors[i], self._vectors[j])
        return ngram_similarity(texts[i], texts[j])


@dataclass
class LoopFinding:
    start: int
    length: int
    similarity: float
    sample: str


def find_loop(
    texts: list[str],
    *,
    threshold: float = 0.92,
    window: int = 3,
    cfg: MemoryConfig | None = None,
) -> tuple[LoopFinding | None, str]:
    """Find `window` consecutive near-identical actions.

    Consecutive, not merely present: revisiting a file later in a task is
    normal work, while doing near-the-same thing three times in a row is a
    stuck agent. Requiring adjacency is what keeps this from flagging honest
    iteration.
    """
    if len(texts) < window:
        return None, "ngram"

    sim = Similarity.build(texts, cfg)

    for start in range(len(texts) - window + 1):
        scores = [
            sim.between(start + offset, start + offset + 1, texts)
            for offset in range(window - 1)
        ]
        if all(score >= threshold for score in scores):
            return (
                LoopFinding(
                    start=start,
                    length=window,
                    similarity=round(min(scores), 3),
                    sample=texts[start][:120],
                ),
                sim.source,
            )
    return None, sim.source


def loop_detect_semantic(
    receipt: Receipt,
    *,
    threshold: float = 0.92,
    window: int = 3,
    cfg: MemoryConfig | None = None,
) -> GateResult:
    """Gate: near-duplicate consecutive tool calls (MAST: step repetition)."""
    texts = [describe(call) for call in receipt.tool_calls]
    texts = [t for t in texts if t]
    finding, source = find_loop(texts, threshold=threshold, window=window, cfg=cfg)

    if finding is None:
        return GateResult(
            gate="loop_detect_semantic",
            status=GateStatus.PASS,
            detail=f"{len(texts)} tool call(s), no repeated run (via {source})",
        )

    return GateResult(
        gate="loop_detect_semantic",
        status=GateStatus.FAIL,
        mast_mode="step repetition",
        detail=(
            f"{receipt.node_id} repeated {finding.length} near-identical actions from index "
            f"{finding.start} (similarity {finding.similarity}, via {source}): "
            f"{finding.sample!r}"
        ),
    )


def restatement_fidelity(original: str, restated: str, cfg: MemoryConfig | None = None) -> float:
    """How faithfully a verifier restated the acceptance criterion.

    The original `acceptance_drift` gate used word overlap, which punishes a
    faithful paraphrase and rewards one that parrots the words while checking
    something else. Similarity over meaning is the right measure here.
    """
    texts = [original.strip().lower(), restated.strip().lower()]
    if not texts[0] or not texts[1]:
        return 0.0
    sim = Similarity.build(texts, cfg)
    return round(sim.between(0, 1, texts), 3)
