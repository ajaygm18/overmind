"""Overmind: a meta-harness composition layer.

This package contains no orchestration engine, no agent loop, no memory store,
and no sandbox. Each of those is an upstream process:

  - planning        -> Open Multi-Agent, via bridge/planner.ts
  - execution       -> Omnigent CLI (bwrap/seatbelt sandboxed)
  - vector memory   -> Ruflo over MCP, four tools allowlisted

What lives here is the composition: the plan rewrite, vendor-diversity
routing, MAST-derived gates, and the receipt ledger.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
