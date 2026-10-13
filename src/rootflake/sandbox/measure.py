"""Measures one test under runc and runsc to compare outcomes and wall time.

Only fixture projects from this repository's tests/fixtures/maven/ are measured, never a
corpus project: runc shares the host kernel, so it must not run code nobody here wrote. The
fixture is staged under the managed root's pristine/ and copied into a fresh run directory
the usual way. Each round runs the test once per runtime, alternating which runtime goes
first, so drift on the machine falls on both runtimes alike. Every run goes through run_test
and its argument check unchanged; only the measurement runner swaps the checked
`--runtime runsc` value. Normal runs have no way to choose a runtime. Only first attempts
are timed; a retried run is counted but left out of the time statistics.

    python -m rootflake.sandbox.measure --image IMG --fixture NAME --maven-repo M2 \\
        --test pkg.Class#method --runs 50 result.json
"""

import argparse
import hashlib
import json
import os
import pyexpat
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from rootflake.sandbox.container import RUNTIME, RunSpec, SandboxLimits
from rootflake.sandbox.executor import DockerRunner, ProcessRunner, ProcResult, run_test
from rootflake.sandbox.reports import read_report as read_report_file
from rootflake.sandbox.results import TestId
from rootflake.sandbox.workspace import ManagedRoot, default_root

ALLOWED_RUNTIMES = ("runc", "runsc")
FIXTURE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,60}")
FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "maven"


def resolve_fixture(name: str, fixtures_dir: Path = FIXTURES_DIR) -> Path:
    """The fixture project `name`, strictly inside the fixtures directory, with no symlinks."""
    if not FIXTURE_RE.fullmatch(name):
        raise ValueError(f"fixture name must match {FIXTURE_RE.pattern}: {name!r}")
    try:
        base = fixtures_dir.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"no fixtures directory at {fixtures_dir}") from error
    path = base / name
    if path.is_symlink():
        raise ValueError(f"fixture must not be a symlink: {path}")
    if not path.is_dir() or path.resolve(strict=True).parent != base:
        raise ValueError(f"no such fixture: {path}")
    for directory, dirnames, filenames in os.walk(path):
        for entry in dirnames + filenames:
            if os.path.islink(os.path.join(directory, entry)):
                raise ValueError(f"fixture contains a symlink: {directory}/{entry}")
    return path


def _stage(root: ManagedRoot, name: str, fixture: Path) -> Path:
    digest = hashlib.sha256()
    for directory, _dirnames, filenames in sorted(os.walk(fixture)):
        for filename in sorted(filenames):
            file = Path(directory) / filename
            digest.update(str(file.relative_to(fixture)).encode() + b"\0")
            digest.update(file.read_bytes() + b"\0")
    pristine = root.pristine_dir(f"fixture-{name}-{digest.hexdigest()[:40]}")
    shutil.copytree(fixture, pristine, dirs_exist_ok=True)
    os.chmod(pristine, 0o755)
    return pristine


class RuntimeOverride:
    """Runs already checked sandbox arguments under runc or runsc. For measurement only."""

    def __init__(self, inner: ProcessRunner, runtime: str) -> None:
        if runtime not in ALLOWED_RUNTIMES:
            raise ValueError(f"runtime must be one of {ALLOWED_RUNTIMES}, got {runtime!r}")
        self.inner = inner
        self.runtime = runtime

    def run(self, args: list[str], timeout_s: float) -> ProcResult:
        flags = [i for i, arg in enumerate(args) if arg == "--runtime"]
        if len(flags) != 1 or args[flags[0] + 1 : flags[0] + 2] != [RUNTIME]:
            raise ValueError(f"expected exactly one --runtime {RUNTIME} in checked arguments")
        swapped = list(args)
        swapped[flags[0] + 1] = self.runtime
        return self.inner.run(swapped, timeout_s)

    def stop(self, name: str) -> None:
        self.inner.stop(name)


def nearest_rank(values: Sequence[float], percent: int) -> float:
    ordered = sorted(values)
    rank = (percent * len(ordered) + 99) // 100
    return ordered[max(rank, 1) - 1]


def _timed(rows: list[dict]) -> list[float]:
    return [row["seconds"] for row in rows if row["attempts"] == 1]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def summarize(rows: list[dict]) -> dict:
    seconds = _timed(rows)
    outcomes: dict[str, int] = {}
    for row in rows:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
    return {
        "runs": len(rows),
        "outcomes": dict(sorted(outcomes.items())),
        "retries": sum(row["attempts"] - 1 for row in rows),
        "timed_runs": len(seconds),
        "excluded_from_timing": len(rows) - len(seconds),
        "median_s": _round(statistics.median(seconds) if seconds else None),
        "p90_s": _round(nearest_rank(seconds, 90) if seconds else None),
        "total_s": _round(sum(seconds) if seconds else None),
    }


def _ratio(rows: list[dict]) -> float | None:
    runc = _timed([row for row in rows if row["runtime"] == "runc"])
    runsc = _timed([row for row in rows if row["runtime"] == "runsc"])
    if not runc or not runsc or statistics.median(runc) <= 0:
        return None
    return round(statistics.median(runsc) / statistics.median(runc), 3)


def first_line(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return f"unavailable: {error}"
    if done.returncode != 0:
        return f"unavailable: exit {done.returncode}"
    lines = done.stdout.splitlines()
    return lines[0].strip() if lines else ""


def collect_versions(spec: RunSpec, command: Callable[[list[str]], str] = first_line) -> dict:
    return {
        "runsc": command(["runsc", "--version"]),
        "docker": command(["docker", "version", "--format", "{{.Server.Version}}"]),
        "image": spec.image,
        "jdk": spec.jdk,
        "expat": pyexpat.EXPAT_VERSION,
    }


def measure_runtimes(
    fixture: str,
    root: ManagedRoot,
    maven_repo: Path,
    jdk: int,
    limits: SandboxLimits,
    test: TestId,
    module: str,
    runs: int,
    runtimes: Sequence[str] = ALLOWED_RUNTIMES,
    *,
    fixtures_dir: Path = FIXTURES_DIR,
    runner: ProcessRunner | None = None,
    read_report: Callable[[Path, Path], bytes | None] = read_report_file,
    timeout_s: float = 600,
    clock: Callable[[], float] = time.monotonic,
    versions: Callable[[RunSpec], dict] = collect_versions,
) -> dict:
    if type(runs) is not int or runs < 1:
        raise ValueError(f"runs must be a positive integer, got {runs!r}")
    if not runtimes or len(set(runtimes)) != len(runtimes):
        raise ValueError(f"runtimes must be distinct and non empty: {runtimes}")
    overrides = {name: RuntimeOverride(runner or DockerRunner(), name) for name in runtimes}
    pristine = _stage(root, fixture, resolve_fixture(fixture, fixtures_dir))
    run_dir, workspace = root.new_run("run")
    spec = RunSpec(
        image=root.image,
        workspace=workspace,
        maven_repo=maven_repo,
        jdk=jdk,
        command=["placeholder"],
        name="rootflake-00000000",
    )
    rows: list[dict] = []
    try:
        copy_errors = root.copy_from(run_dir, pristine)
        if not copy_errors:
            for round_ in range(runs):
                order = runtimes if round_ % 2 == 0 else tuple(reversed(runtimes))
                for name in order:
                    started = clock()
                    record = run_test(
                        spec, limits, test, module, overrides[name], read_report,
                        (root.path,), timeout_s,
                    )  # fmt: skip
                    rows.append(
                        {
                            "round": round_,
                            "runtime": name,
                            "outcome": record.outcome.value,
                            "attempts": record.attempts,
                            "exit_code": record.exit_code,
                            "seconds": clock() - started,
                        }
                    )
    except BaseException as error:
        error.add_note(f"cleanup errors: {root.remove(run_dir)}")
        raise
    cleanup_errors = root.remove(run_dir)
    if copy_errors:
        raise RuntimeError(f"fixture copy failed: {copy_errors}; cleanup: {cleanup_errors}")
    return {
        "fixture": fixture,
        "test": str(test),
        "module": module,
        "runs_per_runtime": runs,
        "runtimes": {
            name: summarize([r for r in rows if r["runtime"] == name]) for name in runtimes
        },
        "ratio_median_runsc_over_runc": _ratio(rows),
        "versions": versions(spec),
        "runs": rows,
        "cleanup_errors": cleanup_errors,
    }


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="Compare one fixture test under runc and runsc")
    parser.add_argument("--image", required=True, help="sandbox image pinned by digest")
    parser.add_argument("--fixture", required=True, help="name under tests/fixtures/maven/")
    parser.add_argument("--root-dir", type=Path, default=None, help="managed root")
    parser.add_argument("--maven-repo", required=True, type=Path)
    parser.add_argument("--test", required=True, help="pkg.Class#method")
    parser.add_argument("--module", default="")
    parser.add_argument("--jdk", type=int, default=17)
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("output", type=Path)
    options = parser.parse_args(argv)
    root = ManagedRoot(options.root_dir or default_root(), options.image, DockerRunner())
    result = measure_runtimes(
        options.fixture,
        root,
        options.maven_repo,
        options.jdk,
        SandboxLimits(),
        TestId.parse(options.test),
        options.module,
        options.runs,
        timeout_s=options.timeout,
    )
    options.output.write_text(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
