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
    command: list[str] = Field(
        default_factory=lambda: ["npx", "ruflo@latest", "mcp", "start"]
    )
    recall_limit: int = 12

    # Whether the semantic gates may fall back to character n-grams.
    #
    # False (default): a memory outage degrades the measure and every result
    # says `via ngram`. Right for CI, which has no Ruflo and must still exercise
    # the gate offline.
    #
    # True: the fallback is a gate failure. Right for a real run, where a
    # silently weaker loop detector waves through the rephrased repetition it
    # was installed to catch. Requires `enabled = true`; asking for embeddings
    # from a memory layer that is switched off is a contradiction, not a
    # preference, so it is rejected at load rather than at the first gate.
    require_embeddings: bool = False

    @model_validator(mode="after")
    def _required_implies_enabled(self) -> MemoryConfig:
        if self.require_embeddings and not self.enabled:
            raise ValueError(
                "[memory] require_embeddings = true needs enabled = true; "
                "embeddings cannot be required from a disabled memory layer"
            )
        return self


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
    # vendor -> model id, emitted as executor.model in generated specs.
    # Optional: AGENT_YAML_SPEC allows CLI flags to supply a missing model.
    models: dict[str, str] = Field(default_factory=dict)
    # vendor -> executor.auth block, emitted verbatim. Optional because several
    # harnesses (cursor, copilot, kimi) authenticate from the ambient
    # environment and documented-ly take no auth block at all.
    auth: dict[str, dict[str, str]] = Field(default_factory=dict)
    # tool name -> declaration, passed through to generated specs
    tools: dict[str, dict[str, object]] = Field(default_factory=dict)
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

    @model_validator(mode="after")
    def _models_and_auth_name_known_vendors(self) -> Config:
        """A model or auth block keyed by a typo would be silently ignored.

        Silently ignored configuration is how a run ends up on the wrong model
        while the config file looks correct, so it is rejected instead.
        """
        for label, mapping in (("models", self.models), ("auth", self.auth)):
            unknown = sorted(set(mapping) - set(self.vendors))
            if unknown:
                raise ValueError(f"[{label}] references unconfigured vendors: {unknown}")
        return self

    def harness_for(self, vendor: str) -> str:
        try:
            return self.vendors[vendor]
        except KeyError:
            raise ValueError(f"vendor {vendor!r} is not configured") from None


def load(path: Path | str = "overmind.toml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found; copy the one in the repo root and edit it"
        )
    with p.open("rb") as fh:
        return Config.model_validate(tomllib.load(fh))
