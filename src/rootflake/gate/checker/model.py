"""Types shared by the checker's entry point and its rule modules."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from rootflake.gate.checker.calls import ExpansionBudget
from rootflake.gate.checker.scope import Program, is_test_code
from rootflake.gate.checker.syntax import CompilationUnit


@dataclass(frozen=True)
class Finding:
    code: str
    path: str | None
    line: int | None = None

    @property
    def is_flag(self) -> bool:
        return self.code.startswith("flag/")


@dataclass(frozen=True)
class Target:
    class_name: str
    method: str | None
    module: str | None = None  # the target's module path, "" for the repository root

    @property
    def dotted(self) -> str:
        return self.class_name.replace("$", ".")


@dataclass(frozen=True)
class CheckResult:
    findings: tuple[Finding, ...]

    @property
    def verdict(self) -> Literal["reject", "accept", "accept_flagged"]:
        if any(not finding.is_flag for finding in self.findings):
            return "reject"
        return "accept_flagged" if self.findings else "accept"


@dataclass(frozen=True)
class FileChange:
    path: str
    before: CompilationUnit | None
    after: CompilationUnit


@dataclass
class Context:
    changes: list[FileChange]
    target: Target | None
    overridden: Sequence[str] = ()
    budget: ExpansionBudget = field(default_factory=ExpansionBudget)

    def __post_init__(self) -> None:
        self.before = Program({c.path: c.before for c in self.changes if c.before is not None})
        self.after = Program({c.path: c.after for c in self.changes})
        # A new assertion earns no credit through a name either side declares; an old one keeps
        # what its own side gave it, so taking credit away never hides an assertion removed.
        self.before.declared_fqns = frozenset(self.before.types_by_fqn)
        self.before.old_side = True
        self.after.pristine = self.before
        self.after.declared_fqns = self.before.declared_fqns | frozenset(self.after.types_by_fqn)
        self.test_paths = {
            c.path
            for c in self.changes
            if is_test_code(c.path, c.after)
            or (c.before is not None and is_test_code(c.path, c.before))
        }
