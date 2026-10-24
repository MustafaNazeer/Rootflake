import os
from pathlib import Path

import pytest

from rootflake.gate.checker.check import Finding, Target, check
from rootflake.gate.checker.files import MAX_FILE_BYTES, MAX_FILES, CheckerError, read_side

JAVA = "src/test/java/org/example/FooTest.java"
FOO = "package org.example;\nclass FooTest {\n    void t() {}\n}\n"
TARGET = Target("org.example.FooTest", "t")


def write(root: Path, rel: str, content: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


class TestReadSide:
    def test_a_regular_file_is_read(self, tmp_path):
        write(tmp_path, JAVA, FOO)
        assert read_side(tmp_path, JAVA) == FOO

    def test_a_missing_file_is_absent(self, tmp_path):
        assert read_side(tmp_path, JAVA) is None

    def test_an_empty_file_is_not_absent(self, tmp_path):
        write(tmp_path, JAVA, "")
        assert read_side(tmp_path, JAVA) == ""

    def test_a_symlinked_file_is_refused(self, tmp_path):
        real = write(tmp_path, "src/test/java/org/example/Real.java", FOO)
        (tmp_path / JAVA).symlink_to(real)
        with pytest.raises(CheckerError):
            read_side(tmp_path, JAVA)

    def test_a_symlinked_directory_component_is_refused(self, tmp_path):
        write(tmp_path, "elsewhere/org/example/FooTest.java", FOO)
        (tmp_path / "src/test").mkdir(parents=True)
        (tmp_path / "src/test/java").symlink_to(tmp_path / "elsewhere")
        with pytest.raises(CheckerError):
            read_side(tmp_path, JAVA)

    def test_a_non_regular_file_is_refused(self, tmp_path):
        (tmp_path / JAVA).parent.mkdir(parents=True)
        os.mkfifo(tmp_path / JAVA)
        with pytest.raises(CheckerError):
            read_side(tmp_path, JAVA)

    def test_a_file_at_the_limit_is_read(self, tmp_path):
        write(tmp_path, JAVA, "a" * MAX_FILE_BYTES)
        assert len(read_side(tmp_path, JAVA)) == MAX_FILE_BYTES

    def test_a_file_one_byte_over_the_limit_is_refused(self, tmp_path):
        write(tmp_path, JAVA, "a" * (MAX_FILE_BYTES + 1))
        with pytest.raises(CheckerError):
            read_side(tmp_path, JAVA)

    @pytest.mark.parametrize("data", [b"a\xffb", b"a\x00b"], ids=["invalid_utf8", "nul"])
    def test_bad_bytes_are_refused(self, tmp_path, data):
        write(tmp_path, JAVA, data)
        with pytest.raises(CheckerError):
            read_side(tmp_path, JAVA)

    @pytest.mark.parametrize("path", ["../x.java", "/etc/passwd", "a//b.java", "a/./b.java", ""])
    def test_a_path_that_leaves_or_bends_the_tree_is_refused(self, tmp_path, path):
        with pytest.raises(CheckerError):
            read_side(tmp_path, path)


class TestCheckBounds:
    def trees(self, tmp_path, before=FOO, after=FOO):
        pristine, workspace = tmp_path / "p", tmp_path / "w"
        if before is not None:
            write(pristine, JAVA, before)
        else:
            pristine.mkdir()
        if after is not None:
            write(workspace, JAVA, after)
        else:
            workspace.mkdir()
        return pristine, workspace

    def test_an_unchanged_file_has_no_findings(self, tmp_path):
        pristine, workspace = self.trees(tmp_path)
        assert check([JAVA], pristine, workspace, TARGET).findings == ()

    def test_more_than_the_file_limit_is_a_checker_error(self, tmp_path):
        pristine, workspace = self.trees(tmp_path)
        paths = [f"src/test/java/F{n}.java" for n in range(MAX_FILES + 1)]
        assert check(paths, pristine, workspace, TARGET).findings == (
            Finding("checker/checker_error", None),
        )

    def test_a_bad_read_is_a_checker_error_on_its_path(self, tmp_path):
        pristine, workspace = self.trees(tmp_path, after="a\x00b")
        assert check([JAVA], pristine, workspace, TARGET).findings == (
            Finding("checker/checker_error", JAVA),
        )

    def test_a_path_absent_on_both_sides_is_a_checker_error(self, tmp_path):
        pristine, workspace = self.trees(tmp_path, before=None, after=None)
        assert check([JAVA], pristine, workspace, TARGET).verdict == "reject"

    def test_a_deleted_file_reports_only_test_file_removed(self, tmp_path):
        pristine, workspace = self.trees(tmp_path, after=None)
        assert check([JAVA], pristine, workspace, TARGET).findings == (
            Finding("disable/test_file_removed", JAVA),
        )

    def test_a_created_file_has_an_absent_pristine_side(self, tmp_path):
        pristine, workspace = self.trees(tmp_path, before=None)
        assert check([JAVA], pristine, workspace, TARGET).findings == ()

    def test_an_unparsable_file_is_unparsable_with_its_line(self, tmp_path):
        pristine, workspace = self.trees(tmp_path, after=FOO + "/* never closed\n")
        assert check([JAVA], pristine, workspace, TARGET).findings == (
            Finding("checker/unparsable", JAVA, 5),
        )

    def test_an_unexpected_exception_is_a_checker_error(self, tmp_path, monkeypatch):
        pristine, workspace = self.trees(tmp_path)
        import rootflake.gate.checker.check as module

        def boom(*_):
            raise KeyError("bug")

        monkeypatch.setattr(module, "_judge", boom)
        assert check([JAVA], pristine, workspace, TARGET).findings == (
            Finding("checker/checker_error", None),
        )

    def test_non_java_paths_are_not_parsed(self, tmp_path):
        pristine, workspace = self.trees(tmp_path)
        write(workspace, "src/test/resources/data.bin", b"\xff\xfe")
        assert check(
            ["src/test/resources/data.bin"], pristine, workspace, TARGET
        ).findings == ()
