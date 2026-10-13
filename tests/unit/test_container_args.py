import tempfile
from pathlib import Path

import pytest

from rootflake.sandbox.container import (
    FORBIDDEN_FLAGS,
    RunSpec,
    SandboxLimits,
    build_docker_args,
    check_args,
)

IMAGE = "rootflake-sandbox@sha256:" + "a" * 64
BASE = Path(tempfile.mkdtemp(prefix="rootflake-test-")).resolve()
ROOT = BASE / "managed"
for sub in ("runs/p1/ws", "m2/p1", "x"):
    (ROOT / sub).mkdir(parents=True, exist_ok=True)
ROOTS = (ROOT,)


def spec(**overrides):
    values = dict(
        image=IMAGE,
        workspace=Path(f"{ROOT}/runs/p1/ws"),
        maven_repo=Path(f"{ROOT}/m2/p1"),
        jdk=17,
        command=["mvn", "test"],
        name="rootflake-00000000",
    )
    values.update(overrides)
    return RunSpec(**values)


def value_after(args, flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag]


def env_of(args):
    return dict(e.split("=", 1) for e in value_after(args, "--env"))


def test_hardening_flags_are_always_present():
    args = build_docker_args(spec(), SandboxLimits())
    for flag in ["--rm", "--read-only", "--init"]:
        assert flag in args
    assert value_after(args, "--network") == ["none"]
    assert value_after(args, "--cap-drop") == ["ALL"]
    assert value_after(args, "--security-opt") == ["no-new-privileges=true"]
    assert value_after(args, "--user") == ["10001:10001"]
    assert value_after(args, "--runtime") == ["runsc"]


def test_limits_come_from_sandbox_limits():
    args = build_docker_args(spec(), SandboxLimits(memory_mib=2048, cpus=1, pids=256))
    assert value_after(args, "--memory") == ["2048m"]
    assert value_after(args, "--memory-swap") == ["2048m"]
    assert value_after(args, "--cpus") == ["1"]
    assert value_after(args, "--pids-limit") == ["256"]
    assert value_after(args, "--ulimit") == ["nofile=4096:4096"]


def test_tmp_is_executable_because_native_libraries_load_from_it():
    tmpfs = value_after(build_docker_args(spec(), SandboxLimits()), "--tmpfs")
    tmp = next(t for t in tmpfs if t.startswith("/tmp:"))
    options = tmp.split(":", 1)[1].split(",")
    assert "exec" in options and "nosuid" in options and "nodev" in options
    assert "noexec" not in options


def test_exactly_two_bind_mounts_and_the_repository_is_read_only():
    mounts = value_after(build_docker_args(spec(), SandboxLimits()), "--mount")
    assert mounts == [
        f"type=bind,src={ROOT}/runs/p1/ws,dst=/workspace",
        f"type=bind,src={ROOT}/m2/p1,dst=/m2/tail,readonly",
    ]


def test_image_must_be_pinned_by_digest():
    with pytest.raises(ValueError, match="digest"):
        build_docker_args(spec(image="rootflake-sandbox:dev"), SandboxLimits())


def test_image_and_command_come_last():
    args = build_docker_args(spec(command=["mvn", "-q", "test"]), SandboxLimits())
    assert args[-4:] == [IMAGE, "mvn", "-q", "test"]


@pytest.mark.parametrize("jdk", [7, 9, 21])
def test_unsupported_jdk_is_rejected(jdk):
    with pytest.raises(ValueError, match="JDK"):
        build_docker_args(spec(jdk=jdk), SandboxLimits())


def test_env_carries_jdk_and_caps_jvm_memory_explicitly():
    env = env_of(build_docker_args(spec(jdk=8), SandboxLimits(memory_mib=4096)))
    assert env["ROOTFLAKE_JDK"] == "8"
    assert "-XX:MaxRAM=4096m" in env["JAVA_TOOL_OPTIONS"].split()
    assert "-Xmx768m" in env["MAVEN_OPTS"].split()


def test_maven_runs_offline_with_a_writable_head_and_read_only_tails():
    maven_args = env_of(build_docker_args(spec(), SandboxLimits()))["MAVEN_ARGS"].split()
    assert "--offline" in maven_args
    assert "-Dmaven.repo.local=/tmp/m2" in maven_args
    assert "-Dmaven.repo.local.tail=/m2/tail,/opt/m2-tail" in maven_args


def test_time_zone_sets_tz_and_user_timezone():
    env = env_of(build_docker_args(spec(timezone="Asia/Tokyo"), SandboxLimits()))
    assert env["TZ"] == "Asia/Tokyo"
    assert "-Duser.timezone=Asia/Tokyo" in env["JAVA_TOOL_OPTIONS"].split()


def test_no_time_zone_means_no_tz_variable():
    assert "TZ" not in env_of(build_docker_args(spec(), SandboxLimits()))


@pytest.mark.parametrize("zone", ["../etc/passwd", "Asia/Tokyo -XX:+X", "", "a" * 80])
def test_malformed_time_zone_is_rejected(zone):
    with pytest.raises(ValueError, match="zone"):
        build_docker_args(spec(timezone=zone), SandboxLimits())


def test_every_env_entry_has_an_explicit_value():
    for entry in value_after(build_docker_args(spec(), SandboxLimits()), "--env"):
        assert "=" in entry


def test_built_args_pass_the_check():
    check_args(build_docker_args(spec(timezone="UTC"), SandboxLimits()), ROOTS, IMAGE)


@pytest.mark.parametrize("flag", sorted(FORBIDDEN_FLAGS))
def test_check_rejects_every_forbidden_flag(flag):
    args = build_docker_args(spec(), SandboxLimits())
    with pytest.raises(ValueError, match="forbidden"):
        check_args(args[:2] + [flag] + args[2:], ROOTS, IMAGE)


@pytest.mark.parametrize(
    "extra",
    [
        ["--network", "host"],
        ["--network", "bridge"],
        ["--security-opt", "seccomp=unconfined"],
        ["--security-opt", "apparmor=unconfined"],
        ["--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock"],
        ["--mount", "type=bind,src=/home/user/.config/gcloud,dst=/x"],
        ["-v", "/home:/home"],
        ["--runtime", "runc"],
        ["--env", "HOME"],
    ],
)
def test_check_rejects_unsafe_values(extra):
    args = build_docker_args(spec(), SandboxLimits())
    with pytest.raises(ValueError):
        check_args(args[:2] + extra + args[2:], ROOTS, IMAGE)


def test_check_rejects_missing_hardening_flag():
    args = build_docker_args(spec(), SandboxLimits())
    args.remove("--read-only")
    with pytest.raises(ValueError, match="--read-only"):
        check_args(args, ROOTS, IMAGE)


def args_with(*extra):
    args = build_docker_args(spec(), SandboxLimits())
    return args[:2] + list(extra) + args[2:]


def replaced(old, new):
    args = build_docker_args(spec(), SandboxLimits())
    return [new if a == old else a for a in args]


@pytest.mark.parametrize(
    "extra",
    [
        ["--read-only=false"],
        ["--rm=false"],
        ["--init=false"],
        ["--name=", "--privileged"],
        ["--workdir=", "--volume=/:/host"],
        ["--name=", "--cap-add=SYS_ADMIN"],
        ["--name=x"],
    ],
    ids=str,
)
def test_inline_values_and_empty_values_cannot_smuggle_flags(extra):
    with pytest.raises(ValueError):
        check_args(args_with(*extra), ROOTS, IMAGE)


@pytest.mark.parametrize(
    "mount",
    [
        "type=bind,src=/,dst=/workspace,target=/host",
        "type=bind,src=/,dst=/workspace,destination=/host",
        "type=bind,src=/,dst=/workspace,DST=/host",
        'type=bind,src=/,dst=/workspace,"dst=/host,x"',
        f"type=bind,src={ROOT}/m2/p1,dst=/m2/tail,readonly,readonly=false",
        "type=bind,src=/var/run/docker.sock,dst=/workspace",
        "type=bind,src=/,dst=/workspace",
        f"type=bind,src={ROOT}/../../etc,dst=/workspace",
        f"type=bind,src={ROOT}/runs/p1/ws,dst=/workspace,bind-propagation=shared",
        "type=volume,src=/srv/rootflake/runs/p1/ws,dst=/workspace",
        f"type=bind,src={ROOT}/m2/p1,dst=/m2/tail",
    ],
)
def test_mount_values_are_parsed_strictly(mount):
    args = build_docker_args(spec(), SandboxLimits())
    mounts = [i + 1 for i, a in enumerate(args) if a == "--mount"]
    target = mounts[1] if "/m2/tail" in mount else mounts[0]
    args[target] = mount
    with pytest.raises(ValueError):
        check_args(args, ROOTS, IMAGE)


def test_a_third_mount_is_rejected():
    with pytest.raises(ValueError):
        check_args(args_with("--mount", f"type=bind,src={ROOT}/x,dst=/workspace"), ROOTS, IMAGE)


@pytest.mark.parametrize(
    "workspace",
    [
        Path(f"{ROOT}/ws,src=/var/run/docker.sock"),
        Path(f'{ROOT}/ws"x'),
        Path(f"{ROOT}/ws\nx"),
        Path("relative/ws"),
        Path(f"{ROOT}/a/../../../etc"),
    ],
)
def test_builder_rejects_unsafe_paths(workspace):
    with pytest.raises(ValueError):
        build_docker_args(spec(workspace=workspace), SandboxLimits())


def test_mount_source_outside_the_managed_roots_is_rejected():
    outside = BASE / "outside"
    outside.mkdir(exist_ok=True)
    args = build_docker_args(spec(workspace=outside), SandboxLimits())
    with pytest.raises(ValueError, match="outside the managed roots"):
        check_args(args, ROOTS, IMAGE)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("4096m", "0"),
        ("1024", "-1"),
        ("2", "0"),
        ("10", "-1"),
        ("max-size=10m", "max-size=-1"),
        ("nofile=4096:4096", "nproc=-1"),
        ("/tmp:rw,exec,nosuid,nodev,size=1g", "/etc:rw,exec"),
        ("/tmp:rw,exec,nosuid,nodev,size=1g", "/tmp:rw,exec,nosuid,nodev,size=1g,suid,dev"),
        ("10001:10001", "0:0"),
        ("ALL", "NET_RAW"),
        ("/workspace", "/"),
    ],
)
def test_limit_and_identity_values_are_checked(old, new):
    with pytest.raises(ValueError):
        check_args(replaced(old, new), ROOTS, IMAGE)


def test_memory_swap_must_equal_memory():
    args = build_docker_args(spec(), SandboxLimits())
    args[args.index("--memory-swap") + 1] = "8192m"
    with pytest.raises(ValueError):
        check_args(args, ROOTS, IMAGE)


@pytest.mark.parametrize(
    "flag", ["--tmpfs", "--log-opt", "--ulimit", "--stop-timeout", "--mount", "--workdir"]
)
def test_removing_any_required_flag_is_rejected(flag):
    args = build_docker_args(spec(), SandboxLimits())
    i = args.index(flag)
    del args[i : i + 2]
    with pytest.raises(ValueError):
        check_args(args, ROOTS, IMAGE)


@pytest.mark.parametrize(
    "limits",
    [
        dict(memory_mib=0),
        dict(pids=-1),
        dict(cpus=0),
        dict(stop_timeout_s=-1),
        dict(tmp_size="1g,suid,dev"),
        dict(home_size="0"),
        dict(log_max_mib=-1),
        dict(nofile=0),
    ],
)
def test_sandbox_limits_are_validated(limits):
    with pytest.raises(ValueError):
        SandboxLimits(**limits)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("none", "host"),
        ("no-new-privileges=true", "seccomp=unconfined"),
        ("10", "0"),
    ],
)
def test_replacing_a_hardening_value_is_rejected(old, new):
    with pytest.raises(ValueError):
        check_args(replaced(old, new), ROOTS, IMAGE)


def test_duplicate_boolean_flag_is_rejected():
    with pytest.raises(ValueError, match="--rm"):
        check_args(args_with("--rm"), ROOTS, IMAGE)


def test_mount_source_equal_to_a_root_is_rejected():
    args = build_docker_args(spec(workspace=ROOT), SandboxLimits())
    with pytest.raises(ValueError, match="root"):
        check_args(args, ROOTS, IMAGE)


def test_mount_source_through_a_symlink_is_rejected():
    link = ROOT / "runs" / "link"
    if not link.exists():
        link.symlink_to(BASE)
    args = build_docker_args(spec(workspace=link), SandboxLimits())
    with pytest.raises(ValueError, match="symlink|outside"):
        check_args(args, ROOTS, IMAGE)


def test_missing_mount_source_is_rejected():
    args = build_docker_args(spec(workspace=ROOT / "runs" / "absent"), SandboxLimits())
    with pytest.raises(ValueError):
        check_args(args, ROOTS, IMAGE)


@pytest.mark.parametrize("roots", [(Path("/"),), (Path("relative"),), ()])
def test_unsafe_roots_are_rejected(roots):
    with pytest.raises(ValueError, match="root"):
        check_args(build_docker_args(spec(), SandboxLimits()), roots, IMAGE)


def test_a_different_image_than_expected_is_rejected():
    other = "rootflake-sandbox@sha256:" + "b" * 64
    with pytest.raises(ValueError, match="image"):
        check_args(build_docker_args(spec(image=other), SandboxLimits()), ROOTS, IMAGE)


def test_jvms_are_told_the_cpu_count():
    env = env_of(build_docker_args(spec(), SandboxLimits(cpus=2)))
    assert "-XX:ActiveProcessorCount=2" in env["JAVA_TOOL_OPTIONS"].split()


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("JAVA_TOOL_OPTIONS", "-javaagent:/workspace/a.jar -XX:MaxRAM=4096m"),
        ("JAVA_TOOL_OPTIONS", "-XX:MaxRAM=16384m -XX:ActiveProcessorCount=2"),
        ("MAVEN_ARGS", "--batch-mode"),
        ("MAVEN_ARGS", "-Dmaven.repo.local=/workspace/m2"),
        ("MAVEN_OPTS", "-Xmx16g"),
        ("HOME", "/workspace"),
        ("ROOTFLAKE_MAX_PROCS", "999999"),
        ("ROOTFLAKE_JDK", "21"),
    ],
)
def test_computed_env_values_are_checked_exactly(key, bad):
    args = build_docker_args(spec(), SandboxLimits())
    i = next(i for i, a in enumerate(args) if a.startswith(f"{key}="))
    args[i] = f"{key}={bad}"
    with pytest.raises(ValueError, match=key):
        check_args(args, ROOTS, IMAGE)


@pytest.mark.parametrize("zone", ["../../x", "UTC -Dx=y"])
def test_tz_value_is_checked(zone):
    args = build_docker_args(spec(timezone="UTC"), SandboxLimits())
    args[args.index("TZ=UTC")] = f"TZ={zone}"
    with pytest.raises(ValueError, match="TZ|zone"):
        check_args(args, ROOTS, IMAGE)


def test_tz_without_matching_user_timezone_is_rejected():
    args = build_docker_args(spec(timezone="UTC"), SandboxLimits())
    args[args.index("TZ=UTC")] = "TZ=Asia/Tokyo"
    with pytest.raises(ValueError):
        check_args(args, ROOTS, IMAGE)


def test_missing_computed_env_key_is_rejected():
    args = build_docker_args(spec(), SandboxLimits())
    i = next(i for i, a in enumerate(args) if a.startswith("MAVEN_OPTS="))
    del args[i - 1 : i + 1]
    with pytest.raises(ValueError, match="MAVEN_OPTS"):
        check_args(args, ROOTS, IMAGE)


def test_jdk_must_be_an_int():
    with pytest.raises(ValueError, match="JDK"):
        build_docker_args(spec(jdk=8.0), SandboxLimits())
