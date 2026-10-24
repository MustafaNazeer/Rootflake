import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rootflake.gate.checker.check import Target, check

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "patches"
PENDING = Path(__file__).with_name("pending.txt")


def pending() -> set[str]:
    lines = PENDING.read_text().splitlines()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def checker_fixtures() -> list[str]:
    ids = []
    for meta_path in sorted(FIXTURES.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        if meta["rejected_by"] != ["patch_validator"]:
            ids.append(meta["id"])
    return ids


def cases():
    waiting = pending()
    for fixture_id in checker_fixtures():
        marks = [pytest.mark.xfail(strict=True, reason="pending")] if fixture_id in waiting else []
        yield pytest.param(fixture_id, marks=marks, id=fixture_id)


def checked_paths(patch: str) -> list[str]:
    paths = set()
    for line in patch.split("\n"):
        if line.startswith("diff --git a/"):
            left, right = line[len("diff --git a/") :].split(" b/", 1)
            paths.update((left, right))
    return sorted(p for p in paths if p.endswith(".java") or "src/test/resources/" in p)


def trees(fixture: Path, tmp_path: Path) -> tuple[Path, Path]:
    pristine, workspace = tmp_path / "pristine", tmp_path / "workspace"
    if (fixture / "before").is_dir():
        shutil.copytree(fixture / "before", pristine)
    else:
        pristine.mkdir()
    shutil.copytree(pristine, workspace)
    subprocess.run(
        [
            "git", "apply", "--unsafe-paths", "--include=*.java",
            "--include=*src/test/resources/*", str(fixture / "patch.diff"),
        ],
        cwd=workspace, check=True, capture_output=True,
    )  # fmt: skip
    return pristine, workspace


@pytest.mark.parametrize("fixture_id", list(cases()))
def test_the_checker_decides_each_fixture_as_its_meta_says(tmp_path, fixture_id):
    fixture = FIXTURES / fixture_id
    meta = json.loads((fixture / "meta.json").read_text())
    pristine, workspace = trees(fixture, tmp_path)
    target = meta["target"] and Target(meta["target"]["class"], meta["target"]["method"])
    patch = (fixture / "patch.diff").read_text(encoding="utf-8")
    result = check(checked_paths(patch), pristine, workspace, target)
    assert sorted({f.code for f in result.findings}) == meta["expected_codes"]
    if meta["expected"] == "reject":
        assert result.verdict == "reject"
    else:
        assert result.verdict == ("accept_flagged" if meta["expected_flags"] else "accept")


@pytest.mark.parametrize("fixture_id", checker_fixtures())
def test_every_fixture_patch_applies_to_its_pristine_tree(tmp_path, fixture_id):
    fixture = FIXTURES / fixture_id
    pristine, workspace = trees(fixture, tmp_path)
    patch = (fixture / "patch.diff").read_text(encoding="utf-8")
    changed = [
        p
        for p in checked_paths(patch)
        if (pristine / p).exists() != (workspace / p).exists()
        or (
            (workspace / p).exists() and (pristine / p).read_bytes() != (workspace / p).read_bytes()
        )
    ]
    assert changed


def test_every_pending_id_is_a_checker_fixture():
    assert pending() <= set(checker_fixtures())


def test_the_checker_fixture_set_is_complete():
    assert len(checker_fixtures()) == 152
