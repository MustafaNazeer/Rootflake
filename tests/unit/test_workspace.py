import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from rootflake.sandbox import workspace as workspace_module
from rootflake.sandbox.executor import ProcResult
from rootflake.sandbox.workspace import (
    COPY_TIMEOUT_S,
    PROJECT_KEY_RE,
    RUNTIME,
    ManagedRoot,
    build_copy_args,
    build_remove_args,
    build_unlock_args,
    check_copy_args,
    check_remove_args,
    check_unlock_args,
    corpus_key,
    default_root,
    project_slug,
)

IMAGE = "rootflake-sandbox@sha256:" + "a" * 64
NAME = "rootflake-0123456789abcdef"
SHA = "0123456789abcdef0123456789abcdef01234567"
KEY = f"demo-{SHA}"


class FakeRunner:
    def __init__(self, *results, error=None):
        self.results = list(results)
        self.error = error
        self.calls = []
        self.stopped = []

    def run(self, args, timeout_s):
        self.calls.append((args, timeout_s))
        if self.error:
            raise self.error
        return self.results.pop(0) if self.results else ProcResult(0, b"", b"", False)

    def stop(self, name):
        self.stopped.append(name)


def make_root(tmp_path, runner=None):
    path = tmp_path / "root"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    root = ManagedRoot(path, IMAGE, runner or FakeRunner())
    (root.pristine_dir(KEY) / "pom.xml").write_text("<project/>")
    return root


def tree(root):
    return root.path / "pristine" / KEY


def mode(path):
    return stat.S_IMODE(path.lstat().st_mode)


def test_default_root_is_under_the_current_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_root() == tmp_path / ".local" / "share" / "rootflake"
    assert default_root().is_relative_to(Path.home())


def test_a_missing_root_is_created_private(tmp_path):
    root = ManagedRoot(tmp_path / "new" / "root", IMAGE, FakeRunner())
    assert mode(root.path) == 0o700
    assert (root.path / "runs").is_dir() and (root.path / "repos").is_dir()


def test_an_existing_root_gets_runs_and_repos(tmp_path):
    root = make_root(tmp_path)
    for sub in ("runs", "repos", "pristine"):
        assert (root.path / sub).is_dir() and not (root.path / sub).is_symlink()


def test_root_must_be_absolute_and_not_slash(tmp_path):
    with pytest.raises(ValueError):
        ManagedRoot(Path("relative/root"), IMAGE, FakeRunner())
    with pytest.raises(ValueError):
        ManagedRoot(Path("/"), IMAGE, FakeRunner())


def test_root_must_not_be_a_symlink(tmp_path):
    real = make_root(tmp_path).path
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        ManagedRoot(link, IMAGE, FakeRunner())


def test_root_must_be_mode_0700(tmp_path):
    path = make_root(tmp_path).path
    path.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        ManagedRoot(path, IMAGE, FakeRunner())


def test_root_must_belong_to_the_current_user(tmp_path, monkeypatch):
    path = make_root(tmp_path).path
    monkeypatch.setattr(workspace_module.os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(ValueError, match="owned"):
        ManagedRoot(path, IMAGE, FakeRunner())


def test_root_path_must_be_plain_characters(tmp_path):
    path = tmp_path / "a,b"
    path.mkdir(mode=0o700)
    with pytest.raises(ValueError):
        ManagedRoot(path, IMAGE, FakeRunner())


def test_a_symlinked_runs_directory_is_refused(tmp_path):
    path = make_root(tmp_path).path
    os.rmdir(path / "runs")
    (path / "runs").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="runs"):
        ManagedRoot(path, IMAGE, FakeRunner())


def test_the_image_must_be_pinned(tmp_path):
    with pytest.raises(ValueError, match="digest"):
        ManagedRoot(tmp_path / "x", "rootflake-sandbox:dev", FakeRunner())


def test_new_run_makes_a_fresh_shared_directory_per_call(tmp_path):
    root = make_root(tmp_path)
    run_dir, ws = root.new_run("run")
    other, _ = root.new_run("run")
    assert run_dir != other
    assert run_dir.parent == root.path / "runs"
    assert run_dir.name.startswith("run-")
    assert mode(run_dir) == 0o777
    assert ws == run_dir / "ws" and not ws.exists()


def test_download_and_run_copies_are_different_directories(tmp_path):
    root = make_root(tmp_path)
    download, _ = root.new_run("download")
    run, _ = root.new_run("run")
    assert download != run
    assert download.name.startswith("download-")


def test_unknown_kinds_are_refused(tmp_path):
    with pytest.raises(ValueError):
        make_root(tmp_path).new_run("shared")


def test_a_download_copy_is_never_handed_out_for_test_runs(tmp_path):
    root = make_root(tmp_path)
    download, ws = root.new_run("download")
    run, run_ws = root.new_run("run")
    assert root.workspace(download, "download") == ws
    assert root.workspace(run, "run") == run_ws
    with pytest.raises(ValueError, match="kind"):
        root.workspace(download, "run")
    with pytest.raises(ValueError, match="kind"):
        root.workspace(run, "download")


@pytest.mark.parametrize(
    "relative", ["repos/run-0123456789abcdef", "runs/run-xyz", "runs/run-0123456789abcdef/ws"]
)
def test_workspace_refuses_paths_that_are_not_managed_run_dirs(tmp_path, relative):
    root = make_root(tmp_path)
    (root.path / relative).mkdir(parents=True)
    with pytest.raises(ValueError):
        root.workspace(root.path / relative, "run")


def expected_copy(pristine, run_dir):
    return [
        "docker", "run", "--rm",
        "--name", NAME,
        "--runtime", "runc",
        "--network", "none",
        "--user", "10001:10001",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--read-only",
        "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1",
        "--pids-limit", "64",
        "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
        "--mount", f"type=bind,src={pristine},dst=/src,readonly",
        "--mount", f"type=bind,src={run_dir},dst=/dst",
        IMAGE,
        "cp", "-a", "/src/.", "/dst/ws",
    ]  # fmt: skip


def test_runtime_is_runc_in_one_constant():
    assert RUNTIME == "runc"


def test_copy_args_are_exactly_the_hardened_form(tmp_path):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    pristine = tree(root)
    assert build_copy_args(NAME, IMAGE, pristine, run_dir) == expected_copy(pristine, run_dir)


def test_remove_args_are_exactly_the_hardened_form(tmp_path):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    copy = expected_copy(tree(root), run_dir)
    expected = (
        copy[: copy.index("--mount")]
        + ["--mount", f"type=bind,src={run_dir},dst=/dst", IMAGE]
        + ["find", "/dst", "-mindepth", "1", "-delete"]
    )
    assert build_remove_args(NAME, IMAGE, run_dir) == expected


def _swap(old, new):
    return lambda a: [new if x == old else x for x in a]


def _insert(*extra):
    return lambda a: a[:3] + list(extra) + a[3:]


COMMON_MUTATIONS = {
    "privileged": _insert("--privileged"),
    "extra volume": _insert("-v", "/:/host"),
    "no read only": lambda a: [x for x in a if x != "--read-only"],
    "no rm": lambda a: [x for x in a if x != "--rm"],
    "host network": _swap("none", "host"),
    "root user": _swap("10001:10001", "0:0"),
    "runsc runtime": _swap("runc", "runsc"),
    "caps kept": _swap("ALL", "NET_RAW"),
    "new privileges": _swap("no-new-privileges=true", "no-new-privileges=false"),
    "bigger memory": _swap("512m", "4096m"),
    "more pids": _swap("64", "4096"),
    "other image": _swap(IMAGE, "rootflake-sandbox@sha256:" + "b" * 64),
    "other name": _swap(NAME, "rootflake-ffffffffffffffff"),
    "trailing arg": lambda a: a + ["extra"],
    "inline flag": lambda a: [("--network=host" if x == "--network" else x) for x in a],
}


@pytest.mark.parametrize(
    "mutate",
    list(COMMON_MUTATIONS.values())
    + [
        lambda a: [x.removesuffix(",readonly") for x in a],
        lambda a: [("-L" if x == "-a" else x) for x in a],
        lambda a: [("/dst/ws/../.." if x == "/dst/ws" else x) for x in a],
    ],
    ids=list(COMMON_MUTATIONS) + ["writable source", "follow links", "escape dst"],
)
def test_copy_check_rejects_every_mutation(tmp_path, mutate):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    pristine = tree(root)
    args = build_copy_args(NAME, IMAGE, pristine, run_dir)
    check_copy_args(args, root, NAME, IMAGE, pristine, run_dir)
    with pytest.raises(ValueError):
        check_copy_args(mutate(args), root, NAME, IMAGE, pristine, run_dir)


@pytest.mark.parametrize(
    "mutate",
    list(COMMON_MUTATIONS.values())
    + [
        lambda a: [("/" if x == "/dst" else x) for x in a],
        lambda a: a[:-1] + ["-print"],
    ],
    ids=list(COMMON_MUTATIONS) + ["find from slash", "other action"],
)
def test_remove_check_rejects_every_mutation(tmp_path, mutate):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    args = build_remove_args(NAME, IMAGE, run_dir)
    check_remove_args(args, root, NAME, IMAGE, run_dir)
    with pytest.raises(ValueError):
        check_remove_args(mutate(args), root, NAME, IMAGE, run_dir)


def test_unlock_args_are_exactly_the_hardened_form(tmp_path):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    remove = build_remove_args(NAME, IMAGE, run_dir)
    expected = remove[: remove.index(IMAGE) + 1] + ["chmod", "-R", "u+rwX", "/dst/ws"]
    assert build_unlock_args(NAME, IMAGE, run_dir) == expected


@pytest.mark.parametrize(
    "mutate",
    list(COMMON_MUTATIONS.values())
    + [
        lambda a: [("/dst" if x == "/dst/ws" else x) for x in a],
        lambda a: [("-L" if x == "-R" else x) for x in a],
        lambda a: [("777" if x == "u+rwX" else x) for x in a],
        lambda a: a[:-1] + ["-L", "/dst/ws"],
    ],
    ids=list(COMMON_MUTATIONS) + ["whole mount", "no recursion", "world writable", "follow"],
)
def test_unlock_check_rejects_every_mutation(tmp_path, mutate):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    args = build_unlock_args(NAME, IMAGE, run_dir)
    check_unlock_args(args, root, NAME, IMAGE, run_dir)
    with pytest.raises(ValueError):
        check_unlock_args(mutate(args), root, NAME, IMAGE, run_dir)


def test_copy_check_requires_a_pristine_tree_inside_the_root(tmp_path):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    outside = tmp_path / "outside"
    outside.mkdir()
    other_run, _ = root.new_run("run")
    (root.path / "other").mkdir()
    candidates = (
        outside,
        other_run,
        root.path / "repos",
        root.path,
        root.path / "other",
        root.path / "pristine",
    )
    for pristine in candidates:
        args = build_copy_args(NAME, IMAGE, pristine, run_dir)
        with pytest.raises(ValueError):
            check_copy_args(args, root, NAME, IMAGE, pristine, run_dir)


def test_copy_check_refuses_a_symlinked_pristine_tree(tmp_path):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    link = root.path / "pristine" / f"linked-{SHA}"
    link.symlink_to(tree(root))
    args = build_copy_args(NAME, IMAGE, link, run_dir)
    with pytest.raises(ValueError, match="symlink"):
        check_copy_args(args, root, NAME, IMAGE, link, run_dir)


def test_builders_refuse_commas_and_dot_dot(tmp_path):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    for bad in (root.path / "a,b", tree(root) / ".." / "x"):
        with pytest.raises(ValueError):
            build_copy_args(NAME, IMAGE, bad, run_dir)
        with pytest.raises(ValueError):
            build_remove_args(NAME, IMAGE, bad)


def test_copy_and_remove_checks_refuse_an_unmanaged_run_dir(tmp_path):
    root = make_root(tmp_path)
    stray = root.path / "repos" / "run-0123456789abcdef"
    stray.mkdir()
    pristine = tree(root)
    with pytest.raises(ValueError):
        check_copy_args(
            build_copy_args(NAME, IMAGE, pristine, stray), root, NAME, IMAGE, pristine, stray
        )
    with pytest.raises(ValueError):
        check_remove_args(build_remove_args(NAME, IMAGE, stray), root, NAME, IMAGE, stray)


def test_copy_runs_the_checked_copy_container(tmp_path):
    runner = FakeRunner()
    root = make_root(tmp_path, runner)
    run_dir, _ = root.new_run("run")
    assert root.copy_from(run_dir, tree(root)) == []
    (args, timeout_s) = runner.calls[0]
    name = args[args.index("--name") + 1]
    assert args == build_copy_args(name, IMAGE, tree(root), run_dir)
    assert timeout_s == COPY_TIMEOUT_S


def test_a_failed_copy_is_recorded(tmp_path):
    root = make_root(tmp_path, FakeRunner(ProcResult(1, b"", b"cp: cannot open", False)))
    run_dir, _ = root.new_run("run")
    errors = root.copy_from(run_dir, tree(root))
    assert len(errors) == 1 and "cp: cannot open" in errors[0]


def test_a_copy_timeout_stops_the_container_and_is_recorded(tmp_path):
    runner = FakeRunner(ProcResult(-1, b"", b"", True))
    root = make_root(tmp_path, runner)
    run_dir, _ = root.new_run("run")
    errors = root.copy_from(run_dir, tree(root))
    name = runner.calls[0][0][runner.calls[0][0].index("--name") + 1]
    assert runner.stopped == [name]
    assert len(errors) == 1 and "timed out" in errors[0]


def test_a_runner_error_during_copy_is_recorded(tmp_path):
    root = make_root(tmp_path, FakeRunner(error=OSError("docker not found")))
    run_dir, _ = root.new_run("run")
    errors = root.copy_from(run_dir, tree(root))
    assert len(errors) == 1 and "docker not found" in errors[0]


def test_copy_refuses_a_run_dir_that_already_has_a_workspace(tmp_path):
    root = make_root(tmp_path)
    run_dir, ws = root.new_run("run")
    ws.mkdir()
    with pytest.raises(ValueError, match="already"):
        root.copy_from(run_dir, tree(root))


def test_remove_runs_the_remove_container_then_removes_the_run_dir(tmp_path):
    runner = FakeRunner()
    root = make_root(tmp_path, runner)
    run_dir, _ = root.new_run("download")
    assert root.remove(run_dir) == []
    args, timeout_s = runner.calls[0]
    name = args[args.index("--name") + 1]
    assert args == build_remove_args(name, IMAGE, run_dir)
    assert timeout_s == COPY_TIMEOUT_S
    assert not run_dir.exists()


def test_remove_unlocks_an_existing_workspace_first(tmp_path):
    runner = FakeRunner()
    root = make_root(tmp_path, runner)
    run_dir, ws = root.new_run("run")
    ws.mkdir()
    errors = root.remove(run_dir)
    unlock, remove = (call[0] for call in runner.calls)
    assert unlock == build_unlock_args(unlock[unlock.index("--name") + 1], IMAGE, run_dir)
    assert remove == build_remove_args(remove[remove.index("--name") + 1], IMAGE, run_dir)
    assert all(timeout == COPY_TIMEOUT_S for _, timeout in runner.calls)
    assert len(errors) == 1 and str(run_dir) in errors[0]


def test_a_failed_unlock_is_recorded_and_the_delete_still_runs(tmp_path):
    runner = FakeRunner(ProcResult(1, b"", b"chmod: denied", False))
    root = make_root(tmp_path, runner)
    run_dir, ws = root.new_run("run")
    ws.mkdir()
    errors = root.remove(run_dir)
    assert len(runner.calls) == 2
    assert any("chmod: denied" in e for e in errors)


def test_a_symlinked_workspace_is_not_unlocked(tmp_path):
    runner = FakeRunner()
    root = make_root(tmp_path, runner)
    run_dir, ws = root.new_run("run")
    ws.symlink_to(tmp_path)
    errors = root.remove(run_dir)
    assert [call[0][-1] for call in runner.calls] == ["-delete"]
    assert any("not a real directory" in e for e in errors)


def test_a_failed_remove_container_is_recorded_and_rmdir_still_tried(tmp_path):
    root = make_root(tmp_path, FakeRunner(ProcResult(1, b"", b"find: denied", False)))
    run_dir, _ = root.new_run("run")
    errors = root.remove(run_dir)
    assert any("find: denied" in e for e in errors)
    assert not run_dir.exists()


def test_a_failed_rmdir_is_recorded(tmp_path):
    root = make_root(tmp_path)
    run_dir, _ = root.new_run("run")
    (run_dir / "left").write_text("x")
    errors = root.remove(run_dir)
    assert len(errors) == 1 and str(run_dir) in errors[0]


def test_a_remove_timeout_and_runner_error_are_recorded(tmp_path):
    runner = FakeRunner(ProcResult(-1, b"", b"", True))
    root = make_root(tmp_path, runner)
    run_dir, _ = root.new_run("run")
    errors = root.remove(run_dir)
    assert runner.stopped and any("timed out" in e for e in errors)
    root = ManagedRoot(root.path, IMAGE, FakeRunner(error=OSError("daemon gone")))
    run_dir, _ = root.new_run("run")
    assert any("daemon gone" in e for e in root.remove(run_dir))


def test_pristine_dir_is_a_host_owned_directory_per_project_key(tmp_path):
    root = make_root(tmp_path)
    pristine = root.pristine_dir(f"commons-lang-{SHA}")
    assert pristine == root.path / "pristine" / f"commons-lang-{SHA}"
    assert mode(pristine) == 0o755
    assert root.pristine_dir(f"commons-lang-{SHA}") == pristine
    root.check_pristine(pristine)


@pytest.mark.parametrize("key", [f"../x-{SHA}", "commons-lang", f"a,b-{SHA}"])
def test_pristine_dir_refuses_malformed_keys(tmp_path, key):
    with pytest.raises(ValueError):
        make_root(tmp_path).pristine_dir(key)


def test_pristine_dir_refuses_a_planted_symlink(tmp_path):
    root = make_root(tmp_path)
    (root.path / "pristine" / f"x-{SHA}").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        root.pristine_dir(f"x-{SHA}")


def test_repo_dir_is_a_shared_directory_per_project_key(tmp_path):
    root = make_root(tmp_path)
    repo = root.repo_dir(f"commons-lang-{SHA}")
    assert repo == root.path / "repos" / f"commons-lang-{SHA}"
    assert mode(repo) == 0o777
    assert root.repo_dir(f"commons-lang-{SHA}") == repo


@pytest.mark.parametrize(
    "key",
    [
        f"../x-{SHA}",
        f"a/b-{SHA}",
        f"Upper-{SHA}",
        "commons-lang",
        f"commons-lang-{SHA[:39]}",
        f"-lead-{SHA}",
        f"{'a' * 82}-{SHA}",
        f"a,b-{SHA}",
    ],
)
def test_repo_dir_refuses_malformed_keys(tmp_path, key):
    with pytest.raises(ValueError):
        make_root(tmp_path).repo_dir(key)


def test_repo_dir_refuses_a_planted_symlink(tmp_path):
    root = make_root(tmp_path)
    (root.path / "repos" / f"x-{SHA}").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        root.repo_dir(f"x-{SHA}")


def _sandbox_image():
    if shutil.which("docker") is None:
        return None
    probe = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "rootflake-sandbox:dev",
            "--format",
            "{{json .RepoDigests}}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        return None
    digests = [d for d in json.loads(probe.stdout) if d.startswith("rootflake-sandbox@sha256:")]
    return digests[0] if digests else None


@pytest.mark.docker
def test_real_copy_keeps_symlinks_and_remove_leaves_nothing(tmp_path):
    from rootflake.sandbox.executor import DockerRunner

    image = _sandbox_image()
    if image is None:
        pytest.skip("needs a Docker daemon and the rootflake-sandbox:dev image (build sandbox/)")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("untouched")
    root_path = tmp_path / "root"
    root_path.mkdir(mode=0o700)
    root_path.chmod(0o700)
    root = ManagedRoot(root_path, image, DockerRunner())
    pristine = root.pristine_dir(KEY)
    (pristine / "sub").mkdir(parents=True)
    (pristine / "sub" / "a.txt").write_text("hello")
    (pristine / "escape").symlink_to(outside / "secret.txt")
    (pristine / "escape-dir").symlink_to(outside)
    run_dir, ws = root.new_run("run")
    assert root.copy_from(run_dir, pristine) == []
    assert (ws / "sub" / "a.txt").read_text() == "hello"
    assert (ws / "escape").is_symlink()
    assert os.readlink(ws / "escape") == str(outside / "secret.txt")
    assert (ws / "escape-dir").is_symlink()
    assert (ws / "sub" / "a.txt").stat().st_uid == 10001

    assert root.remove(run_dir) == []
    assert not run_dir.exists()
    assert (outside / "secret.txt").read_text() == "untouched"
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert (pristine / "escape").is_symlink()


@pytest.mark.docker
def test_real_remove_clears_a_directory_locked_by_the_sandbox_user(tmp_path):
    from rootflake.sandbox.executor import DockerRunner

    image = _sandbox_image()
    if image is None:
        pytest.skip("needs a Docker daemon and the rootflake-sandbox:dev image (build sandbox/)")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("untouched")
    (outside / "secret.txt").chmod(0o600)
    root_path = tmp_path / "root"
    root_path.mkdir(mode=0o700)
    root_path.chmod(0o700)
    root = ManagedRoot(root_path, image, DockerRunner())
    pristine = root.pristine_dir(KEY)
    (pristine / "pom.xml").write_text("<project/>")
    (pristine / "escape").symlink_to(outside / "secret.txt")
    run_dir, ws = root.new_run("run")
    assert root.copy_from(run_dir, pristine) == []

    lock = build_remove_args("rootflake-locktest0000", image, run_dir)
    lock = lock[: lock.index(image) + 1] + [
        "bash", "-c",
        "mkdir -p /dst/ws/locked/deep && echo x > /dst/ws/locked/deep/f "
        "&& chmod 000 /dst/ws/locked/deep /dst/ws/locked",
    ]  # fmt: skip
    assert subprocess.run(lock, capture_output=True, timeout=120, check=False).returncode == 0
    assert mode(ws / "locked") == 0

    assert root.remove(run_dir) == []
    assert not run_dir.exists()
    assert mode(outside / "secret.txt") == 0o600
    assert (outside / "secret.txt").read_text() == "untouched"


def test_copy_refuses_a_pristine_tree_outside_the_root_before_running(tmp_path):
    runner = FakeRunner()
    root = make_root(tmp_path, runner)
    run_dir, _ = root.new_run("run")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError):
        root.copy_from(run_dir, outside)
    assert runner.calls == []


@pytest.mark.parametrize("builder", ["build_unlock_args", "build_remove_args", "build_copy_args"])
def test_tampered_container_args_are_refused_before_running(tmp_path, monkeypatch, builder):
    real = getattr(workspace_module, builder)
    calls = []

    def tampered_once(*args):
        built = real(*args)
        calls.append(built)
        return built[:3] + ["--privileged"] + built[3:] if len(calls) == 1 else built

    monkeypatch.setattr(workspace_module, builder, tampered_once)
    runner = FakeRunner()
    root = make_root(tmp_path, runner)
    run_dir, ws = root.new_run("run")
    with pytest.raises(ValueError):
        if builder == "build_copy_args":
            root.copy_from(run_dir, tree(root))
        else:
            ws.mkdir()
            root.remove(run_dir)
    assert all("--privileged" not in call[0] for call in runner.calls)


def test_run_kind_of_a_missing_run_dir_is_a_value_error(tmp_path):
    root = make_root(tmp_path)
    with pytest.raises(ValueError):
        root.run_kind(root.path / "runs" / "run-0123456789abcdef")


@pytest.mark.parametrize("method", ["repo_dir", "pristine_dir"])
def test_an_existing_keyed_dir_with_the_wrong_mode_is_refused(tmp_path, method):
    root = make_root(tmp_path)
    path = getattr(root, method)(f"x-{SHA}")
    path.chmod(0o700)
    with pytest.raises(ValueError, match="mode"):
        getattr(root, method)(f"x-{SHA}")


@pytest.mark.parametrize("method", ["repo_dir", "pristine_dir"])
def test_an_existing_keyed_dir_owned_by_someone_else_is_refused(tmp_path, monkeypatch, method):
    root = make_root(tmp_path)
    path = getattr(root, method)(f"x-{SHA}")
    monkeypatch.setattr(workspace_module.os, "getuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(ValueError, match="owned"):
        getattr(root, method)(f"x-{SHA}")


def test_the_root_has_a_private_mirrors_directory(tmp_path):
    root = make_root(tmp_path)
    assert mode(root.mirrors) == 0o700


def test_a_mirror_directory_is_keyed_private_and_owned(tmp_path):
    root = make_root(tmp_path)
    path = root.mirror_dir("apache_struts")
    assert path == root.mirrors / "apache_struts"
    assert mode(path) == 0o700
    for key in ("../x", "Apache_Struts", "", "a/b", "fixture-x"):
        with pytest.raises(ValueError):
            root.mirror_dir(key)


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/Apache/Struts", "apache_struts"),
        ("https://github.com/o-1/r.js_x-y", "o-1_r.js_x-y"),
        ("https://github.com/fixture/x", "fixture_x"),
    ],
)
def test_project_slugs_are_case_folded_with_an_unambiguous_separator(url, slug):
    assert project_slug(url) == slug


def test_a_long_repository_name_is_hashed_into_the_slug():
    url = "https://github.com/owner/" + "r" * 100
    slug = project_slug(url)
    expected = hashlib.sha256(b"owner/" + b"r" * 100).hexdigest()[:16]
    assert slug == f"owner_{expected}"
    assert PROJECT_KEY_RE.fullmatch(corpus_key(url, "a" * 40))


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/o/r",
        "https://github.com/o",
        "https://github.com/o/r/pull/1",
        "https://github.com/o/..",
        "https://github.com/-o/r",
        "file:///tmp/r",
    ],
)
def test_other_project_urls_are_refused(url):
    with pytest.raises(ValueError):
        project_slug(url)


def test_a_corpus_key_is_the_slug_and_the_commit():
    assert corpus_key("https://github.com/Apache/Struts", "b" * 40) == "apache_struts-" + "b" * 40


@pytest.mark.parametrize("sha", ["B" * 40, "b" * 39, "main"])
def test_a_corpus_key_needs_a_full_lowercase_commit(sha):
    with pytest.raises(ValueError):
        corpus_key("https://github.com/o/r", sha)


def test_a_corpus_key_never_takes_the_fixture_prefix(monkeypatch):
    monkeypatch.setattr(workspace_module, "project_slug", lambda url: "fixture-x")
    with pytest.raises(ValueError, match="fixture"):
        corpus_key("https://github.com/o/r", "b" * 40)
