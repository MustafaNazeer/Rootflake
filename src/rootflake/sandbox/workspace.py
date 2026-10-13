"""Run copies of a pristine checkout, made and deleted inside short lived containers.

Everything lives under one managed root: `pristine/` holds the host's checkouts, `runs/`
holds one directory per copy (a download copy and a test run copy are never the same
directory), and `repos/` holds one Maven repository per project and commit. Copies are made
with `cp -a` and deleted with `chmod -R` then `find -delete`, each from the sandbox image
running as UID 10001, so the files belong to the sandbox user and the host never needs root.
`cp -a` copies symlinks as symlinks, and `chmod -R` and `find` never follow them, so a link
in a target project cannot reach outside its copy.
"""

import os
import re
import secrets
import stat
from pathlib import Path

from rootflake.sandbox.container import DIGEST_RE, NAME_RE, SAFE_PATH_RE, SANDBOX_USER
from rootflake.sandbox.executor import ProcessRunner, result_output

# runc, not runsc: these containers run only the image's own cp and find, never target code.
RUNTIME = "runc"
COPY_TIMEOUT_S = 900
KINDS = ("download", "run")
RUN_DIR_RE = re.compile(r"(download|run)-[0-9a-f]{16}")
PROJECT_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,80}-[0-9a-f]{40}")


def default_root() -> Path:
    return Path.home() / ".local" / "share" / "rootflake"


def _plain(name: str, path: Path) -> str:
    text = str(path)
    if not SAFE_PATH_RE.fullmatch(text) or ".." in path.parts:
        raise ValueError(f"{name} must be an absolute path of plain characters: {text!r}")
    return text


def _flags(name: str, image: str) -> list[str]:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"container name must match {NAME_RE.pattern}: {name!r}")
    if not DIGEST_RE.fullmatch(image):
        raise ValueError(f"image must be pinned by digest: {image!r}")
    return [
        "docker", "run", "--rm",
        "--name", name,
        "--runtime", RUNTIME,
        "--network", "none",
        "--user", SANDBOX_USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--read-only",
        "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1",
        "--pids-limit", "64",
        "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
    ]  # fmt: skip


def build_copy_args(name: str, image: str, pristine: Path, run_dir: Path) -> list[str]:
    source, target = _plain("pristine", pristine), _plain("run_dir", run_dir)
    return [
        *_flags(name, image),
        "--mount", f"type=bind,src={source},dst=/src,readonly",
        "--mount", f"type=bind,src={target},dst=/dst",
        image,
        "cp", "-a", "/src/.", "/dst/ws",
    ]  # fmt: skip


def build_remove_args(name: str, image: str, run_dir: Path) -> list[str]:
    target = _plain("run_dir", run_dir)
    return [
        *_flags(name, image),
        "--mount", f"type=bind,src={target},dst=/dst",
        image,
        "find", "/dst", "-mindepth", "1", "-delete",
    ]  # fmt: skip


def build_unlock_args(name: str, image: str, run_dir: Path) -> list[str]:
    # The sandbox user may have locked directories (mode 000) that find could not enter.
    # Only ws: the run directory itself belongs to the host user.
    target = _plain("run_dir", run_dir)
    return [
        *_flags(name, image),
        "--mount", f"type=bind,src={target},dst=/dst",
        image,
        "chmod", "-R", "u+rwX", "/dst/ws",
    ]  # fmt: skip


def check_copy_args(
    args: list[str], root: ManagedRoot, name: str, image: str, pristine: Path, run_dir: Path
) -> None:
    """Raise ValueError unless `args` is exactly the copy command for managed paths."""
    root.run_kind(run_dir)
    root.check_pristine(pristine)
    if args != build_copy_args(name, image, pristine, run_dir):
        raise ValueError("copy arguments differ from the hardened form")


def check_remove_args(
    args: list[str], root: ManagedRoot, name: str, image: str, run_dir: Path
) -> None:
    """Raise ValueError unless `args` is exactly the remove command for a managed run dir."""
    root.run_kind(run_dir)
    if args != build_remove_args(name, image, run_dir):
        raise ValueError("remove arguments differ from the hardened form")


def check_unlock_args(
    args: list[str], root: ManagedRoot, name: str, image: str, run_dir: Path
) -> None:
    """Raise ValueError unless `args` is exactly the unlock command for a managed run dir."""
    root.run_kind(run_dir)
    if args != build_unlock_args(name, image, run_dir):
        raise ValueError("unlock arguments differ from the hardened form")


def _make_dir(path: Path, mode: int) -> None:
    os.mkdir(path, mode)
    os.chmod(path, mode)


def _keyed_dir(parent: Path, project_key: str, mode: int) -> Path:
    if not PROJECT_KEY_RE.fullmatch(project_key):
        raise ValueError(f"project key must match {PROJECT_KEY_RE.pattern}: {project_key!r}")
    path = parent / project_key
    if not os.path.lexists(path):
        _make_dir(path, mode)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{path} must be a real directory, not a symlink or file")
    if info.st_uid != os.getuid():
        raise ValueError(f"{path} must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != mode:
        raise ValueError(f"{path} must have mode {mode:04o}")
    return path


class ManagedRoot:
    """The one directory tree the sandbox may write to, private to the current user."""

    def __init__(self, path: Path, image: str, runner: ProcessRunner) -> None:
        if not DIGEST_RE.fullmatch(image):
            raise ValueError(f"image must be pinned by digest: {image!r}")
        if not path.is_absolute() or path == Path("/"):
            raise ValueError(f"managed root must be absolute and not /: {path}")
        _plain("managed root", path)
        if not os.path.lexists(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            _make_dir(path, 0o700)
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise ValueError(f"managed root must not be or go through a symlink: {path}")
        self._check_owned(path)
        if stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
            raise ValueError(f"managed root must be mode 0700: {path}")
        self.path = path
        self.image = image
        self.runner = runner
        self.runs = path / "runs"
        self.repos = path / "repos"
        self.pristine = path / "pristine"
        for sub in (self.runs, self.repos, self.pristine):
            if not os.path.lexists(sub):
                _make_dir(sub, 0o700)
            if not stat.S_ISDIR(os.lstat(sub).st_mode):
                raise ValueError(f"{sub} must be a real directory, not a symlink or file")
            self._check_owned(sub)

    @staticmethod
    def _check_owned(path: Path) -> None:
        info = os.lstat(path)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{path} must be a directory")
        if info.st_uid != os.getuid():
            raise ValueError(f"{path} must be owned by the current user")

    def new_run(self, kind: str) -> tuple[Path, Path]:
        """A fresh run directory, writable by the sandbox user, and the workspace inside it."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        run_dir = self.runs / f"{kind}-{secrets.token_hex(8)}"
        _make_dir(run_dir, 0o777)
        return run_dir, run_dir / "ws"

    def run_kind(self, run_dir: Path) -> str:
        match = RUN_DIR_RE.fullmatch(run_dir.name)
        if run_dir.parent != self.runs or not match:
            raise ValueError(f"not a managed run directory: {run_dir}")
        try:
            info = os.lstat(run_dir)
        except FileNotFoundError as error:
            raise ValueError(f"run directory does not exist: {run_dir}") from error
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"run directory must be a real directory: {run_dir}")
        return match.group(1)

    def workspace(self, run_dir: Path, kind: str) -> Path:
        if self.run_kind(run_dir) != kind:
            raise ValueError(f"{run_dir} is not a {kind!r} kind run directory")
        return run_dir / "ws"

    def check_pristine(self, pristine: Path) -> None:
        _plain("pristine", pristine)
        if pristine.resolve(strict=True) != pristine:
            raise ValueError(f"pristine tree must not be or go through a symlink: {pristine}")
        if pristine == self.pristine or not pristine.is_relative_to(self.pristine):
            raise ValueError(f"pristine tree must be inside {self.pristine}: {pristine}")

    def copy_from(self, run_dir: Path, pristine: Path) -> list[str]:
        """Copy `pristine` to `run_dir/ws`; returns every failure, empty on success."""
        self.run_kind(run_dir)
        if os.path.lexists(run_dir / "ws"):
            raise ValueError(f"{run_dir} already has a workspace")
        name = f"rootflake-{secrets.token_hex(8)}"
        args = build_copy_args(name, self.image, pristine, run_dir)
        check_copy_args(args, self, name, self.image, pristine, run_dir)
        return self._run(args, name, "copy")

    def remove(self, run_dir: Path) -> list[str]:
        """Delete a run directory's contents in a container, then the directory itself."""
        self.run_kind(run_dir)
        errors: list[str] = []
        ws = run_dir / "ws"
        if os.path.lexists(ws):
            if stat.S_ISDIR(os.lstat(ws).st_mode):
                name = f"rootflake-{secrets.token_hex(8)}"
                args = build_unlock_args(name, self.image, run_dir)
                check_unlock_args(args, self, name, self.image, run_dir)
                errors += self._run(args, name, "unlock")
            else:
                errors.append(f"{ws} is not a real directory, so it was not unlocked")
        name = f"rootflake-{secrets.token_hex(8)}"
        args = build_remove_args(name, self.image, run_dir)
        check_remove_args(args, self, name, self.image, run_dir)
        errors += self._run(args, name, "remove")
        try:
            os.rmdir(run_dir)
        except OSError as error:
            errors.append(f"run directory {run_dir} not removed: {error}")
        return errors

    def repo_dir(self, project_key: str) -> Path:
        """The Maven repository directory for one project and commit, writable by the sandbox."""
        return _keyed_dir(self.repos, project_key, 0o777)

    def pristine_dir(self, project_key: str) -> Path:
        """The host's checkout for one project and commit, readable by the sandbox user."""
        return _keyed_dir(self.pristine, project_key, 0o755)

    def _run(self, args: list[str], name: str, what: str) -> list[str]:
        try:
            result = self.runner.run(args, COPY_TIMEOUT_S)
        except Exception as error:
            return [f"{what} container {name} failed to run: {error}"]
        if result.timed_out:
            errors = [f"{what} container {name} timed out after {COPY_TIMEOUT_S} s"]
            try:
                self.runner.stop(name)
            except Exception as error:
                errors.append(f"{what} container {name} not stopped: {error}")
            return errors
        if result.exit_code != 0:
            return [f"{what} container {name} exited {result.exit_code}: {result_output(result)}"]
        return []
