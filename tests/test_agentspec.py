"""T02 and T03 tests: generated specs, and worktree confinement.

No Omnigent process and no git. `build_spec` is pure, and `write_spec` is given
explicit roots so it writes only into tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from overmind.agentspec import (
    ALLOWED_TOP_LEVEL,
    AgentSpecError,
    build_executor,
    build_instructions,
    build_os_env,
    build_spec,
    role_persona,
    write_spec,
)
from overmind.config import Config
from overmind.models import ExitCheck, ExitKind, Role, TaskNode

WORKTREE = "/tmp/wt/run1-n1"


def cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "vendors": {"anthropic": "claude-sdk", "openai": "codex"},
    }
    base.update(overrides)
    return Config.model_validate(base)


def node(**overrides: object) -> TaskNode:
    base: dict[str, object] = {
        "id": "n1",
        "role": Role.IMPLEMENTER,
        "intent": "add device-code auth",
        "acceptance": "tests in tests/test_auth.py pass",
        "reads": ["src/"],
        "writes": ["src/auth.py"],
        "exit_check": ExitCheck(kind=ExitKind.TESTS_PASS),
        "vendor": "anthropic",
        "harness": "claude-sdk",
        "budget_usd": 2.0,
    }
    base.update(overrides)
    return TaskNode.model_validate(base)


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    (d / "implementer.yaml").write_text(
        "name: implementer\nprompt: |\n  You implement exactly what was asked.\n"
    )
    return d


# --------------------------------------------------------------------------
# documented-fields contract
# --------------------------------------------------------------------------


def test_spec_uses_only_documented_top_level_fields() -> None:
    spec = build_spec(node(), cfg(), WORKTREE, "/tmp/i.md")
    assert set(spec) <= ALLOWED_TOP_LEVEL


def test_spec_rejects_undocumented_fields_rather_than_shipping_them() -> None:
    assert "prompt" not in build_spec(node(), cfg(), WORKTREE, "/tmp/i.md")


def test_executor_omits_model_and_auth_when_unconfigured() -> None:
    """Several harnesses documented-ly take no auth block; do not invent one."""
    executor = build_executor(node(), cfg())
    assert executor == {"harness": "claude-sdk"}


def test_executor_emits_configured_model_and_auth() -> None:
    conf = cfg(
        models={"anthropic": "databricks-claude-sonnet-4-6"},
        auth={"anthropic": {"type": "databricks", "profile": "oss"}},
    )
    executor = build_executor(node(), conf)
    assert executor["model"] == "databricks-claude-sonnet-4-6"
    assert executor["auth"] == {"type": "databricks", "profile": "oss"}


def test_executor_never_emits_the_deprecated_profile_shorthand() -> None:
    conf = cfg(auth={"anthropic": {"type": "databricks", "profile": "oss"}})
    assert "profile" not in build_executor(node(), conf)


def test_unrouted_node_is_refused() -> None:
    with pytest.raises(AgentSpecError):
        build_executor(node(harness=None), cfg())


def test_config_rejects_model_keyed_by_unknown_vendor() -> None:
    """A typo'd vendor key would silently put the run on the wrong model."""
    with pytest.raises(ValueError, match="models"):
        cfg(models={"anthropc": "some-model"})


# --------------------------------------------------------------------------
# T03: confinement
# --------------------------------------------------------------------------


def test_sandbox_write_paths_is_exactly_the_worktree() -> None:
    sandbox = build_os_env(WORKTREE)["sandbox"]
    assert sandbox["write_paths"] == [WORKTREE]
    assert "." not in sandbox["write_paths"]
    assert "/" not in sandbox["write_paths"]


def test_sandbox_type_is_omitted_so_both_platforms_work() -> None:
    assert "type" not in build_os_env(WORKTREE)["sandbox"]


def test_cwd_is_the_worktree() -> None:
    assert build_os_env(WORKTREE)["cwd"] == WORKTREE


def test_every_spec_carries_worktree_confinement() -> None:
    policies = build_spec(node(), cfg(), WORKTREE, "/tmp/i.md")["policies"]
    confinement = policies["overmind_worktree_confinement"]
    assert confinement["factory_params"]["allowed_dirs"] == [WORKTREE]


def test_confinement_is_present_for_every_role(agents_dir: Path) -> None:
    for role in Role:
        spec = build_spec(node(role=role), cfg(), WORKTREE, "/tmp/i.md")
        assert "overmind_worktree_confinement" in spec["policies"]


# --------------------------------------------------------------------------
# instructions
# --------------------------------------------------------------------------


def test_acceptance_is_passed_verbatim() -> None:
    text = build_instructions(node())
    assert "tests in tests/test_auth.py pass" in text
    assert "do not paraphrase" in text


def test_instructions_state_the_confinement() -> None:
    assert "denied at the tool call" in build_instructions(node())


def test_persona_is_merged_ahead_of_the_task(agents_dir: Path) -> None:
    persona = role_persona("implementer", agents_dir)
    text = build_instructions(node(), persona)
    assert text.index("You implement exactly") < text.index("TASK:")


def test_missing_role_definition_is_an_error_not_a_silent_default(
    tmp_path: Path,
) -> None:
    with pytest.raises(AgentSpecError):
        role_persona("implementer", tmp_path / "nonexistent")


def test_inspector_is_told_which_node_it_reviews() -> None:
    text = build_instructions(node(inspects="n0"))
    assert "inspecting node n0" in text
    assert "PASS or FAIL" in text


# --------------------------------------------------------------------------
# write_spec
# --------------------------------------------------------------------------


def test_write_spec_round_trips_as_yaml(tmp_path: Path, agents_dir: Path) -> None:
    path = write_spec(
        node(), cfg(), "run1", WORKTREE, root=tmp_path / "specs", agents_dir=agents_dir
    )
    loaded = yaml.safe_load(path.read_text())
    assert loaded["name"] == "overmind-n1"
    assert set(loaded) <= ALLOWED_TOP_LEVEL


def test_nothing_is_written_inside_the_worktree(
    tmp_path: Path, agents_dir: Path
) -> None:
    """A file in the worktree would fail the node's own declared_scope gate."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    write_spec(
        node(), cfg(), "run1", worktree, root=tmp_path / "specs", agents_dir=agents_dir
    )
    assert list(worktree.iterdir()) == []


def test_instructions_are_referenced_by_path_not_inlined(
    tmp_path: Path, agents_dir: Path
) -> None:
    path = write_spec(
        node(), cfg(), "run1", WORKTREE, root=tmp_path / "specs", agents_dir=agents_dir
    )
    loaded = yaml.safe_load(path.read_text())
    assert Path(loaded["instructions"]).exists()
    assert "ACCEPTANCE" not in str(loaded["instructions"])


def test_specs_for_two_nodes_do_not_collide(tmp_path: Path, agents_dir: Path) -> None:
    root = tmp_path / "specs"
    a = write_spec(node(id="n1"), cfg(), "run1", WORKTREE, root=root, agents_dir=agents_dir)
    b = write_spec(node(id="n2"), cfg(), "run1", WORKTREE, root=root, agents_dir=agents_dir)
    assert a != b
    assert a.parent == b.parent
