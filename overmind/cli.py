"""overmind CLI. Thin: it wires the modules together and prints."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from . import config as config_mod
from . import (
    executor,
    gates,
    introspect,
    linearity,
    otel,
    planner,
    receipts,
    router,
    semantic,
)
from . import integrate as integrate_mod
from . import resume as resume_mod
from .memory import MemoryUnavailable, RufloMemory
from .models import GateStatus, Plan

app = typer.Typer(add_completion=False, help="Compose Omnigent, Open Multi-Agent, and Ruflo.")
console = Console()


def _load(config_path: Path) -> config_mod.Config:
    try:
        return config_mod.load(config_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise typer.Exit(2) from exc


def _build_plan(goal: str, cfg: config_mod.Config, wide: bool) -> tuple[Plan, float, str]:
    """Recall prior decisions, ask OMA to plan, then rewrite the plan."""
    prior: list[str] = []
    try:
        with RufloMemory(cfg.memory) as mem:
            prior = mem.recall(goal)
    except MemoryUnavailable as exc:
        console.print(f"[yellow]memory unavailable, planning without recall:[/yellow] {exc}")

    if prior:
        console.print(f"[dim]recalled {len(prior)} prior decision(s) from memory[/dim]")

    try:
        raw, ambiguity = planner.plan(goal, cfg, prior)
    except planner.PlanInvalid as exc:
        # Ordered before PlanRejected, which it subclasses. The bridge already
        # retried once with these errors fed back; retrying here would spend
        # again to reach the same 422.
        console.print("[red]the coordinator could not produce a valid plan.[/red]")
        console.print(str(exc))
        raise typer.Exit(1) from exc
    except planner.PlannerUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    except planner.PlanRejected as exc:
        console.print(f"[red]plan rejected:[/red] {exc}")
        raise typer.Exit(1) from exc

    report = linearity.rewrite(raw, cfg.run.max_parallel, wide=wide)
    router.route(raw, cfg)
    router.distribute_budget(raw, cfg.run.budget_usd)
    return raw, ambiguity, report.summary()


def _render(plan: Plan, ambiguity: float, summary: str) -> None:
    console.print(f"\n[bold]goal:[/bold] {plan.goal}")
    console.print(f"[bold]plan:[/bold] {plan.content_hash()}  [dim]{summary}[/dim]")
    console.print(f"[bold]ambiguity:[/bold] {ambiguity:.2f}\n")

    table = Table(show_lines=False)
    for col in ("lvl", "node", "role", "vendor", "writes", "exit", "$"):
        table.add_column(col)
    for depth, level in enumerate(plan.levels):
        for node_id in level:
            n = plan.node(node_id)
            table.add_row(
                str(depth),
                f"{n.id}{' *' if n.synthesized else ''}",
                str(n.role),
                n.vendor or "-",
                ", ".join(n.writes) or "-",
                str(n.exit_check.kind),
                f"{n.budget_usd:.2f}" if n.budget_usd else "-",
            )
    console.print(table)
    console.print("[dim]* inserted by the rewriter, not the planner[/dim]")


def _dry_run(goal: str, cfg: config_mod.Config) -> None:
    """Print the exact POST body, then stop. No model call, nothing spent.

    The recalled decisions are the interesting part: they are injected as
    constraints, so a stale or irrelevant recall is a common reason a plan comes
    back wrong, and there was previously no way to look at them without paying
    for a planning call.
    """
    prior = _recall(cfg, goal)
    body = planner.payload(goal, cfg, prior)

    console.print(f"[bold]POST[/bold] {cfg.bridge.url.rstrip('/')}/plan")
    console.print(f"[dim]timeout {cfg.bridge.timeout_s}s, max_parallel_hint {cfg.run.max_parallel}[/dim]\n")
    console.print(json.dumps(body, indent=2))

    console.print(
        f"\n[dim]{len(prior)} prior decision(s) would be injected as constraints[/dim]"
        if prior
        else "\n[dim]no prior decisions recalled; this plan starts cold[/dim]"
    )

    if planner.health(cfg):
        console.print(f"[green]bridge is up at {cfg.bridge.url}[/green]")
        return
    console.print(f"[red]bridge is not reachable at {cfg.bridge.url}[/red] (run `make bridge`)")
    raise typer.Exit(2)


def _report_gates(results: list[gates.GateResult]) -> bool:
    """Print gate results. Returns True if execution may proceed."""
    blocking = [g for g in results if g.blocking]
    if not blocking:
        console.print(f"[green]all {len(results)} plan gates passed[/green]")
        return True
    for g in blocking:
        colour = "yellow" if g.status is GateStatus.HALT else "red"
        console.print(f"[{colour}]{g.status.upper()}[/{colour}] {g.gate} ({g.mast_mode}): {g.detail}")
    return False


@app.command()
def plan(
    goal: str,
    config: Path = typer.Option(Path("overmind.toml"), "--config"),
    wide: bool = typer.Option(False, "--wide", help="allow fan-out beyond max_parallel"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="show the request the bridge would receive, then stop"
    ),
) -> None:
    """Plan only. No execution, no spend."""
    cfg = _load(config)

    if dry_run:
        _dry_run(goal, cfg)
        return

    p, ambiguity, summary = _build_plan(goal, cfg, wide)
    _render(p, ambiguity, summary)
    _report_gates(gates.plan_gates(p, ambiguity, cfg.run.ambiguity_threshold))


@app.command()
def run(
    goal: str,
    config: Path = typer.Option(Path("overmind.toml"), "--config"),
    budget: float | None = typer.Option(None, "--budget", help="override budget_usd"),
    wide: bool = typer.Option(False, "--wide"),
    yes: bool = typer.Option(False, "--yes", help="skip plan approval"),
) -> None:
    """Plan, gate, approve, execute."""
    cfg = _load(config)
    if budget is not None:
        cfg.run.budget_usd = budget

    if missing := executor.preflight():
        console.print(f"[red]missing required tools:[/red] {missing}. run `make setup`.")
        raise typer.Exit(2)

    p, ambiguity, summary = _build_plan(goal, cfg, wide)
    _render(p, ambiguity, summary)

    plan_hash = p.content_hash()
    run_id = executor.new_run_id()
    ledger = receipts.Ledger(cfg.receipts, run_id)

    results = gates.plan_gates(p, ambiguity, cfg.run.ambiguity_threshold)
    ledger.append_gates(plan_hash, "__plan__", results)
    if not _report_gates(results):
        console.print("[red]plan gates blocked execution. nothing was spent.[/red]")
        raise typer.Exit(1)

    # Snapshot before spending. Receipts record what happened; only the
    # snapshot records what was supposed to happen, and resume needs both.
    resume_mod.save_plan(p, run_id)

    if not yes and not typer.confirm(f"execute {len(p.nodes)} nodes at up to ${cfg.run.budget_usd:.2f}?"):
        raise typer.Exit(0)

    console.print(f"[bold]run {run_id}[/bold] plan {plan_hash}")
    halted = _execute(p, cfg, run_id, plan_hash, ledger)

    summary_obj = receipts.summarize(ledger.read())
    console.print(json.dumps(summary_obj, indent=2))
    if halted:
        console.print(f"[yellow]halted. resume with:[/yellow] overmind resume {run_id}")
        raise typer.Exit(1)
    console.print(f"[green]run {run_id} complete[/green]")


def _execute(
    p: Plan,
    cfg: config_mod.Config,
    run_id: str,
    plan_hash: str,
    ledger: receipts.Ledger,
    levels: list[list[str]] | None = None,
) -> bool:
    """Execute levels in order. Returns True if the run halted.

    Budget exhaustion is a halt, not a failure: the checkpoint stays clean and
    resumable rather than dying mid-diff.
    """
    router.audit(p)  # re-check vendor diversity right before spending
    audits: dict[str, introspect.WriteAudit] = {}

    for depth, level in enumerate(levels if levels is not None else p.levels):
        for node_id in level:
            node = p.node(node_id)

            if ledger.spent() >= cfg.run.budget_usd:
                console.print(
                    f"[yellow]budget ${cfg.run.budget_usd:.2f} exhausted at {node_id}[/yellow]"
                )
                return True

            console.print(f"[dim]L{depth}[/dim] {node_id} -> {node.vendor}/{node.harness}")
            outcome = executor.execute(node, cfg, run_id)
            receipt = executor.to_receipt(run_id, plan_hash, node, outcome)

            # Writes are measured from git here, not taken from the plan.
            audit = introspect.audit_writes(node, outcome.worktree)
            audits[node_id] = audit

            node_gates = [
                gates.exit_proof(node, outcome.exit_code),
                gates.decision_surface(receipt),
                gates.action_trace(node, receipt),
                # Measured scope, so an under-reporting harness cannot hide it.
                introspect.declared_scope(audit),
                # Subsumes the old exact-match loop_detect: byte-identical
                # calls score 1.0, and rephrased repeats now score too.
                semantic.loop_detect_semantic(receipt, cfg=cfg.memory),
            ]
            receipt.gates = node_gates
            ledger.append(receipt)

            for decision in outcome.decisions:
                _remember(cfg, run_id, node_id, decision)

            if blocking := [g for g in node_gates if g.blocking]:
                for g in blocking:
                    console.print(f"  [red]{g.gate}[/red] ({g.mast_mode}): {g.detail}")
                    _remember_failure(cfg, run_id, node_id, g.mast_mode or g.gate, g.detail)
                console.print(f"[red]{node_id} failed its gates. worktree retained.[/red]")
                return True

            if receipt.status == "failed":
                console.print(f"  [red]{outcome.error or 'node failed'}[/red]")
                return True

    # The rewriter authorised concurrency from declared file sets. Now that the
    # real ones are known, re-prove it.
    disjoint = introspect.prove_disjoint(p, audits)
    ledger.append_gates(plan_hash, "__disjoint__", disjoint)
    if not _report_gates(disjoint):
        for g in disjoint:
            if g.blocking:
                _remember_failure(cfg, run_id, "__disjoint__", g.mast_mode or g.gate, g.detail)
        return True

    # Under-declaration is a planning defect. Teach it, so the next plan for
    # this repository declares the path and the rewriter can trust it.
    for node_id, audit in audits.items():
        if (note := introspect.lesson(audit)) is not None:
            _remember(cfg, run_id, node_id, note)

    return _integrate(p, cfg, run_id, plan_hash, ledger)


def _integrate(
    p: Plan,
    cfg: config_mod.Config,
    run_id: str,
    plan_hash: str,
    ledger: receipts.Ledger,
) -> bool:
    """Merge the run's worktrees into the base branch. True if it halted.

    Without this the parallelism was decorative: nodes finished, gates passed,
    and the work stayed on branches nobody merged.
    """
    try:
        report = integrate_mod.integrate(p, run_id, ledger.read())
    except integrate_mod.IntegrationError as exc:
        console.print(f"[red]integration refused:[/red] {exc}")
        return True

    result = integrate_mod.clean_merge(report)
    ledger.append_gates(plan_hash, "__integrate__", [result])

    if result.blocking:
        console.print(f"[red]{result.gate}[/red] ({result.mast_mode}): {result.detail}")
        _remember_failure(
            cfg, run_id, "__integrate__", result.mast_mode or result.gate, result.detail
        )
        return True

    console.print(f"[green]{report.summary()}[/green]")
    return False


def _remember(cfg: config_mod.Config, run_id: str, node_id: str, decision: str) -> None:
    try:
        with RufloMemory(cfg.memory) as mem:
            mem.record_decision(run_id, node_id, decision)
    except MemoryUnavailable:
        pass  # memory is an optimisation, never a dependency of correctness


def _remember_failure(
    cfg: config_mod.Config, run_id: str, node_id: str, mode: str, detail: str
) -> None:
    try:
        with RufloMemory(cfg.memory) as mem:
            mem.record_failure(run_id, node_id, mode, detail)
    except MemoryUnavailable:
        pass


def _recall(cfg: config_mod.Config, query: str) -> list[str]:
    try:
        with RufloMemory(cfg.memory) as mem:
            return mem.recall(query)
    except MemoryUnavailable:
        return []


@app.command()
def resume(
    run_id: str,
    config: Path = typer.Option(Path("overmind.toml"), "--config"),
    budget: float | None = typer.Option(None, "--budget", help="extend the original budget"),
    yes: bool = typer.Option(False, "--yes"),
) -> None:
    """Continue a halted run from its last good checkpoint."""
    cfg = _load(config)
    if missing := executor.preflight():
        console.print(f"[red]missing required tools:[/red] {missing}")
        raise typer.Exit(2)

    try:
        point = resume_mod.plan_resume(run_id, cfg.receipts)
    except (resume_mod.ResumeError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    console.print(f"[bold]resume {run_id}[/bold] plan {point.plan_hash}")
    console.print(point.summary())

    if point.complete:
        console.print("[green]nothing left to do[/green]")
        raise typer.Exit(0)

    # Amnesia is what makes resume dangerous, not bookkeeping. Re-seed the
    # earlier half's decisions and confirm they can actually be read back;
    # a resumed agent that cannot see them will re-decide them differently.
    if point.prior_decisions and cfg.memory.enabled:
        for decision in point.prior_decisions:
            _remember(cfg, run_id, "__resume__", decision)
        carried = _recall(cfg, point.plan.goal)
        continuity = gates.checkpoint_continuity(point.prior_decisions, carried)
        if continuity.blocking:
            console.print(f"[red]{continuity.gate}[/red]: {continuity.detail}")
            console.print("[red]refusing to resume without the prior context.[/red]")
            raise typer.Exit(1)
        console.print(f"[dim]carried {len(point.prior_decisions)} prior decision(s)[/dim]")
    elif point.prior_decisions:
        console.print(
            f"[yellow]memory disabled: {len(point.prior_decisions)} prior decision(s) "
            "cannot be carried forward. the resumed nodes may re-decide them.[/yellow]"
        )

    if budget is not None:
        cfg.run.budget_usd = budget
    remaining = point.budget_left(cfg.run.budget_usd)
    console.print(f"[bold]budget left:[/bold] ${remaining:.2f}")
    if remaining <= 0:
        console.print("[red]original budget is spent. pass --budget to extend it.[/red]")
        raise typer.Exit(1)

    if not yes and not typer.confirm(f"resume {len(point.remaining)} node(s)?"):
        raise typer.Exit(0)

    ledger = receipts.Ledger(cfg.receipts, run_id)
    router.route(point.plan, cfg)
    router.distribute_budget(point.plan, cfg.run.budget_usd)

    halted = _execute(
        point.plan,
        cfg,
        run_id,
        point.plan_hash,
        ledger,
        levels=point.remaining_levels,
    )

    console.print(json.dumps(receipts.summarize(ledger.read()), indent=2))
    if halted:
        console.print(f"[yellow]halted again. inspect with:[/yellow] overmind replay {run_id}")
        raise typer.Exit(1)
    console.print(f"[green]run {run_id} complete[/green]")


@app.command()
def replay(
    run_id: str,
    config: Path = typer.Option(Path("overmind.toml"), "--config"),
) -> None:
    """Reconstruct a run from its receipts. No model calls, no spend."""
    cfg = _load(config)
    try:
        path = receipts.find(cfg.receipts, run_id)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    entries = list(receipts.iter_receipts(path))
    table = Table(title=f"run {run_id}")
    for col in ("node", "vendor", "status", "$", "gates", "decisions"):
        table.add_column(col)
    for r in entries:
        failed = [g.gate for g in r.gates if g.blocking]
        table.add_row(
            r.node_id,
            r.vendor or "-",
            r.status,
            f"{r.cost_usd:.4f}",
            ",".join(failed) or "ok",
            str(len(r.decisions)),
        )
    console.print(table)
    console.print(json.dumps(receipts.summarize(entries), indent=2))


@app.command()
def export(
    run_id: str,
    config: Path = typer.Option(Path("overmind.toml"), "--config"),
    otlp: str | None = typer.Option(
        None, "--otlp", help="OTLP/HTTP collector URL, e.g. http://127.0.0.1:4318"
    ),
    out: Path | None = typer.Option(None, "--out", help="write the payload to this file"),
) -> None:
    """Export a run's receipts as OpenTelemetry spans.

    With no destination the payload goes to stdout, which is what makes it
    pipeable and diffable. Span ids are derived from the run and node ids, so
    exporting the same run twice produces the same trace rather than a duplicate.
    """
    cfg = _load(config)
    try:
        path = receipts.find(cfg.receipts, run_id)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    entries = list(receipts.iter_receipts(path))
    if not entries:
        console.print(f"[red]run {run_id} has no receipts to export[/red]")
        raise typer.Exit(2)

    payload = otel.to_otlp(entries)
    count = otel.span_count(payload)

    if out is None and otlp is None:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    if out is not None:
        console.print(f"[green]wrote {count} span(s) to {otel.write(payload, out)}[/green]")

    if otlp is not None:
        try:
            status = otel.post(payload, otlp)
        except otel.ExportFailed as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
        console.print(f"[green]exported {count} span(s) to {otlp} ({status})[/green]")

    # Said out loud, not buried in a docstring: whoever is about to read these
    # spans in a backend is exactly the person who must not mistake a derived
    # duration for a measured one.
    console.print(
        "[dim]span durations are derived from ledger order; receipts record "
        "completion time only[/dim]"
    )


@app.command(name="list")
def list_runs(config: Path = typer.Option(Path("overmind.toml"), "--config")) -> None:
    """List recorded runs."""
    cfg = _load(config)
    runs = receipts.list_runs(cfg.receipts)
    console.print("\n".join(runs) if runs else "[dim]no runs recorded[/dim]")


@app.command()
def doctor(config: Path = typer.Option(Path("overmind.toml"), "--config")) -> None:
    """Check that every upstream this repo depends on is reachable."""
    cfg = _load(config)
    ok = True

    missing = executor.preflight()
    console.print(
        "[green]omnigent + git present[/green]"
        if not missing
        else f"[red]missing: {missing}[/red]"
    )
    ok = ok and not missing

    bridge_up = planner.health(cfg)
    console.print(
        f"[green]OMA bridge up at {cfg.bridge.url}[/green]"
        if bridge_up
        else f"[red]OMA bridge unreachable at {cfg.bridge.url}[/red] (run `make bridge`)"
    )
    ok = ok and bridge_up

    if cfg.memory.enabled:
        try:
            with RufloMemory(cfg.memory) as mem:
                mem.recall("healthcheck", limit=1)
            console.print("[green]ruflo memory reachable[/green]")
        except MemoryUnavailable as exc:
            console.print(f"[yellow]ruflo memory unavailable: {exc}[/yellow] (degrades, not fatal)")
    else:
        console.print("[dim]ruflo memory disabled in config[/dim]")

    # The container publishes OVERMIND_BRIDGE_PORT; the client dials
    # [bridge] url. When those disagree the symptom is "bridge unreachable" on a
    # bridge that is up, which sends people to inspect Docker rather than two
    # numbers that do not match.
    declared = os.environ.get("OVERMIND_BRIDGE_PORT")
    if declared:
        configured = urlparse(cfg.bridge.url).port
        if configured is not None and str(configured) != declared.strip():
            console.print(
                f"[red]port mismatch:[/red] OVERMIND_BRIDGE_PORT={declared.strip()} "
                f"but overmind.toml dials port {configured}"
            )
            ok = False
        else:
            console.print(f"[green]bridge port {declared.strip()} agrees with overmind.toml[/green]")

    # Informational only. A GET to /v1/traces proves nothing, and POSTing to
    # check liveness would put a fabricated trace in someone's backend.
    if endpoint := os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        console.print(f"[dim]traces would export to {endpoint} (overmind export <run-id> --otlp)[/dim]")

    console.print(f"[green]vendors: {sorted(cfg.vendors)}[/green]")
    sys.exit(0 if ok else 1)


@app.command()
def gc() -> None:
    """Remove retained worktrees from failed runs."""
    console.print(f"removed {executor.gc_worktrees()} worktree(s)")


if __name__ == "__main__":
    app()
