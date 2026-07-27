# MAST traces

One JSONL file per MAST category, plus the two beyond-MAST failures from
Cognition's *Don't Build Multi-Agents*. Each line is one trace and one
expectation:

```json
{
  "mode": "premature termination",
  "gate": "exit_proof",
  "expect": "blocking",
  "why": "the exit check never ran, so nothing proves the node finished",
  "driver": "exit_proof",
  "payload": { "node": { "...": "..." }, "exit_code": null }
}
```

- `mode` matches the first column of a table in `../../docs/MAST-GATES.md`,
  case-insensitively. A mode in the docs with no trace here fails the suite.
- `gate` is the function that must react, and is asserted against
  `GateResult.gate` so a fixture cannot quietly be graded by the wrong gate.
- `expect` is `blocking` (FAIL or HALT) or `clear` (PASS). Every mode needs at
  least one of each: a gate that blocks everything provides no coverage.
- `driver` names the wiring in `../test_mast_coverage.py`. Gate signatures
  differ, and encoding calling conventions in JSON would mean writing a small
  interpreter nobody can read.
- `mast_mode_checked` defaults to true, asserting the gate reports the same mode
  string as the docs. It is false for the three gates whose `mast_mode` text is
  not fixed by this repo's docs (`declared_scope`, `prove_disjoint`).

Everything runs offline. `MemoryConfig(enabled=False)` forces the n-gram
similarity path so the semantic gates are deterministic and need no Ruflo.
