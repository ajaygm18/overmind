"""A real git repository for tests.

`integrate.py` runs `git merge --no-ff`, `git merge --abort` and
`git reset --hard` against a real branch. Mocking `subprocess` there would
assert that the argument lists have not changed, which is not the property worth
protecting -- the property worth protecting is that a conflict leaves the base
branch byte-identical to where it started. Only real git can demonstrate that.

Everything is confined to a tmp_path. Nothing here touches the developer's
repository, and no test in this suite may be run from the repo root without a
`monkeypatch.chdir` first, because `integrate` deliberately operates on the
process working directory.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# Paths Overmind creates but never intends to commit. Without these ignores a
# repository is dirty the instant the first worktree appears, and integrate()
# refuses to start -- so this is part of the contract, not test scaffolding.
GITIGNORE = ".worktrees/\n.overmind/\n"


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def must(args: list[str], cwd: Path) -> str:
    res = git(args, cwd)
    if res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {res.stderr.strip()}")
    return res.stdout.strip()


@dataclass
class Repo:
    """A throwaway repository on branch `main` with one commit."""

    path: Path

    # -- inspection ------------------------------------------------------

    def head(self, ref: str = "HEAD") -> str:
        return must(["rev-parse", ref], self.path)

    def branch(self) -> str:
        return must(["rev-parse", "--abbrev-ref", "HEAD"], self.path)

    def branches(self) -> list[str]:
        out = must(["for-each-ref", "--format=%(refname:short)", "refs/heads"], self.path)
        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def dirty(self) -> bool:
        return bool(git(["status", "--porcelain"], self.path).stdout.strip())

    def merging(self) -> bool:
        """True while a merge is unresolved. Must be false after a rollback."""
        return (self.path / ".git" / "MERGE_HEAD").exists()

    def read(self, rel: str) -> str:
        target = self.path / rel
        return target.read_text() if target.exists() else ""

    def exists(self, rel: str) -> bool:
        return (self.path / rel).exists()

    # -- mutation --------------------------------------------------------

    def write(self, rel: str, text: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str) -> str:
        must(["add", "-A"], self.path)
        must(["commit", "-m", message], self.path)
        return self.head()

    def worktree(self, run_id: str, node_id: str) -> Path:
        """Create a worktree the way the executor does, named the same way.

        Used for arranging test state. `test_branch_naming_agrees_with_the_executor`
        goes through `executor.make_worktree` itself so the two cannot drift.
        """
        path = self.path / ".worktrees" / f"{run_id}-{node_id}"
        path.parent.mkdir(parents=True, exist_ok=True)
        must(["worktree", "add", "-b", f"overmind/{run_id}/{node_id}", str(path)], self.path)
        return path

    def work(self, worktree: Path, rel: str, text: str, commit: bool = False) -> None:
        """Simulate what an agent session leaves behind.

        Uncommitted by default, because that is what agents actually do --
        `commit_worktree` exists precisely to handle it.
        """
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        if commit:
            must(["add", "-A"], worktree)
            must(["commit", "-m", f"work: {rel}"], worktree)


def make_repo(root: Path) -> Repo:
    """Initialise a repository that behaves like a real project checkout.

    Identity and signing are set locally so the suite passes on a machine with
    commit signing enabled globally, and `-b main` pins the branch name rather
    than inheriting whatever the host's `init.defaultBranch` happens to be.
    """
    root.mkdir(parents=True, exist_ok=True)
    must(["init", "-b", "main"], root)
    must(["config", "user.email", "test@overmind.invalid"], root)
    must(["config", "user.name", "Overmind Test"], root)
    must(["config", "commit.gpgsign", "false"], root)

    repo = Repo(path=root)
    repo.write(".gitignore", GITIGNORE)
    repo.write("src/app.py", "VERSION = 1\n")
    repo.commit("initial")
    return repo
