import atexit
import inspect
import json
import pyexpat
import shutil
import tempfile
from pathlib import Path

import pytest

import rootflake
from rootflake.sandbox import measure
from rootflake.sandbox.container import RUNTIME, RunSpec, SandboxLimits, build_docker_args
from rootflake.sandbox.download import check_proxy_args
from rootflake.sandbox.executor import ProcResult, run_test
from rootflake.sandbox.measure import (
    RuntimeOverride,
    collect_versions,
    measure_runtimes,
    nearest_rank,
    resolve_fixture,
    summarize,
)
from rootflake.sandbox.results import TestId
from rootflake.sandbox.workspace import ManagedRoot

IMAGE = "rootflake-sandbox@sha256:" + "a" * 64
PROXY = "rootflake-proxy@sha256:" + "b" * 64
REAL_FIXTURES_DIR = measure.FIXTURES_DIR
ROOT = Path(tempfile.mkdtemp(prefix="rootflake-measure-")).resolve()
atexit.register(shutil.rmtree, ROOT, ignore_errors=True)
(ROOT / "ws").mkdir()
(ROOT / "m2").mkdir()
TEST = TestId.parse("demo.DemoTest#adds")
PASS = b'<testsuite><testcase classname="demo.DemoTest" name="adds"/></testsuite>'


def spec():
    return RunSpec(
        image=IMAGE,
        workspace=ROOT / "ws",
        maven_repo=ROOT / "m2",
        jdk=17,
        command=["placeholder"],
        name="rootflake-00000000",
    )


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeRunner:
    """Takes `durations[runtime]` seconds per call on the fake clock, in call order."""

    def __init__(self, clock, durations, results=None):
        self.clock = clock
        self.durations = {k: list(v) for k, v in durations.items()}
        self.results = results or {}
        self.runtimes = []
        self.stopped = []

    def run(self, args, timeout_s):
        runtime = args[args.index("--runtime") + 1]
        self.runtimes.append(runtime)
        self.clock.now += self.durations[runtime].pop(0)
        queue = self.results.get(runtime)
        return queue.pop(0) if queue else ProcResult(0, b"", b"", False)

    def stop(self, name):
        self.stopped.append(name)


class CopyFake:
    """Does on the host what the copy, unlock and remove containers would do."""

    def __init__(self, fail_copy=False):
        self.fail_copy = fail_copy
        self.calls = []

    def run(self, args, timeout_s):
        self.calls.append(args)
        mounts = dict(
            reversed(m.split(",")[1:3])
            for m in (args[i + 1] for i, a in enumerate(args) if a == "--mount")
        )
        dst = Path(mounts["dst=/dst"].removeprefix("src="))
        if "cp" in args:
            if self.fail_copy:
                return ProcResult(1, b"", b"cp: failed", False)
            shutil.copytree(
                Path(mounts["dst=/src"].removeprefix("src=")), dst / "ws", symlinks=True
            )
        elif "find" in args:
            for child in dst.iterdir():
                shutil.rmtree(child)
        return ProcResult(0, b"", b"", False)

    def stop(self, name):
        pass


def make_fixtures(tmp_path):
    fixtures = tmp_path / "fixtures"
    demo = fixtures / "demo"
    (demo / "src").mkdir(parents=True)
    (demo / "pom.xml").write_text("<project/>")
    (demo / "src" / "DemoTest.java").write_text("class DemoTest {}")
    return fixtures


@pytest.fixture(autouse=True)
def fixtures(tmp_path, monkeypatch):
    path = make_fixtures(tmp_path)
    monkeypatch.setattr(measure, "FIXTURES_DIR", path)
    return path


class DownloadFake:
    """Plays the docker side of download_dependencies and records the fetch container."""

    def __init__(self, exit_code=0):
        self.exit_code = exit_code
        self.proxy = None
        self.fetches = []

    def docker(self, argv):
        if argv[:3] == ["docker", "network", "inspect"]:
            return ProcResult(0, self.proxy.encode(), b"", False)
        return ProcResult(0, b"", b"", False)

    def start_proxy(self, args, name, egress, image):
        check_proxy_args(args, name, egress, image)
        self.proxy = name
        return ProcResult(0, b"", b"", False)

    def run(self, args, timeout_s):
        self.fetches.append(args)
        return ProcResult(self.exit_code, b"", b"", False)

    def stop(self, name):
        pass


def mount_source(args, dst):
    for i, arg in enumerate(args):
        if arg == "--mount" and f"dst={dst}" in args[i + 1]:
            return Path(args[i + 1].split(",")[1].removeprefix("src="))
    raise AssertionError(f"no mount for {dst}")


def make_root(tmp_path, copier=None):
    path = tmp_path / "root"
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return ManagedRoot(path, IMAGE, copier or CopyFake())


def always_pass(workspace, relative, stale=None):
    return PASS


def fixed_versions(spec):
    return {"stub": "1"}


def go(tmp_path, runner, clock, runs, root=None, fixture="demo", download=None, **kwargs):
    root = root or make_root(tmp_path)
    return measure_runtimes(
        fixture,
        root,
        17,
        SandboxLimits(),
        TEST,
        "",
        runs,
        proxy_image=PROXY,
        download_ops=download or DownloadFake(),
        runner=runner,
        read_report=always_pass,
        clock=clock,
        versions=fixed_versions,
        **kwargs,
    )


def test_runtimes_alternate_order_each_round(tmp_path):
    clock = Clock()
    runner = FakeRunner(clock, {"runc": [1] * 3, "runsc": [1] * 3})
    go(tmp_path, runner, clock, 3)
    assert runner.runtimes == ["runc", "runsc", "runsc", "runc", "runc", "runsc"]


def test_per_runtime_statistics_come_from_the_measured_times(tmp_path):
    clock = Clock()
    runner = FakeRunner(clock, {"runc": [2, 4, 3, 1], "runsc": [5, 7, 6, 8]})
    result = go(tmp_path, runner, clock, 4)
    runc, runsc = result["runtimes"]["runc"], result["runtimes"]["runsc"]
    assert runc == {
        "runs": 4,
        "outcomes": {"passed": 4},
        "retries": 0,
        "timed_runs": 4,
        "excluded_from_timing": 0,
        "median_s": 2.5,
        "p90_s": 4.0,
        "total_s": 10.0,
    }
    assert (runsc["median_s"], runsc["p90_s"], runsc["total_s"]) == (6.5, 8.0, 26.0)
    assert result["ratio_median_runsc_over_runc"] == 2.6
    assert result["versions"] == {"stub": "1"}
    assert result["fixture"] == "demo"
    assert len(result["runs"]) == 8
    assert result["runs"][0] == {
        "round": 0,
        "runtime": "runc",
        "outcome": "passed",
        "attempts": 1,
        "exit_code": 0,
        "seconds": 2.0,
    }


def test_the_median_is_not_the_mean(tmp_path):
    clock = Clock()
    runner = FakeRunner(clock, {"runc": [1, 1, 10], "runsc": [2, 2, 20]})
    result = go(tmp_path, runner, clock, 3)
    assert result["runtimes"]["runc"]["median_s"] == 1.0
    assert result["runtimes"]["runsc"]["median_s"] == 2.0


def test_retried_runs_are_counted_but_not_timed(tmp_path):
    clock = Clock()
    infra = ProcResult(137, b"", b"", False)
    runner = FakeRunner(
        clock,
        {"runc": [1, 1], "runsc": [3, 4, 5, 6]},
        results={"runsc": [infra, ProcResult(0, b"", b"", False), infra, infra]},
    )
    result = go(tmp_path, runner, clock, 2, timeout_s=60)
    runsc = result["runtimes"]["runsc"]
    assert runsc["retries"] == 2
    assert runsc["outcomes"] == {"infrastructure": 1, "passed": 1}
    assert (runsc["timed_runs"], runsc["excluded_from_timing"]) == (0, 2)
    assert runsc["median_s"] is None and runsc["total_s"] is None
    assert result["ratio_median_runsc_over_runc"] is None
    assert result["runtimes"]["runc"]["excluded_from_timing"] == 0


def test_the_ratio_uses_unrounded_medians(tmp_path):
    clock = Clock()
    runner = FakeRunner(clock, {"runc": [1.0004], "runsc": [2.0008]})
    result = go(tmp_path, runner, clock, 1)
    assert result["runtimes"]["runsc"]["median_s"] == 2.001
    assert result["ratio_median_runsc_over_runc"] == 2.0


def test_a_zero_runc_median_gives_no_ratio(tmp_path):
    clock = Clock()
    runner = FakeRunner(clock, {"runc": [0, 0], "runsc": [1, 1]})
    assert go(tmp_path, runner, clock, 2)["ratio_median_runsc_over_runc"] is None


def test_the_ratio_needs_both_runtimes(tmp_path):
    clock = Clock()
    runner = FakeRunner(clock, {"runc": [1, 2]})
    result = go(tmp_path, runner, clock, 2, runtimes=("runc",))
    assert result["ratio_median_runsc_over_runc"] is None
    assert list(result["runtimes"]) == ["runc"]


@pytest.mark.parametrize(
    ("n", "rank"), [(1, 1), (9, 9), (10, 9), (11, 10), (20, 18), (50, 45), (70, 63)]
)
def test_p90_is_the_nearest_rank(n, rank):
    assert nearest_rank(list(range(1, n + 1)), 90) == rank


def test_summarize_rounds_to_milliseconds():
    rows = [{"outcome": "passed", "attempts": 1, "seconds": s} for s in (1.00049, 2.0004, 3.0)]
    assert summarize(rows)["median_s"] == 2.0
    assert summarize(rows)["total_s"] == 6.001


@pytest.mark.parametrize(
    "kwargs",
    [
        {"runtimes": ("runc", "runc")},
        {"runtimes": ("runc", "kata")},
        {"runtimes": ()},
        {"runs": 0},
        {"runs": True},
        {"runs": 2.0},
    ],
)
def test_bad_parameters_are_refused(tmp_path, kwargs):
    clock = Clock()
    runs = kwargs.pop("runs", 2)
    with pytest.raises(ValueError):
        go(tmp_path, FakeRunner(clock, {}), clock, runs, **kwargs)
    assert not any((tmp_path / "root" / "runs").iterdir())


def test_the_fixture_is_staged_and_copied_into_a_fresh_run_dir_then_removed(tmp_path):
    clock = Clock()
    copier = CopyFake()
    root = make_root(tmp_path, copier)
    result = go(tmp_path, FakeRunner(clock, {"runc": [1], "runsc": [1]}), clock, 1, root=root)
    cp = next(args for args in copier.calls if "cp" in args)
    source = Path(cp[cp.index("--mount") + 1].split(",")[1].removeprefix("src="))
    assert source.parent == root.path / "pristine"
    assert source.name.startswith("fixture-demo-")
    assert (source / "src" / "DemoTest.java").read_text() == "class DemoTest {}"
    assert result["cleanup_errors"] == []
    assert list((root.path / "runs").iterdir()) == []


def test_a_fixture_can_be_measured_twice(tmp_path):
    clock = Clock()
    root = make_root(tmp_path)
    go(tmp_path, FakeRunner(clock, {"runc": [1], "runsc": [1]}), clock, 1, root=root)
    go(tmp_path, FakeRunner(clock, {"runc": [1], "runsc": [1]}), clock, 1, root=root)


def test_a_failed_fixture_copy_stops_the_measurement_and_cleans_up(tmp_path):
    clock = Clock()
    root = make_root(tmp_path, CopyFake(fail_copy=True))
    runner = FakeRunner(clock, {"runc": [1], "runsc": [1]})
    with pytest.raises(RuntimeError, match="cp: failed"):
        go(tmp_path, runner, clock, 1, root=root)
    assert runner.runtimes == []
    assert list((root.path / "runs").iterdir()) == []


@pytest.mark.parametrize(
    "name",
    [
        "../corpus",
        "corpus/commons-lang",
        "/etc",
        "Demo",
        "demo_x",
        "",
        "-demo",
        "a" * 62,
        "missing",
    ],
)
def test_anything_but_a_fixture_name_is_refused(tmp_path, fixtures, name):
    for clean in ("corpus/commons-lang", "fixtures/Demo", "fixtures/demo_x", "fixtures/-demo"):
        (tmp_path / clean).mkdir(parents=True)
        (tmp_path / clean / "pom.xml").write_text("<project/>")
    (fixtures / ("a" * 62)).mkdir()
    with pytest.raises(ValueError):
        resolve_fixture(name)


def test_an_absolute_or_parent_path_to_a_clean_tree_is_refused(tmp_path, fixtures):
    clean = tmp_path / "corpus" / "commons-lang"
    clean.mkdir(parents=True)
    for name in (str(clean), "../corpus/commons-lang", "../corpus"):
        with pytest.raises(ValueError):
            resolve_fixture(name)


def test_an_arbitrary_path_is_never_measured(tmp_path):
    clock = Clock()
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError):
        go(tmp_path, FakeRunner(clock, {}), clock, 1, fixture=str(project))


def test_a_symlinked_fixture_is_refused(tmp_path, fixtures):
    (fixtures / "linked").symlink_to(fixtures / "demo")
    with pytest.raises(ValueError, match="symlink"):
        resolve_fixture("linked")


def test_a_symlink_inside_a_fixture_is_refused(tmp_path, fixtures):
    (fixtures / "demo" / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        resolve_fixture("demo")


def test_a_valid_fixture_resolves_inside_the_fixtures_dir(tmp_path, fixtures):
    assert resolve_fixture("demo") == fixtures / "demo"


def test_the_default_fixtures_dir_is_in_this_repository():
    repository = Path(__file__).resolve().parents[2]
    assert REAL_FIXTURES_DIR == repository / "tests" / "fixtures" / "maven"


class Recorder:
    def __init__(self):
        self.calls = []
        self.stopped = []

    def run(self, args, timeout_s):
        self.calls.append(args)
        return ProcResult(0, b"", b"", False)

    def stop(self, name):
        self.stopped.append(name)


def checked_args():
    return build_docker_args(spec(), SandboxLimits())


@pytest.mark.parametrize("runtime", ["runc", "runsc"])
def test_override_swaps_only_the_runtime_value(runtime):
    inner = Recorder()
    args = checked_args()
    RuntimeOverride(inner, runtime).run(args, 10)
    (sent,) = inner.calls
    i = args.index("--runtime")
    assert sent[: i + 1] == args[: i + 1] and sent[i + 2 :] == args[i + 2 :]
    assert sent[i + 1] == runtime


@pytest.mark.parametrize("runtime", ["kata", "io.containerd.runc.v2", "", "RUNC", "runc "])
def test_override_accepts_only_runc_or_runsc(runtime):
    with pytest.raises(ValueError):
        RuntimeOverride(Recorder(), runtime)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: [x for x in a if x != "--runtime" and x != RUNTIME],
        lambda a: [("runc" if x == RUNTIME else x) for x in a],
        lambda a: a + ["--runtime", RUNTIME],
        lambda a: a[: a.index("--runtime") + 1],
    ],
    ids=["no runtime", "not the sandbox runtime", "two runtimes", "dangling flag"],
)
def test_override_refuses_args_that_are_not_the_checked_form(mutate):
    inner = Recorder()
    with pytest.raises(ValueError):
        RuntimeOverride(inner, "runc").run(mutate(checked_args()), 10)
    assert inner.calls == []


def test_override_passes_stop_through():
    inner = Recorder()
    RuntimeOverride(inner, "runsc").stop("rootflake-0000000000")
    assert inner.stopped == ["rootflake-0000000000"]


def test_normal_runs_cannot_choose_a_runtime():
    assert "runtime" not in inspect.signature(run_test).parameters
    assert checked_args()[checked_args().index("--runtime") + 1] == "runsc"
    package = Path(rootflake.__file__).parent
    users = [
        p.relative_to(package).as_posix()
        for p in package.rglob("*.py")
        if "RuntimeOverride" in p.read_text()
    ]
    assert users == ["sandbox/measure.py"]


def test_versions_are_recorded():
    seen = []

    def command(argv):
        seen.append(argv)
        return {"runsc": "runsc version release-20261005.0", "docker": "29.1.3"}[argv[0]]

    versions = collect_versions(spec(), command=command)
    assert versions == {
        "runsc": "runsc version release-20261005.0",
        "docker": "29.1.3",
        "image": IMAGE,
        "jdk": 17,
        "expat": pyexpat.EXPAT_VERSION,
    }
    assert ["runsc", "--version"] in seen


def test_first_line_reports_an_unavailable_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert measure.first_line(["runsc", "--version"]).startswith("unavailable")


def test_first_line_keeps_only_the_first_line(tmp_path, monkeypatch):
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nprintf 'one\\ntwo\\n'\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert measure.first_line(["tool"]) == "one"


def test_main_takes_a_fixture_name_and_writes_the_json(tmp_path, monkeypatch):
    seen = {}

    def fake_measure(fixture, root, jdk, limits, test, module, runs, **kwargs):
        seen.update(fixture=fixture, root=root, jdk=jdk, test=test)
        seen.update(module=module, runs=runs, **kwargs)
        return {"ratio_median_runsc_over_runc": 1.5}

    monkeypatch.setattr(measure, "measure_runtimes", fake_measure)
    root = make_root(tmp_path)
    out = tmp_path / "result.json"
    measure.main(
        [
            "--image", IMAGE,
            "--fixture", "demo",
            "--root-dir", str(root.path),
            "--proxy-image", PROXY,
            "--test", "demo.DemoTest#adds",
            "--module", "core",
            "--jdk", "11",
            "--runs", "50",
            "--timeout", "300",
            str(out),
        ]
    )  # fmt: skip
    assert json.loads(out.read_text()) == {"ratio_median_runsc_over_runc": 1.5}
    assert seen["fixture"] == "demo" and seen["root"].path == root.path
    assert seen["root"].image == IMAGE and seen["jdk"] == 11
    assert (seen["test"], seen["module"], seen["runs"]) == (TEST, "core", 50)
    assert seen["timeout_s"] == 300 and seen["proxy_image"] == PROXY


@pytest.mark.parametrize("option", ["--workspace", "--fixtures-dir", "--maven-repo"])
def test_the_command_line_has_no_way_to_point_at_other_trees(tmp_path, monkeypatch, option):
    called = []
    monkeypatch.setattr(measure, "measure_runtimes", lambda *a, **k: called.append(a) or {})
    root = make_root(tmp_path)
    valid = [
        "--image", IMAGE,
        "--fixture", "demo",
        "--root-dir", str(root.path),
        "--proxy-image", PROXY,
        "--test", "demo.DemoTest#adds",
    ]  # fmt: skip
    measure.main([*valid, str(tmp_path / "ok.json")])
    assert len(called) == 1
    with pytest.raises(SystemExit):
        measure.main([*valid, option, str(tmp_path), str(tmp_path / "x.json")])
    assert len(called) == 1


def test_a_missing_fixtures_directory_is_a_value_error(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "FIXTURES_DIR", tmp_path / "absent")
    with pytest.raises(ValueError, match="fixtures"):
        resolve_fixture("demo")


def staged(root):
    (path,) = (root.path / "pristine").iterdir()
    return path


def test_the_repository_is_derived_from_the_fixture_and_filled_by_its_download(tmp_path):
    clock = Clock()
    download = DownloadFake()
    runner = FakeRunner(clock, {"runc": [1], "runsc": [1]})
    root = make_root(tmp_path)
    result = go(tmp_path, runner, clock, 1, root=root, download=download)
    key = staged(root).name
    assert key.startswith("fixture-demo-")
    repo = root.path / "repos" / key
    (fetch,) = download.fetches
    assert fetch[-1] == "rootflake-fetch"
    assert fetch[fetch.index("--runtime") + 1] == "runsc"
    assert mount_source(fetch, "/m2/repo") == repo
    assert mount_source(fetch, "/workspace").parent.name.startswith("download-")
    assert result["download"]["outcome"] == "ok"
    assert result["download"]["cleanup_errors"] == []
    assert list((root.path / "runs").iterdir()) == []


def test_measured_runs_load_only_the_derived_repository(tmp_path):
    clock = Clock()
    seen = []

    class Watch(FakeRunner):
        def run(self, args, timeout_s):
            seen.append(mount_source(args, "/m2/tail"))
            return super().run(args, timeout_s)

    root = make_root(tmp_path)
    go(tmp_path, Watch(clock, {"runc": [1], "runsc": [1]}), clock, 1, root=root)
    assert seen == [root.path / "repos" / staged(root).name] * 2


def test_a_repository_under_any_other_key_is_refused(tmp_path, monkeypatch):
    clock = Clock()
    root = make_root(tmp_path)
    other = root.repo_dir("commons-lang-" + "1" * 40)
    monkeypatch.setattr(root, "repo_dir", lambda key: other)
    download = DownloadFake()
    runner = FakeRunner(clock, {"runc": [1], "runsc": [1]})
    with pytest.raises(ValueError, match="repository"):
        go(tmp_path, runner, clock, 1, root=root, download=download)
    assert download.fetches == [] and runner.runtimes == []


def test_a_failed_download_stops_the_measurement(tmp_path):
    clock = Clock()
    root = make_root(tmp_path)
    runner = FakeRunner(clock, {"runc": [1], "runsc": [1]})
    with pytest.raises(RuntimeError, match="download"):
        go(tmp_path, runner, clock, 1, root=root, download=DownloadFake(exit_code=1))
    assert runner.runtimes == []
    assert list((root.path / "runs").iterdir()) == []


@pytest.mark.parametrize("keyword", ["maven_repo", "fixtures_dir"])
def test_callers_cannot_supply_a_repository_or_fixtures_dir(tmp_path, keyword):
    clock = Clock()
    with pytest.raises(TypeError):
        go(tmp_path, FakeRunner(clock, {}), clock, 1, **{keyword: tmp_path})
    with pytest.raises(TypeError):
        resolve_fixture("demo", tmp_path)


def test_a_staged_fixture_with_matching_content_is_reused(tmp_path, monkeypatch):
    clock = Clock()
    root = make_root(tmp_path)
    go(tmp_path, FakeRunner(clock, {"runc": [1], "runsc": [1]}), clock, 1, root=root)

    def no_copy(*args, **kwargs):
        raise AssertionError("a matching staged fixture must not be copied again")

    monkeypatch.setattr(measure, "copytree", no_copy)
    go(tmp_path, FakeRunner(clock, {"runc": [1], "runsc": [1]}), clock, 1, root=root)


def test_a_staged_fixture_with_a_leftover_file_is_refused(tmp_path):
    clock = Clock()
    root = make_root(tmp_path)
    go(tmp_path, FakeRunner(clock, {"runc": [1], "runsc": [1]}), clock, 1, root=root)
    (staged(root) / "leftover.txt").write_text("x")
    runner = FakeRunner(clock, {"runc": [1], "runsc": [1]})
    with pytest.raises(ValueError, match="staged"):
        go(tmp_path, runner, clock, 1, root=root)
    assert runner.runtimes == []


def test_a_symlink_added_between_the_check_and_the_copy_is_caught(tmp_path, fixtures, monkeypatch):
    clock = Clock()
    root = make_root(tmp_path)
    real_copytree = shutil.copytree

    def racing_copytree(src, dst, **kwargs):
        (Path(src) / "escape").symlink_to(tmp_path)
        return real_copytree(src, dst, **kwargs)

    monkeypatch.setattr(measure, "copytree", racing_copytree)
    runner = FakeRunner(clock, {"runc": [1], "runsc": [1]})
    with pytest.raises(ValueError, match="symlink"):
        go(tmp_path, runner, clock, 1, root=root)
    assert runner.runtimes == []
    assert list((root.path / "pristine").iterdir()) == []
