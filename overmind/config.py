"""Configuration loading. Reads overmind.toml; no magic defaults hidden in code."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class RunConfig(BaseModel):
    max_parallel: int = 3
    budget_usd: float = 10.0
    ambiguity_threshold: float = 0.65


class BridgeConfig(BaseModel):
    url: str = "http://127.0.0.1:7801"
    timeout_s: int = 180


class MemoryConfig(BaseModel):
    enabled: bool = True
    command: list[str] = Field(default_factory=lambda: ["npx", "ruflo@latest", "mcp", "start"])
    recall_limit: int = 12


class PolicyConfig(BaseModel):
    ask_on_shell: bool = True
    max_tool_calls_per_session: int = 60


class ReceiptConfig(BaseModel):
    dir: Path = Path(".overmind/receipts")
    redact_tool_args: bool = False


class Config(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    # vendor -> omnigent harness name
    vendors: dict[str, str] = Field(default_factory=dict)
    # role -> preferred vendor
    roles: dict[str, str] = Field(default_factory=dict)
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    policies: PolicyConfig = Field(default_factory=PolicyConfig)
    receipts: ReceiptConfig = Field(default_factory=ReceiptConfig)

    @model_validator(mode="after")
    def _require_vendor_diversity(self) -> Config:
        """ADR-004: refuse to start rather than degrade to same-vendor review.

        A single-vendor config would still 'work' and would look identical in
        logs while providing far weaker verification. Failing loudly is the
        whole point.
        """
        if len(self.vendors) < 2:
            raise ValueError(
                "overmind requires at least two vendors so a reviewer can differ "
                f"from the author (ADR-004). configured: {sorted(self.vendors) or 'none'}"
            )
        unknown = {v for v in self.roles.values() if v not in self.vendors}
        if unknown:
            raise ValueError(f"roles reference unconfigured vendors: {sorted(unknown)}")
        return self

    def harness_for(self, vendor: str) -> str:
        try:
            return self.vendors[vendor]
        except KeyError:
            raise ValueError(f"vendor {vendor!r} is not configured") from None


def load(path: Path | str = "overmind.toml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{p} not found; copy the one in the repo root and edit it")
    with p.open("rb") as fh:
        return Config.model_validate(tomllib.load(fh))
