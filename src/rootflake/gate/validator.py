"""The patch validator: SH-15 in the sandbox hardening checklist.

It reads the patch text and the pristine checkout, never the sandbox copy, and returns accept
or reject, never a flag. The patch must be a git style unified diff in the narrow form SH-15
rule 1 defines; anything that form does not define is a rejection, not something to skip.
Hunks are applied in memory at their stated position, with no offset and no fuzz, so the
content each file will have after the patch is known before anything touches the disk.
"""

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

MAX_PATCH_BYTES = 256 * 1024
MAX_SECTIONS = 20
MAX_FILE_BYTES = 1024 * 1024

# A patch can remove at most its own length from a file, so a larger pristine file cannot end
# under the file limit and is rejected without being read.
_MAX_PRISTINE_BYTES = MAX_FILE_BYTES + MAX_PATCH_BYTES
MAX_PATH_BYTES = 1024
MAX_COMPONENT_BYTES = 255

_COMPONENT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)
_SOURCE_SETS = (("test", "java"), ("main", "java"), ("test", "resources"))
_GENERATED = frozenset(["target", "generated-sources", "generated-test-sources"])
_SUITE_MARKERS = (b"<suite", b"testng.org/testng")
OVERRIDES_PATH = Path(__file__).resolve().parent / "overrides.json"
LIFTABLE = frozenset(["outside_allowlist", "not_a_module", "denied_test_engine_config", "size"])
_OVERRIDE_KEYS = ("project", "test", "path", "rule", "reason")
_DIFF_PREFIX = "diff --git "
_INDEX_RE = re.compile(r"index [0-9a-f]{7,64}\.\.[0-9a-f]{7,64}(?: ([0-7]{6}))?")
_HUNK_RE = re.compile(
    r"@@ -([0-9]{1,9})(?:,([0-9]{1,9}))? \+([0-9]{1,9})(?:,([0-9]{1,9}))? @@(?: .*)?"
)
_NO_NEWLINE = "\\ No newline at end of file"
_FORBIDDEN_HEADERS = (
    ("rename from ", "rename"),
    ("rename to ", "rename"),
    ("copy from ", "rename"),
    ("copy to ", "rename"),
    ("similarity index ", "rename"),
    ("dissimilarity index ", "rename"),
    ("old mode ", "mode"),
    ("new mode ", "mode"),
    ("deleted file mode ", "delete"),
    ("GIT binary patch", "binary"),
    ("Binary files ", "binary"),
)


@dataclass(frozen=True)
class Rejection:
    code: str
    path: str | None = None


@dataclass(frozen=True)
class Change:
    path: str
    created: bool
    content: bytes


@dataclass(frozen=True)
class Override:
    project: str
    test: str
    path: str
    rule: str
    reason: str


@dataclass(frozen=True)
class Validation:
    rejections: tuple[Rejection, ...]
    changes: tuple[Change, ...] = ()
    overridden: tuple[Rejection, ...] = ()

    @property
    def accepted(self) -> bool:
        return not self.rejections


@dataclass
class _Hunk:
    old_start: int
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)


@dataclass
class _Section:
    path: str
    created: bool
    hunks: list[_Hunk]


class _Reject(Exception):
    def __init__(self, code: str, path: str | None = None):
        super().__init__(code)
        self.rejection = Rejection(code, path)


def _header_path(line: str) -> str:
    if "\r" in line:
        raise _Reject("format")
    rest = line[len(_DIFF_PREFIX) :]
    if (len(rest) - 5) % 2 or len(rest) < 7:
        raise _Reject("format")
    path = rest[2 : 2 + (len(rest) - 5) // 2]
    if rest != f"a/{path} b/{path}" or not path:
        raise _Reject("format")
    return path


def _expect(lines: list[str], i: int, wanted: str, path: str) -> int:
    if i >= len(lines) or lines[i] != wanted:
        raise _Reject("format", path)
    return i + 1


def _parse_hunk(lines: list[str], i: int, path: str) -> tuple[_Hunk, int]:
    match = _HUNK_RE.fullmatch(lines[i])
    if not match or "\r" in lines[i]:
        raise _Reject("format", path)
    old_start = int(match[1])
    old_count = 1 if match[2] is None else int(match[2])
    new_count = 1 if match[4] is None else int(match[4])
    hunk = _Hunk(old_start)
    i += 1
    old_seen = new_seen = 0
    while old_seen < old_count or new_seen < new_count:
        if i >= len(lines) or not lines[i]:
            raise _Reject("format", path)
        tag, text = lines[i][0], lines[i][1:] + "\n"
        i += 1
        if tag not in " -+":
            raise _Reject("format", path)
        if i < len(lines) and lines[i] == _NO_NEWLINE:
            last_old = tag == "+" or old_seen + 1 == old_count
            last_new = tag == "-" or new_seen + 1 == new_count
            if not (last_old and last_new):
                raise _Reject("format", path)
            text = text[:-1]
            i += 1
        if tag in " -":
            hunk.old_lines.append(text)
            old_seen += 1
        if tag in " +":
            hunk.new_lines.append(text)
            new_seen += 1
    if old_seen != old_count or new_seen != new_count:
        raise _Reject("format", path)
    return hunk, i


def _reject_carriage_returns_in_headers(lines: list[str], i: int, path: str) -> None:
    while i < len(lines) and not lines[i].startswith(("@@ ", _DIFF_PREFIX)):
        if "\r" in lines[i]:
            raise _Reject("format", path)
        i += 1


def _reject_forbidden_header(lines: list[str], i: int, path: str) -> None:
    for prefix, code in _FORBIDDEN_HEADERS:
        if i < len(lines) and lines[i].startswith(prefix):
            raise _Reject(code, path)


def _parse_section(lines: list[str], i: int) -> tuple[_Section, int]:
    if not lines[i].startswith(_DIFF_PREFIX):
        raise _Reject("format")
    path = _header_path(lines[i])
    i += 1
    _reject_carriage_returns_in_headers(lines, i, path)
    created = False
    if i < len(lines) and lines[i].startswith("new file mode "):
        if lines[i] != "new file mode 100644":
            raise _Reject("mode", path)
        created = True
        i += 1
    _reject_forbidden_header(lines, i, path)
    if i < len(lines) and lines[i].startswith("index "):
        match = _INDEX_RE.fullmatch(lines[i])
        if not match:
            raise _Reject("format", path)
        if match[1] is not None and match[1] != "100644":
            raise _Reject("mode", path)
        i += 1
    _reject_forbidden_header(lines, i, path)
    i = _expect(lines, i, "--- /dev/null" if created else f"--- a/{path}", path)
    if i < len(lines) and lines[i] == "+++ /dev/null":
        raise _Reject("delete", path)
    i = _expect(lines, i, f"+++ b/{path}", path)
    hunks = []
    while i < len(lines) and lines[i].startswith("@@ "):
        hunk, i = _parse_hunk(lines, i, path)
        hunks.append(hunk)
    if not hunks:
        raise _Reject("format", path)
    if created and (len(hunks) != 1 or hunks[0].old_start != 0 or hunks[0].old_lines):
        raise _Reject("format", path)
    return _Section(path, created, hunks), i


def _parse(patch: bytes) -> list[_Section]:
    if b"\0" in patch:
        raise _Reject("binary")
    try:
        text = patch.decode("utf-8")
    except UnicodeDecodeError:
        raise _Reject("binary") from None
    if not text.endswith("\n"):
        raise _Reject("format")
    lines = text[:-1].split("\n")
    sections, seen, i = [], set(), 0
    while i < len(lines):
        section, i = _parse_section(lines, i)
        if section.path in seen:
            raise _Reject("format", section.path)
        seen.add(section.path)
        sections.append(section)
    if not sections:
        raise _Reject("format")
    return sections


def _split_lf(data: bytes) -> list[bytes]:
    lines = data.split(b"\n")
    last = lines.pop()
    return [line + b"\n" for line in lines] + ([last] if last else [])


def _apply(original: bytes, section: _Section) -> bytes:
    lines = _split_lf(original)
    out, position = [], 0
    for hunk in section.hunks:
        old = [line.encode() for line in hunk.old_lines]
        start = hunk.old_start - 1 if old else hunk.old_start
        if start < position or start > len(lines) or lines[start : start + len(old)] != old:
            raise _Reject("does_not_apply", section.path)
        out.extend(lines[position:start])
        out.extend(line.encode() for line in hunk.new_lines)
        position = start + len(old)
    out.extend(lines[position:])
    return b"".join(out)


def _path_syntax(path: str) -> Rejection | None:
    if len(path.encode()) > MAX_PATH_BYTES:
        return Rejection("path_syntax", path)
    for component in path.split("/"):
        if (
            not _COMPONENT_RE.fullmatch(component)
            or component.endswith(".")
            or len(component.encode()) > MAX_COMPONENT_BYTES
            or component.split(".")[0].upper() in _RESERVED_NAMES
        ):
            return Rejection("path_syntax", path)
    return None


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError, NotADirectoryError:
        return None


def _is_real_dir(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat.S_ISDIR(info.st_mode)


def _allowlist(path: str) -> Rejection | None:
    parts = path.split("/")
    if "src" not in parts or _GENERATED.intersection(parts):
        return Rejection("outside_allowlist", path)
    at = parts.index("src")
    source_set, rest = tuple(parts[at + 1 : at + 3]), parts[at + 3 :]
    if source_set not in _SOURCE_SETS or not rest:
        return Rejection("outside_allowlist", path)
    if source_set[1] == "java" and not rest[-1].endswith(".java"):
        return Rejection("outside_allowlist", path)
    return None


def _module(path: str, pristine: Path) -> Rejection | None:
    parts = path.split("/")
    if "src" not in parts:
        return None
    directory = pristine
    for component in parts[: parts.index("src")]:
        directory = directory / component
        if not _is_real_dir(directory):
            return Rejection("not_a_module", path)
    pom = _lstat(directory / "pom.xml")
    return None if pom and stat.S_ISREG(pom.st_mode) else Rejection("not_a_module", path)


def _denied_by_name(path: str) -> Rejection | None:
    original = path.split("/")
    at = original.index("src") if "src" in original else len(original)
    in_test_resources = original[at + 1 : at + 3] == ["test", "resources"]
    parts = [part.lower() for part in original]
    name = parts[-1]
    if name == "module-info.java":
        return Rejection("denied_test_engine_config", path)
    if in_test_resources and (
        "meta-inf" in parts
        or "mockito-extensions" in parts
        or name == "junit-platform.properties"
        or (name.startswith("testng") and name.endswith(".xml"))
    ):
        return Rejection("denied_test_engine_config", path)
    return None


def _existence(section: _Section, pristine: Path) -> Rejection | None:
    path = section.path
    directory = pristine
    for component in path.split("/")[:-1]:
        directory = directory / component
        info = _lstat(directory)
        if info is None:
            return None if section.created else Rejection("not_found", path)
        if not stat.S_ISDIR(info.st_mode):
            return Rejection("symlink", path)
    info = _lstat(pristine / path)
    if section.created:
        return None if info is None else Rejection("already_exists", path)
    if info is None:
        return Rejection("not_found", path)
    if not stat.S_ISREG(info.st_mode):
        return Rejection("symlink", path)
    if info.st_mode & 0o111:
        return Rejection("mode", path)
    return None


def _prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: n + 1]) for n in range(len(parts))]


def _patch_case_collisions(sections: list[_Section]) -> set[str]:
    spellings: dict[str, set[str]] = {}
    for section in sections:
        for prefix in _prefixes(section.path):
            spellings.setdefault(prefix.lower(), set()).add(prefix)
    return {
        section.path
        for section in sections
        if section.created
        and any(len(spellings[prefix.lower()]) > 1 for prefix in _prefixes(section.path))
    }


def _pristine_case_collision(path: str, pristine: Path) -> Rejection | None:
    directory = pristine
    for component in path.split("/"):
        if not _is_real_dir(directory):
            return None
        folded = component.lower()
        if any(entry != component and entry.lower() == folded for entry in os.listdir(directory)):
            return Rejection("case_collision", path)
        directory = directory / component
    return None


def _content(path: str, content: bytes) -> Rejection | None:
    if len(content) > MAX_FILE_BYTES:
        return Rejection("size", path)
    if b"\0" in content:
        return Rejection("binary", path)
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return Rejection("binary", path)
    return None


def _denied_by_content(path: str, before: bytes, after: bytes) -> Rejection | None:
    if path.lower().endswith(".xml") and any(
        marker in content for content in (before, after) for marker in _SUITE_MARKERS
    ):
        return Rejection("denied_test_engine_config", path)
    return None


def _path_problems(
    section: _Section, pristine: Path, colliding: bool
) -> Iterator[Rejection | None]:
    # Lazy and ordered: a path that fails the syntax rule, which no override lifts, stops the
    # section before any later check touches the filesystem with it.
    path = section.path
    yield _path_syntax(path)
    yield _allowlist(path)
    yield _module(path, pristine)
    yield _denied_by_name(path)
    yield _existence(section, pristine)
    yield Rejection("case_collision", path) if colliding else None
    if section.created:
        yield _pristine_case_collision(path, pristine)


def _content_problems(path: str, original: bytes, content: bytes) -> Iterator[Rejection | None]:
    yield _content(path, content)
    yield _denied_by_content(path, original, content)


def parse_overrides(text: str) -> tuple[Override, ...]:
    try:
        entries = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"override list is not JSON: {error}") from None
    if not isinstance(entries, list):
        raise ValueError("override list must be a JSON array")
    overrides = []
    for entry in entries:
        if not isinstance(entry, dict) or sorted(entry) != sorted(_OVERRIDE_KEYS):
            raise ValueError(f"override entry must have exactly {_OVERRIDE_KEYS}: {entry!r}")
        if not all(isinstance(entry[key], str) and entry[key] for key in _OVERRIDE_KEYS):
            raise ValueError(f"override entry values must be non empty strings: {entry!r}")
        if entry["rule"] not in LIFTABLE:
            raise ValueError(f"override rule must be one of {sorted(LIFTABLE)}: {entry!r}")
        overrides.append(Override(**entry))
    return tuple(overrides)


def load_overrides() -> tuple[Override, ...]:
    return parse_overrides(OVERRIDES_PATH.read_text(encoding="utf-8"))


def validate(
    patch: bytes,
    pristine: Path,
    project: str,
    test: str,
    overrides: tuple[Override, ...] | None = None,
) -> Validation:
    """Overrides come from the committed list unless a caller passes its own, as tests do."""
    if overrides is None:
        overrides = load_overrides()
    lifts = {(o.path, o.rule) for o in overrides if (o.project, o.test) == (project, test)}
    if len(patch) > MAX_PATCH_BYTES:
        return Validation((Rejection("size"),))
    try:
        sections = _parse(patch)
    except _Reject as reject:
        return Validation((reject.rejection,))
    if len(sections) > MAX_SECTIONS:
        return Validation((Rejection("size"),))
    colliding = _patch_case_collisions(sections)
    rejections, changes, overridden = [], [], []

    def blocked(problems: Iterator[Rejection | None]) -> bool:
        for problem in problems:
            if problem is None:
                continue
            if (problem.path, problem.code) in lifts:
                overridden.append(problem)
                continue
            rejections.append(problem)
            return True
        return False

    for section in sections:
        if blocked(_path_problems(section, pristine, section.path in colliding)):
            continue
        path = section.path
        if not section.created and os.lstat(pristine / path).st_size > _MAX_PRISTINE_BYTES:
            rejections.append(Rejection("size", path))
            continue
        original = b"" if section.created else (pristine / path).read_bytes()
        try:
            content = _apply(original, section)
        except _Reject as reject:
            rejections.append(reject.rejection)
            continue
        if blocked(_content_problems(path, original, content)):
            continue
        changes.append(Change(path, section.created, content))
    if rejections:
        return Validation(tuple(rejections), (), tuple(overridden))
    return Validation((), tuple(changes), tuple(overridden))


def _snapshot(root: Path) -> dict[str, tuple]:
    entries: dict[str, tuple] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as scan:
            for entry in scan:
                info = entry.stat(follow_symlinks=False)
                rel = Path(entry.path).relative_to(root).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    entries[rel] = ("dir",)
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    entries[rel] = ("file", stat.S_IMODE(info.st_mode), Path(entry.path))
                elif stat.S_ISLNK(info.st_mode):
                    entries[rel] = ("link", os.readlink(entry.path))
                else:
                    entries[rel] = ("other", stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode))
    return entries


def _same(expected: tuple, actual: tuple, content: bytes | None) -> bool:
    if expected[0] != actual[0]:
        return False
    if expected[0] != "file":
        return expected == actual
    if expected[1] != actual[1]:
        return False
    wanted = expected[2].read_bytes() if content is None else content
    return actual[2].read_bytes() == wanted


def check_after_apply(pristine: Path, workspace: Path, validation: Validation) -> Validation:
    """SH-15 rule 10. On a mismatch the caller discards the copy; nothing here deletes it."""
    if not validation.accepted:
        raise ValueError("only an accepted validation can be checked after apply")
    expected = _snapshot(pristine)
    contents: dict[str, bytes] = {}
    for change in validation.changes:
        for prefix in _prefixes(change.path)[:-1]:
            expected.setdefault(prefix, ("dir",))
        mode = 0o644 if change.created else expected[change.path][1]
        expected[change.path] = ("file", mode, None)
        contents[change.path] = change.content
    actual = _snapshot(workspace)
    for rel in sorted(expected.keys() | actual.keys()):
        if (
            rel not in expected
            or rel not in actual
            or not _same(expected[rel], actual[rel], contents.get(rel))
        ):
            return Validation((Rejection("post_apply_mismatch", rel),), (), validation.overridden)
    return validation
