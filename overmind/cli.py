"""overmind CLI. Thin: it wires the modules together and prints."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import config as config_mod
from . import executor, gates, linearity, planner, receipts, router
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

    raw, ambiguity = planner.plan(goal, cfg, prior)
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
) -> None:
    """Plan only. No execution, no spend."""
    cfg = _load(config)
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
) -> bool:
    """Execute levels in order. Returns True if the run halted.

    Budget exhaustion is a halt, not a failure: the checkpoint stays clean and
    resumable rather than dying mid-diff.
    """
    router.audit(p)  # re-check vendor diversity right before spending

    for depth, level in enumerate(p.levels):
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

            node_gates = [
                gates.loop_detect(receipt),
                gates.exit_proof(node, outcome.exit_code),
                gates.decision_surface(receipt),
                gates.action_trace(node, receipt),
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

    console.print(f"[green]vendors: {sorted(cfg.vendors)}[/green]")
    sys.exit(0 if ok else 1)


@app.command()
def gc() -> None:
    """Remove retained worktrees from failed runs."""
    console.print(f"removed {executor.gc_worktrees()} worktree(s)")


if __name__ == "__main__":
    app()
