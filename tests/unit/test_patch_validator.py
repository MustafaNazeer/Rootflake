import difflib
import json
import os
import shutil
from pathlib import Path

import pytest

from rootflake.gate import validator as validator_module
from rootflake.gate.validator import (
    MAX_FILE_BYTES,
    MAX_PATCH_BYTES,
    MAX_SECTIONS,
    OVERRIDES_PATH,
    Override,
    Rejection,
    Validation,
    check_after_apply,
    load_overrides,
    parse_overrides,
    validate,
)

PROJECT = "https://github.com/example/demo"
TEST = "org.example.FooTest#testFoo"
TEST_FILE = "src/test/java/org/example/FooTest.java"
FOO = "class FooTest {\n    void testFoo() {\n        assertTrue(true);\n    }\n}\n"


def tree(root: Path, files: dict[str, str | bytes], modules=("",)) -> Path:
    for module in modules:
        pom = root / module / "pom.xml" if module else root / "pom.xml"
        pom.parent.mkdir(parents=True, exist_ok=True)
        pom.write_text("<project/>\n")
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        path.chmod(0o644)
    return root


def lf_lines(text: str) -> list[str]:
    return [line + "\n" for line in text.split("\n")[:-1]]


def hunks(before: str, after: str) -> str:
    lines = difflib.unified_diff(lf_lines(before), lf_lines(after), n=3)
    return "".join(list(lines)[2:])


def modify(path: str, before: str, after: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n" + hunks(before, after)
    )


def create(path: str, content: str, mode: str = "100644") -> str:
    lines = lf_lines(content)
    body = "".join("+" + line for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode {mode}\n"
        "index 0000000..3333333\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n" + body
    )


def run(patch: str | bytes, root: Path, overrides=()):
    data = patch.encode() if isinstance(patch, str) else patch
    return validate(data, root, PROJECT, TEST, overrides=overrides)


def codes(result) -> set[str]:
    return {r.code for r in result.rejections}


@pytest.fixture
def repo(tmp_path):
    return tree(tmp_path / "repo", {TEST_FILE: FOO})


FOO_FIXED = FOO.replace("assertTrue(true)", "assertEquals(1, 1)")


class TestAccepted:
    def test_a_modified_test_class_is_accepted_with_its_new_content(self, repo):
        result = run(modify(TEST_FILE, FOO, FOO_FIXED), repo)
        assert result.accepted
        assert result.rejections == ()
        [change] = result.changes
        assert (change.path, change.created, change.content) == (
            TEST_FILE,
            False,
            FOO_FIXED.encode(),
        )

    def test_a_created_helper_class_is_accepted(self, repo):
        path = "src/test/java/org/example/Waits.java"
        result = run(create(path, "class Waits {}\n"), repo)
        assert result.accepted
        [change] = result.changes
        assert (change.path, change.created, change.content) == (
            path,
            True,
            b"class Waits {}\n",
        )

    def test_crlf_content_lines_keep_their_line_endings(self, tmp_path):
        before = "class FooTest {\r\n    int a = 1;\r\n}\r\n"
        after = "class FooTest {\r\n    int a = 2;\r\n}\r\n"
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        result = run(modify(TEST_FILE, before, after), root)
        assert result.accepted
        assert result.changes[0].content == after.encode()

    def test_a_missing_final_newline_is_kept(self, tmp_path):
        root = tree(tmp_path / "repo", {TEST_FILE: "a\nb"})
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            f"--- a/{TEST_FILE}\n+++ b/{TEST_FILE}\n"
            "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n"
            "\\ No newline at end of file\n"
        )
        result = run(patch, root)
        assert result.accepted
        assert result.changes[0].content == b"a\nc"

    def test_only_lf_ends_a_line_when_hunks_are_applied(self, tmp_path):
        before = "class FooTest {\n    // a b\x0bc\rd\x1ce\n    int a = 1;\n}\n"
        after = before.replace("int a = 1", "int a = 2")
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        result = run(modify(TEST_FILE, before, after), root)
        assert result.accepted
        assert result.changes[0].content == after.encode()

    def test_several_hunks_apply_in_order(self, tmp_path):
        before = "".join(f"line {i}\n" for i in range(1, 31))
        after = before.replace("line 2\n", "line two\n").replace("line 28\n", "line 28b\n")
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        patch = modify(TEST_FILE, before, after)
        assert patch.count("@@ -") == 2
        result = run(patch, root)
        assert result.accepted
        assert result.changes[0].content == after.encode()


class TestFormat:
    def reject(self, patch, root, code):
        result = run(patch, root)
        assert not result.accepted
        assert code in codes(result), result.rejections

    def test_an_empty_patch_is_rejected(self, repo):
        self.reject("", repo, "format")

    def test_differing_header_paths_are_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            f"b/{TEST_FILE}\n", "b/src/test/java/org/example/BarTest.java\n", 1
        )
        self.reject(patch, repo, "format")

    @pytest.mark.parametrize("prefix", ["--- a/", "+++ b/"])
    def test_a_file_line_that_disagrees_with_the_header_is_rejected(self, repo, prefix):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            prefix + TEST_FILE, prefix + "src/test/java/org/example/Other.java"
        )
        self.reject(patch, repo, "format")

    def test_prose_before_the_first_section_is_rejected(self, repo):
        self.reject("Here is the fix:\n" + modify(TEST_FILE, FOO, FOO_FIXED), repo, "format")

    def test_prose_between_sections_is_rejected(self, repo):
        other = "src/test/java/org/example/Waits.java"
        patch = modify(TEST_FILE, FOO, FOO_FIXED) + "and also\n" + create(other, "class Waits {}\n")
        self.reject(patch, repo, "format")

    def test_prose_after_the_last_hunk_is_rejected(self, repo):
        self.reject(modify(TEST_FILE, FOO, FOO_FIXED) + "Hope this helps\n", repo, "format")

    def test_a_combined_diff_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}", f"diff --cc {TEST_FILE}"
        )
        self.reject(patch, repo, "format")

    def test_a_quoted_path_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}",
            f'diff --git "a/{TEST_FILE}" "b/{TEST_FILE}"',
        )
        self.reject(patch, repo, "format")

    @pytest.mark.parametrize(
        ("header", "code"),
        [
            ("rename from src/test/java/org/example/Old.java", "rename"),
            ("rename to src/test/java/org/example/FooTest.java", "rename"),
            ("copy from src/test/java/org/example/Old.java", "rename"),
            ("copy to src/test/java/org/example/FooTest.java", "rename"),
            ("similarity index 97%", "rename"),
            ("dissimilarity index 10%", "rename"),
            ("old mode 100644", "mode"),
            ("new mode 100755", "mode"),
            ("deleted file mode 100644", "delete"),
        ],
    )
    def test_extended_headers_outside_the_format_are_rejected(self, repo, header, code):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            "index 1111111..2222222 100644\n", header + "\n"
        )
        self.reject(patch, repo, code)

    def test_a_deletion_to_dev_null_is_rejected(self, repo):
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            "deleted file mode 100644\n"
            f"--- a/{TEST_FILE}\n+++ /dev/null\n"
            "@@ -1,5 +0,0 @@\n" + "".join("-" + line for line in lf_lines(FOO))
        )
        self.reject(patch, repo, "delete")

    def test_a_git_binary_patch_is_rejected(self, repo):
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            "index 1111111..2222222 100644\n"
            "GIT binary patch\nliteral 4\nLcmZ?d00001\n\n"
        )
        self.reject(patch, repo, "binary")

    def test_a_binary_files_differ_line_is_rejected(self, repo):
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            "index 1111111..2222222 100644\n"
            f"Binary files a/{TEST_FILE} and b/{TEST_FILE} differ\n"
        )
        self.reject(patch, repo, "binary")

    def test_invalid_utf8_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).encode().replace(b"assertEquals", b"assert\xff")
        self.reject(patch, repo, "binary")

    def test_a_nul_byte_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).encode().replace(b"assertEquals", b"assert\0")
        self.reject(patch, repo, "binary")

    @pytest.mark.parametrize("line", ["diff --git", "index", "---", "+++", "@@ -"])
    def test_a_carriage_return_in_a_header_line_is_rejected(self, repo, line):
        text = modify(TEST_FILE, FOO, FOO_FIXED)
        start = text.index(line)
        end = text.index("\n", start)
        self.reject(text[:end] + "\r" + text[end:], repo, "format")

    def test_a_carriage_return_after_hunk_section_text_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            "@@ -1,5 +1,5 @@\n", "@@ -1,5 +1,5 @@ class FooTest {\r\n"
        )
        self.reject(patch, repo, "format")

    @pytest.mark.parametrize(
        "header",
        ["@@ -" + "1" * 5000 + ",5 +1,5 @@", "@@ -\u0661,5 +1,5 @@", "@@ -1,5 +1,\uff15 @@"],
        ids=["five_thousand_digits", "arabic_indic_digit", "fullwidth_digit"],
    )
    def test_a_hunk_header_number_outside_plain_ascii_digits_is_rejected(self, repo, header):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace("@@ -1,5 +1,5 @@", header)
        assert run(patch, repo).rejections == (Rejection("format", TEST_FILE),)

    def test_a_no_newline_marker_before_the_last_line_of_its_side_is_rejected(self, tmp_path):
        root = tree(tmp_path / "repo", {TEST_FILE: "a\n"})
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            f"--- a/{TEST_FILE}\n+++ b/{TEST_FILE}\n"
            "@@ -1 +1,3 @@\n a\n+x\n\\ No newline at end of file\n+y\n"
        )
        assert run(patch, root).rejections == (Rejection("format", TEST_FILE),)

    def test_a_malformed_index_line_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            "index 1111111..2222222 100644", "index 11..22 100644"
        )
        assert run(patch, repo).rejections == (Rejection("format", TEST_FILE),)

    def test_a_section_without_hunks_is_rejected(self, repo):
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            "index 1111111..2222222 100644\n"
            f"--- a/{TEST_FILE}\n+++ b/{TEST_FILE}\n"
        )
        self.reject(patch, repo, "format")

    def test_a_hunk_whose_line_counts_disagree_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace("@@ -1,5 +1,5 @@", "@@ -1,6 +1,5 @@")
        self.reject(patch, repo, "format")

    def test_an_empty_line_inside_a_hunk_is_rejected(self, tmp_path):
        before = "a\n\nb\n"
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        patch = modify(TEST_FILE, before, "a\n\nc\n").replace("\n \n", "\n\n")
        self.reject(patch, root, "format")

    def test_a_patch_without_a_final_newline_is_rejected(self, repo):
        self.reject(modify(TEST_FILE, FOO, FOO_FIXED).rstrip("\n"), repo, "format")

    def test_a_dev_null_source_without_a_new_file_header_is_rejected(self, repo):
        path = "src/test/java/org/example/Waits.java"
        patch = create(path, "class Waits {}\n").replace("new file mode 100644\n", "")
        self.reject(patch, repo, "format")

    def test_a_new_file_header_with_an_a_side_path_is_rejected(self, repo):
        path = "src/test/java/org/example/Waits.java"
        patch = create(path, "class Waits {}\n").replace("--- /dev/null", f"--- a/{path}")
        self.reject(patch, repo, "format")

    def test_a_created_file_hunk_that_does_not_start_at_zero_is_rejected(self, repo):
        path = "src/test/java/org/example/Waits.java"
        patch = create(path, "class Waits {}\n").replace("@@ -0,0 +1,1 @@", "@@ -1,0 +1,1 @@")
        self.reject(patch, repo, "format")

    def test_the_same_path_twice_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED) * 2
        self.reject(patch, repo, "format")

    def test_a_rejection_names_the_offending_path(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            "index 1111111..2222222 100644\n", "old mode 100644\nnew mode 100755\n"
        )
        result = run(patch, repo)
        assert Rejection("mode", TEST_FILE) in result.rejections


def created(path: str, content: str = "x\n") -> str:
    return create(path, content)


class TestPathSyntax:
    @pytest.mark.parametrize(
        "path",
        [
            "src/test/java/../java/FooTest.java",
            "src/test/java/./FooTest.java",
            "/src/test/java/FooTest.java",
            "src/test//java/FooTest.java",
            "src/test/java/.hidden/FooTest.java",
            "src/test/java/-x/FooTest.java",
            "src/test/java/x./FooTest.java",
            "src/test/java/Foo Test.java",
            "src/test/java\\FooTest.java",
            "src/test/java/FoöTest.java",
            "src/test/java/FоoTest.java",
            "src/test/java/" + "a" * 251 + ".java",
            "src/test/java/" + "a/" * 503 + "Foo.java",
            "src/test/java/CON.java",
            "src/test/resources/nul.txt",
            "src/test/java/Com1/FooTest.java",
            "src/test/java/lpt9.data.java",
            ".mvn/maven.config",
            ".github/workflows/ci.yml",
        ],
    )
    def test_a_bad_path_is_rejected(self, repo, path):
        result = run(created(path), repo)
        assert Rejection("path_syntax", path) in result.rejections

    def test_a_path_of_exactly_1024_bytes_passes_the_syntax_rule(self, repo):
        path = "src/test/java/" + "a/" * 502 + "FooBarBazQux.java"
        path = path[: -len("FooBarBazQux.java")] + "F" * (1024 - len(path) + 12) + ".java"
        assert len(path) == 1024
        result = run(created(path), repo)
        assert "path_syntax" not in codes(result)

    def test_a_component_of_255_bytes_passes_the_syntax_rule(self, repo):
        path = "src/test/java/" + "a" * 250 + ".java"
        result = run(created(path), repo)
        assert result.accepted


class TestLocation:
    @pytest.mark.parametrize(
        ("path", "code"),
        [
            ("pom.xml", "outside_allowlist"),
            ("core/pom.xml", "outside_allowlist"),
            (".mvn/extensions.xml", "path_syntax"),
            ("mvnw", "outside_allowlist"),
            ("mvnw.cmd", "outside_allowlist"),
            ("build.gradle", "outside_allowlist"),
            ("settings.gradle.kts", "outside_allowlist"),
            ("gradlew", "outside_allowlist"),
            ("Jenkinsfile", "outside_allowlist"),
            (".travis.yml", "path_syntax"),
            (".gitlab-ci.yml", "path_syntax"),
            (".circleci/config.yml", "path_syntax"),
            ("azure-pipelines.yml", "outside_allowlist"),
            ("src/main/resources/application.properties", "outside_allowlist"),
            ("src/test/kotlin/FooTest.kt", "outside_allowlist"),
            ("src/test/groovy/FooSpec.groovy", "outside_allowlist"),
            ("src/test/scala/FooSpec.scala", "outside_allowlist"),
            ("src/it/java/FooIT.java", "outside_allowlist"),
            ("src/main/webapp/index.html", "outside_allowlist"),
            ("src/test/java/FooTest.kt", "outside_allowlist"),
            ("src/test/java/notes.txt", "outside_allowlist"),
            ("src/test/java/FooTest.JAVA", "outside_allowlist"),
            ("src/test/java/.java", "path_syntax"),
            ("target/generated-sources/Foo.java", "outside_allowlist"),
            ("src/test/java/target/Foo.java", "outside_allowlist"),
            ("src/main/java/generated-sources/Foo.java", "outside_allowlist"),
            ("src/test/java/generated-test-sources/Foo.java", "outside_allowlist"),
            ("Src/Test/Java/FooTest.java", "outside_allowlist"),
            ("src/test/java", "outside_allowlist"),
            ("src/test/resources", "outside_allowlist"),
            ("src/Test/java/FooTest.java", "outside_allowlist"),
        ],
    )
    def test_a_path_outside_the_allowlist_is_rejected(self, repo, path, code):
        result = run(created(path), repo)
        assert Rejection(code, path) in result.rejections

    def test_a_modified_root_pom_is_rejected(self, repo):
        pom = "<project/>\n"
        result = run(modify("pom.xml", pom, "<project><x/></project>\n"), repo)
        assert Rejection("outside_allowlist", "pom.xml") in result.rejections

    def test_a_directory_without_a_pom_is_not_a_module(self, tmp_path):
        root = tree(tmp_path / "repo", {"lib/README": "x\n"})
        path = "lib/src/test/java/FooTest.java"
        result = run(created(path), root)
        assert Rejection("not_a_module", path) in result.rejections

    def test_a_directory_that_does_not_exist_is_not_a_module(self, repo):
        path = "ghost/src/test/java/FooTest.java"
        result = run(created(path), repo)
        assert Rejection("not_a_module", path) in result.rejections

    def test_a_pom_that_is_a_symlink_does_not_make_a_module(self, tmp_path):
        root = tree(tmp_path / "repo", {"real/pom.xml": "<project/>\n"})
        (root / "lib").mkdir()
        (root / "lib" / "pom.xml").symlink_to(root / "real" / "pom.xml")
        path = "lib/src/test/java/FooTest.java"
        result = run(created(path), root)
        assert Rejection("not_a_module", path) in result.rejections

    def test_a_module_directory_that_is_a_symlink_is_not_a_module(self, tmp_path):
        root = tree(tmp_path / "repo", {}, modules=("", "real"))
        (root / "lib").symlink_to(root / "real")
        path = "lib/src/test/java/FooTest.java"
        result = run(created(path), root)
        assert Rejection("not_a_module", path) in result.rejections

    def test_a_module_whose_parent_is_a_symlink_is_not_a_module(self, tmp_path):
        root = tree(tmp_path / "repo", {}, modules=("", "real/core"))
        (root / "lib").symlink_to(root / "real")
        path = "lib/core/src/test/java/FooTest.java"
        result = run(created(path), root)
        assert Rejection("not_a_module", path) in result.rejections

    def test_a_pom_directory_is_not_a_module(self, tmp_path):
        root = tree(tmp_path / "repo", {"lib/pom.xml/x": "x\n"})
        path = "lib/src/test/java/FooTest.java"
        result = run(created(path), root)
        assert Rejection("not_a_module", path) in result.rejections


class TestAllowed:
    def test_a_test_class_three_modules_deep_is_accepted(self, tmp_path):
        path = "a/b/c/src/test/java/org/example/DeepTest.java"
        root = tree(tmp_path / "repo", {path: FOO}, modules=("", "a", "a/b", "a/b/c"))
        assert run(modify(path, FOO, FOO_FIXED), root).accepted

    def test_a_production_class_in_another_module_is_accepted(self, tmp_path):
        path = "core/src/main/java/org/example/Clock.java"
        before = "class Clock {\n    long now() { return 0; }\n}\n"
        root = tree(tmp_path / "repo", {path: before, TEST_FILE: FOO}, modules=("", "core"))
        after = before.replace("return 0", "return 1")
        assert run(modify(path, before, after), root).accepted

    def test_a_new_test_resource_data_file_is_accepted(self, repo):
        assert run(created("src/test/resources/data/input.json", "{}\n"), repo).accepted

    def test_a_modified_logback_test_file_is_accepted(self, tmp_path):
        path = "src/test/resources/logback-test.xml"
        before = '<configuration>\n  <root level="INFO"/>\n</configuration>\n'
        root = tree(tmp_path / "repo", {path: before})
        after = before.replace("INFO", "WARN")
        assert run(modify(path, before, after), root).accepted

    def test_a_java_file_under_test_resources_is_accepted(self, repo):
        assert run(
            created("src/test/resources/sample/Input.java", "class Input {}\n"), repo
        ).accepted


class TestDenyList:
    @pytest.mark.parametrize(
        "path",
        [
            "src/main/java/module-info.java",
            "src/test/java/module-info.java",
            "src/test/java/Module-Info.java",
            "src/test/resources/META-INF/services/"
            "org.junit.platform.launcher.LauncherSessionListener",
            "src/test/resources/META-INF/services/org.junit.platform.launcher.PostDiscoveryFilter",
            "src/test/resources/META-INF/services/org.junit.jupiter.api.extension.Extension",
            "src/test/resources/META-INF/services/org.testng.ITestNGListener",
            "src/test/resources/meta-inf/services/org.testng.ITestNGListener",
            "src/test/resources/a/META-INF/x.txt",
            "src/test/resources/mockito-extensions/org.mockito.plugins.MockMaker",
            "src/test/resources/Mockito-Extensions/org.mockito.plugins.MockMaker",
            "src/test/resources/junit-platform.properties",
            "src/test/resources/JUnit-Platform.properties",
            "src/test/resources/config/junit-platform.properties",
            "src/test/resources/testng.xml",
            "src/test/resources/testng-unit.xml",
            "src/test/resources/TestNG-Unit.XML",
        ],
    )
    def test_a_created_test_engine_file_is_rejected(self, repo, path):
        result = run(created(path), repo)
        assert Rejection("denied_test_engine_config", path) in result.rejections

    @pytest.mark.parametrize(
        "path",
        [
            "src/test/resources/junit-platform.properties",
            "src/test/resources/META-INF/services/org.junit.jupiter.api.extension.Extension",
            "src/test/resources/testng.xml",
        ],
    )
    def test_a_modified_test_engine_file_is_rejected(self, tmp_path, path):
        root = tree(tmp_path / "repo", {path: "a\n"})
        result = run(modify(path, "a\n", "b\n"), root)
        assert Rejection("denied_test_engine_config", path) in result.rejections

    @pytest.mark.parametrize("marker", ['<suite name="all">', "http://testng.org/testng-1.0.dtd"])
    def test_a_created_xml_file_holding_a_suite_is_rejected(self, repo, marker):
        path = "src/test/resources/suites/all.xml"
        result = run(created(path, marker + "\n"), repo)
        assert Rejection("denied_test_engine_config", path) in result.rejections

    def test_an_xml_file_that_held_a_suite_before_the_patch_is_rejected(self, tmp_path):
        path = "src/test/resources/suites/all.xml"
        before = '<suite name="all">\n</suite>\n'
        root = tree(tmp_path / "repo", {path: before})
        result = run(modify(path, before, "<config/>\n"), root)
        assert Rejection("denied_test_engine_config", path) in result.rejections

    def test_an_xml_file_without_a_suite_is_accepted(self, repo):
        assert run(created("src/test/resources/suites/data.xml", "<rows/>\n"), repo).accepted

    def test_a_module_named_like_src_does_not_hide_a_denied_file(self, tmp_path):
        root = tree(tmp_path / "repo", {}, modules=("", "Src"))
        path = "Src/src/test/resources/META-INF/services/org.testng.ITestNGListener"
        result = run(created(path), root)
        assert Rejection("denied_test_engine_config", path) in result.rejections

    def test_a_meta_inf_module_directory_counts_as_a_meta_inf_component(self, tmp_path):
        root = tree(tmp_path / "repo", {}, modules=("", "META-INF"))
        path = "META-INF/src/test/resources/data.txt"
        result = run(created(path), root)
        assert Rejection("denied_test_engine_config", path) in result.rejections


class TestSeveralSections:
    def test_a_problem_in_each_of_two_sections_is_reported_for_both(self, repo):
        patch = created("pom2.xml") + created("src/test/java/CON.java")
        assert run(patch, repo).rejections == (
            Rejection("outside_allowlist", "pom2.xml"),
            Rejection("path_syntax", "src/test/java/CON.java"),
        )


class TestCreateVersusModify:
    def test_modifying_a_path_that_does_not_exist_is_rejected(self, repo):
        path = "src/test/java/org/example/Ghost.java"
        result = run(modify(path, FOO, FOO_FIXED), repo)
        assert Rejection("not_found", path) in result.rejections

    def test_modifying_under_a_missing_directory_is_rejected(self, repo):
        path = "src/test/java/org/ghost/Ghost.java"
        result = run(modify(path, FOO, FOO_FIXED), repo)
        assert Rejection("not_found", path) in result.rejections

    def test_creating_a_path_that_exists_is_rejected(self, repo):
        result = run(created(TEST_FILE, FOO), repo)
        assert Rejection("already_exists", TEST_FILE) in result.rejections

    def test_modifying_a_symlink_is_rejected(self, tmp_path):
        root = tree(tmp_path / "repo", {"src/test/java/Real.java": FOO})
        (root / "src/test/java/Link.java").symlink_to(root / "src/test/java/Real.java")
        path = "src/test/java/Link.java"
        result = run(modify(path, FOO, FOO_FIXED), root)
        assert Rejection("symlink", path) in result.rejections

    def test_modifying_a_directory_is_rejected(self, tmp_path):
        root = tree(tmp_path / "repo", {"src/test/java/Dir.java/x": "x\n"})
        path = "src/test/java/Dir.java"
        result = run(modify(path, FOO, FOO_FIXED), root)
        assert Rejection("symlink", path) in result.rejections

    def test_modifying_a_file_with_an_executable_bit_is_rejected(self, repo):
        (repo / TEST_FILE).chmod(0o744)
        result = run(modify(TEST_FILE, FOO, FOO_FIXED), repo)
        assert Rejection("mode", TEST_FILE) in result.rejections

    def test_an_index_line_with_an_executable_mode_is_rejected(self, repo):
        patch = modify(TEST_FILE, FOO, FOO_FIXED).replace(
            "index 1111111..2222222 100644", "index 1111111..2222222 100755"
        )
        assert Rejection("mode", TEST_FILE) in run(patch, repo).rejections

    @pytest.mark.parametrize("mode", ["100755", "120000", "160000", "100664", "040000"])
    def test_creating_a_file_with_another_mode_is_rejected(self, repo, mode):
        path = "src/test/java/org/example/Waits.java"
        result = run(create(path, "class Waits {}\n", mode=mode), repo)
        assert Rejection("mode", path) in result.rejections

    def test_creating_under_a_parent_symlink_inside_the_tree_is_rejected(self, repo):
        (repo / "src/test/java/org/linked").symlink_to(repo / "src/test/java/org/example")
        path = "src/test/java/org/linked/Waits.java"
        result = run(created(path, "class Waits {}\n"), repo)
        assert Rejection("symlink", path) in result.rejections

    def test_creating_under_a_parent_symlink_outside_the_tree_is_rejected(self, repo, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / "src/test/java/org/out").symlink_to(outside)
        path = "src/test/java/org/out/Waits.java"
        result = run(created(path, "class Waits {}\n"), repo)
        assert Rejection("symlink", path) in result.rejections
        assert list(outside.iterdir()) == []

    def test_modifying_under_a_parent_symlink_is_rejected(self, repo):
        (repo / "src/test/java/org/linked").symlink_to(repo / "src/test/java/org/example")
        path = "src/test/java/org/linked/FooTest.java"
        result = run(modify(path, FOO, FOO_FIXED), repo)
        assert Rejection("symlink", path) in result.rejections

    def test_creating_under_a_parent_that_is_a_file_is_rejected(self, repo):
        path = TEST_FILE + "/Inner.java"
        assert Rejection("symlink", path) in run(created(path), repo).rejections

    def test_a_hunk_that_does_not_match_the_pristine_file_is_rejected(self, repo):
        stale = FOO.replace("assertTrue(true)", "assertTrue(false)")
        result = run(modify(TEST_FILE, stale, FOO_FIXED), repo)
        assert Rejection("does_not_apply", TEST_FILE) in result.rejections

    def test_a_hunk_at_the_wrong_line_is_rejected_even_if_its_context_matches(self, tmp_path):
        before = "".join(f"line {i}\n" for i in range(1, 21))
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        patch = modify(TEST_FILE, before, before.replace("line 10\n", "line ten\n"))
        moved = patch.replace("@@ -7,7 +7,7 @@", "@@ -8,7 +8,7 @@")
        assert moved != patch
        result = run(moved, root)
        assert Rejection("does_not_apply", TEST_FILE) in result.rejections

    def test_hunks_out_of_order_are_rejected(self, tmp_path):
        before = "".join(f"line {i}\n" for i in range(1, 31))
        after = before.replace("line 2\n", "line two\n").replace("line 28\n", "line 28b\n")
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        text = modify(TEST_FILE, before, after)
        head, first, second = text.split("@@ -")
        swapped = head + "@@ -" + second + "@@ -" + first
        assert Rejection("does_not_apply", TEST_FILE) in run(swapped, root).rejections

    def test_a_hunk_past_the_end_of_the_file_is_rejected(self, repo):
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            f"--- a/{TEST_FILE}\n+++ b/{TEST_FILE}\n"
            "@@ -40,0 +41,1 @@\n+extra\n"
        )
        assert Rejection("does_not_apply", TEST_FILE) in run(patch, repo).rejections


class TestDeletionAndRename:
    def test_deleting_a_test_resource_is_rejected(self, tmp_path):
        path = "src/test/resources/data.txt"
        root = tree(tmp_path / "repo", {path: "a\n"})
        patch = (
            f"diff --git a/{path} b/{path}\n"
            "deleted file mode 100644\n"
            "index 1111111..0000000\n"
            f"--- a/{path}\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
        )
        assert Rejection("delete", path) in run(patch, root).rejections

    def test_a_rename_within_test_java_is_rejected(self, repo):
        new = "src/test/java/org/example/BarTest.java"
        patch = (
            f"diff --git a/{TEST_FILE} b/{new}\n"
            "similarity index 100%\n"
            f"rename from {TEST_FILE}\nrename to {new}\n"
        )
        assert run(patch, repo).rejections == (Rejection("format"),)

    def test_a_rename_with_matching_header_paths_is_rejected_as_a_rename(self, repo):
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            "similarity index 100%\n"
            f"rename from {TEST_FILE}\nrename to {TEST_FILE}\n"
        )
        assert Rejection("rename", TEST_FILE) in run(patch, repo).rejections


class TestCollisions:
    def test_a_created_file_next_to_an_existing_one_differing_in_case_is_rejected(self, tmp_path):
        root = tree(tmp_path / "repo", {"src/test/java/Footest.java": FOO})
        path = "src/test/java/FooTest.java"
        assert Rejection("case_collision", path) in run(created(path), root).rejections

    def test_a_created_directory_differing_in_case_from_an_existing_one_is_rejected(self, repo):
        path = "src/test/java/Org/example/Waits.java"
        assert Rejection("case_collision", path) in run(created(path), repo).rejections

    def test_two_created_paths_differing_only_in_case_are_rejected(self, repo):
        first, second = (
            "src/test/java/org/example/Waits.java",
            "src/test/java/org/example/WAITS.java",
        )
        result = run(created(first) + created(second), repo)
        assert "case_collision" in codes(result)

    def test_created_directories_differing_only_in_case_are_rejected(self, repo):
        first, second = "src/test/java/org/x/A.java", "src/test/java/org/X/B.java"
        result = run(created(first) + created(second), repo)
        assert "case_collision" in codes(result)

    def test_a_created_path_differing_in_case_from_a_modified_path_is_rejected(self, repo):
        other = "src/test/java/org/example/FOOTEST.java"
        patch = modify(TEST_FILE, FOO, FOO_FIXED) + created(other)
        assert Rejection("case_collision", other) in run(patch, repo).rejections


class TestSize:
    def padded(self, total: int) -> str:
        path = "src/test/resources/big.txt"
        base = created(path, "\n")
        filler = total - len(base.encode()) - 2
        content = "a" * filler + "\n"
        patch = create(path, "\n" + content)
        assert len(patch.encode()) == total, len(patch.encode())
        return patch

    def test_a_patch_at_the_size_limit_is_accepted(self, repo):
        assert run(self.padded(MAX_PATCH_BYTES), repo).accepted

    def test_a_patch_one_byte_over_the_size_limit_is_rejected(self, repo):
        result = run(self.padded(MAX_PATCH_BYTES + 1), repo)
        assert result.rejections == (Rejection("size"),)

    def sections(self, count: int) -> str:
        return "".join(created(f"src/test/java/org/example/W{n}.java") for n in range(count))

    def test_twenty_sections_are_accepted(self, repo):
        assert run(self.sections(MAX_SECTIONS), repo).accepted

    def test_twenty_one_sections_are_rejected(self, repo):
        assert run(self.sections(MAX_SECTIONS + 1), repo).rejections == (Rejection("size"),)

    def big(self, tmp_path, extra: int):
        before = "a" * (MAX_FILE_BYTES - 3) + "\nb\n"
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        after = before[:-2] + "b" * (1 + extra) + "\n"
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            f"--- a/{TEST_FILE}\n+++ b/{TEST_FILE}\n"
            "@@ -2 +2 @@\n-b\n+" + "b" * (1 + extra) + "\n"
        )
        assert len(before) == MAX_FILE_BYTES and len(after) == MAX_FILE_BYTES + extra
        return run(patch, root)

    def test_a_file_at_the_size_limit_after_the_patch_is_accepted(self, tmp_path):
        assert self.big(tmp_path, 0).accepted

    def test_a_file_over_the_size_limit_after_the_patch_is_rejected(self, tmp_path):
        assert Rejection("size", TEST_FILE) in self.big(tmp_path, 1).rejections

    def test_a_pristine_file_too_large_to_end_under_the_limit_is_not_read(
        self, tmp_path, monkeypatch
    ):
        before = "a" * (MAX_FILE_BYTES + MAX_PATCH_BYTES) + "\nb\n"
        root = tree(tmp_path / "repo", {TEST_FILE: before})
        monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read " + str(self)))
        patch = (
            f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
            f"--- a/{TEST_FILE}\n+++ b/{TEST_FILE}\n"
            "@@ -2 +2 @@\n-b\n+c\n"
        )
        assert run(patch, root).rejections == (Rejection("size", TEST_FILE),)

    def test_a_created_file_with_a_nul_byte_is_rejected(self, repo):
        patch = created("src/test/resources/blob.bin", "a\0b\n")
        assert run(patch, repo).rejections == (Rejection("binary"),)

    def test_a_modified_file_that_stays_invalid_utf8_is_rejected(self, tmp_path):
        before = b"keep \xff\nchange me\n"
        root = tree(tmp_path / "repo", {"src/test/resources/data.txt": before})
        patch = (
            "diff --git a/src/test/resources/data.txt b/src/test/resources/data.txt\n"
            "--- a/src/test/resources/data.txt\n+++ b/src/test/resources/data.txt\n"
            "@@ -2 +2 @@\n-change me\n+changed\n"
        )
        result = run(patch, root)
        assert Rejection("binary", "src/test/resources/data.txt") in result.rejections

    def test_a_modified_file_with_a_nul_byte_kept_is_rejected(self, tmp_path):
        before = b"keep \0\nchange me\n"
        root = tree(tmp_path / "repo", {"src/test/resources/data.txt": before})
        patch = (
            "diff --git a/src/test/resources/data.txt b/src/test/resources/data.txt\n"
            "--- a/src/test/resources/data.txt\n+++ b/src/test/resources/data.txt\n"
            "@@ -2 +2 @@\n-change me\n+changed\n"
        )
        result = run(patch, root)
        assert Rejection("binary", "src/test/resources/data.txt") in result.rejections


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "patches"


def fixture_ids() -> list[str]:
    return sorted(p.parent.name for p in FIXTURES.glob("*/meta.json"))


def fixture_tree(fixture: Path, patch: bytes, root: Path) -> Path:
    before = fixture / "before"
    if before.is_dir():
        shutil.copytree(before, root)
    root.mkdir(exist_ok=True)
    modules = {""}
    for line in patch.decode("utf-8", "replace").split("\n"):
        if line.startswith("diff --git a/"):
            parts = line.split(" b/", 1)[-1].split("/")
            if "src" in parts:
                modules.add("/".join(parts[: parts.index("src")]))
    for module in modules:
        pom = root / module / "pom.xml"
        if not pom.exists():
            pom.parent.mkdir(parents=True, exist_ok=True)
            pom.write_text("<project/>\n")
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o644)
    return root


@pytest.mark.parametrize("fixture_id", fixture_ids())
def test_the_validator_decides_each_fixture_as_its_meta_says(tmp_path, fixture_id):
    fixture = FIXTURES / fixture_id
    meta = json.loads((fixture / "meta.json").read_text())
    patch = (fixture / "patch.diff").read_bytes()
    root = fixture_tree(fixture, patch, tmp_path / "repo")
    result = run(patch, root)
    if "patch_validator" in meta["rejected_by"]:
        assert not result.accepted
        if meta["rejected_by"] == ["patch_validator"]:
            assert sorted(codes(result)) == meta["expected_codes"], result.rejections
    else:
        assert result.accepted, result.rejections


def test_the_fixture_set_is_present():
    assert len(fixture_ids()) >= 159


PROPS = "src/main/resources/application.properties"


def lift(rule="outside_allowlist", path=PROPS, project=PROJECT, test=TEST) -> Override:
    return Override(project=project, test=test, path=path, rule=rule, reason="reviewed by hand")


class TestOverrides:
    def props_repo(self, tmp_path):
        return tree(tmp_path / "repo", {PROPS: "timeout=1\n"})

    def patch(self):
        return modify(PROPS, "timeout=1\n", "timeout=2\n")

    def test_an_override_lifts_its_rule_for_its_project_test_and_path(self, tmp_path):
        result = run(self.patch(), self.props_repo(tmp_path), overrides=(lift(),))
        assert result.accepted
        assert result.overridden == (Rejection("outside_allowlist", PROPS),)
        assert result.changes[0].content == b"timeout=2\n"

    @pytest.mark.parametrize(
        "entry",
        [
            lift(project="https://github.com/example/other"),
            lift(test="org.example.FooTest#testBar"),
            lift(path="src/main/resources/other.properties"),
            lift(rule="not_a_module"),
        ],
    )
    def test_an_override_for_anything_else_lifts_nothing(self, tmp_path, entry):
        result = run(self.patch(), self.props_repo(tmp_path), overrides=(entry,))
        assert result.rejections == (Rejection("outside_allowlist", PROPS),)
        assert result.overridden == ()

    def test_the_rules_after_a_lifted_one_still_apply(self, tmp_path):
        before = "a" * (MAX_FILE_BYTES - 3) + "\nb\n"
        root = tree(tmp_path / "repo", {PROPS: before})
        patch = f"diff --git a/{PROPS} b/{PROPS}\n--- a/{PROPS}\n+++ b/{PROPS}\n"
        patch += "@@ -2 +2 @@\n-b\n+bb\n"
        result = run(patch, root, overrides=(lift(),))
        assert Rejection("size", PROPS) in result.rejections

    def test_a_denied_test_engine_file_can_be_lifted(self, repo):
        path = "src/test/resources/junit-platform.properties"
        entry = lift(rule="denied_test_engine_config", path=path)
        result = run(created(path, "a=b\n"), repo, overrides=(entry,))
        assert result.accepted
        assert result.overridden == (Rejection("denied_test_engine_config", path),)

    def test_a_lifted_path_outside_a_module_is_still_not_a_module(self, tmp_path):
        path = "lib/src/main/resources/app.properties"
        root = tree(tmp_path / "repo", {path: "a\n"})
        result = run(modify(path, "a\n", "b\n"), root, overrides=(lift(path=path),))
        assert result.rejections == (Rejection("not_a_module", path),)
        assert result.overridden == (Rejection("outside_allowlist", path),)

    def test_a_lifted_build_file_is_accepted(self, repo):
        entry = lift(path="pom.xml")
        result = run(
            modify("pom.xml", "<project/>\n", "<project><x/></project>\n"), repo, overrides=(entry,)
        )
        assert result.accepted
        assert result.overridden == (Rejection("outside_allowlist", "pom.xml"),)

    def test_an_unlifted_validation_reports_no_overrides(self, repo):
        assert run(modify(TEST_FILE, FOO, FOO_FIXED), repo).overridden == ()


class TestOverrideList:
    def test_the_list_is_read_from_the_committed_file_in_the_package(self):
        package = Path(validator_module.__file__).resolve().parent
        assert OVERRIDES_PATH == package / "overrides.json"
        assert OVERRIDES_PATH.is_file()

    def test_the_committed_list_starts_empty(self):
        assert load_overrides() == ()

    def test_an_override_file_in_the_workspace_is_ignored(self, tmp_path):
        root = tree(tmp_path / "repo", {PROPS: "timeout=1\n"})
        entry = json.dumps([{**lift().__dict__}])
        for place in (
            "overrides.json",
            ".rootflake/overrides.json",
            "src/main/resources/overrides.json",
        ):
            (root / place).parent.mkdir(parents=True, exist_ok=True)
            (root / place).write_text(entry)
        patch = modify(PROPS, "timeout=1\n", "timeout=2\n").encode()
        result = validate(patch, root, PROJECT, TEST)
        assert result.rejections == (Rejection("outside_allowlist", PROPS),)

    def test_a_valid_entry_parses(self):
        text = json.dumps([{**lift().__dict__}])
        assert parse_overrides(text) == (lift(),)

    @pytest.mark.parametrize(
        "text",
        [
            "{}",
            "[1]",
            json.dumps([{**lift().__dict__, "extra": "x"}]),
            json.dumps([{k: v for k, v in lift().__dict__.items() if k != "reason"}]),
            json.dumps([{**lift().__dict__, "reason": ""}]),
            json.dumps([{**lift().__dict__, "path": 3}]),
            json.dumps([{**lift().__dict__, "rule": "path_syntax"}]),
            json.dumps([{**lift().__dict__, "rule": "format"}]),
            json.dumps([{**lift().__dict__, "rule": "not_found"}]),
            json.dumps([{**lift().__dict__, "rule": "symlink"}]),
            json.dumps([{**lift().__dict__, "rule": "post_apply_mismatch"}]),
            "not json",
        ],
    )
    def test_a_malformed_list_fails_to_load(self, text):
        with pytest.raises(ValueError):
            parse_overrides(text)

    @pytest.mark.parametrize(
        "rule", ["outside_allowlist", "not_a_module", "denied_test_engine_config", "size"]
    )
    def test_the_path_policy_codes_can_be_named(self, rule):
        text = json.dumps([{**lift().__dict__, "rule": rule}])
        assert parse_overrides(text)[0].rule == rule


WAITS = "src/test/java/org/example/Waits.java"


def applied(pristine: Path, workspace: Path, result) -> Path:
    shutil.copytree(pristine, workspace, symlinks=True)
    for change in result.changes:
        target = workspace / change.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(change.content)
        if change.created:
            target.chmod(0o644)
    return workspace


class TestCheckAfterApply:
    @pytest.fixture
    def setup(self, tmp_path):
        root = tree(tmp_path / "repo", {TEST_FILE: FOO, "README.md": "demo\n"})
        (root / "docs").mkdir()
        (root / "docs" / "link.md").symlink_to("../README.md")
        result = run(modify(TEST_FILE, FOO, FOO_FIXED) + created(WAITS, "class Waits {}\n"), root)
        assert result.accepted
        return root, applied(root, tmp_path / "work", result), result

    def mismatch(self, root, work, result):
        checked = check_after_apply(root, work, result)
        assert not checked.accepted
        assert {r.code for r in checked.rejections} == {"post_apply_mismatch"}
        return checked

    def test_a_faithful_apply_passes(self, setup):
        root, work, result = setup
        assert check_after_apply(root, work, result) == result

    def test_an_extra_file_is_caught(self, setup):
        root, work, result = setup
        (work / "src/test/java/org/example/Extra.java").write_text("class Extra {}\n")
        checked = self.mismatch(root, work, result)
        assert checked.rejections[0].path == "src/test/java/org/example/Extra.java"

    def test_a_different_path_from_the_approved_one_is_caught(self, setup):
        root, work, result = setup
        (work / WAITS).rename(work / "src/test/java/org/example/Other.java")
        self.mismatch(root, work, result)

    def test_a_new_symlink_is_caught(self, setup):
        root, work, result = setup
        (work / "src/test/java/org/example/l").symlink_to("/etc/passwd")
        self.mismatch(root, work, result)

    def test_an_approved_file_replaced_by_a_symlink_is_caught(self, setup, tmp_path):
        root, work, result = setup
        (work / WAITS).unlink()
        elsewhere = tmp_path / "elsewhere.java"
        elsewhere.write_text("class Waits {}\n")
        (work / WAITS).symlink_to(elsewhere)
        self.mismatch(root, work, result)

    def test_a_changed_symlink_target_is_caught(self, setup):
        root, work, result = setup
        (work / "docs" / "link.md").unlink()
        (work / "docs" / "link.md").symlink_to("/etc/passwd")
        self.mismatch(root, work, result)

    def test_an_executable_bit_on_a_created_file_is_caught(self, setup):
        root, work, result = setup
        (work / WAITS).chmod(0o755)
        self.mismatch(root, work, result)

    def test_an_executable_bit_on_an_untouched_file_is_caught(self, setup):
        root, work, result = setup
        (work / "README.md").chmod(0o755)
        self.mismatch(root, work, result)

    def test_a_created_file_with_another_mode_is_caught(self, setup):
        root, work, result = setup
        (work / WAITS).chmod(0o600)
        self.mismatch(root, work, result)

    def test_content_that_differs_from_the_validated_content_is_caught(self, setup):
        root, work, result = setup
        (work / TEST_FILE).write_text(FOO_FIXED + "// more\n")
        self.mismatch(root, work, result)

    def test_an_untouched_file_that_changed_is_caught(self, setup):
        root, work, result = setup
        (work / "README.md").write_text("changed\n")
        self.mismatch(root, work, result)

    def test_an_approved_change_that_was_not_made_is_caught(self, setup):
        root, work, result = setup
        (work / TEST_FILE).write_text(FOO)
        self.mismatch(root, work, result)

    def test_a_deleted_file_is_caught(self, setup):
        root, work, result = setup
        (work / "README.md").unlink()
        self.mismatch(root, work, result)

    def test_a_new_empty_directory_is_caught(self, setup):
        root, work, result = setup
        (work / "src" / "extra").mkdir()
        self.mismatch(root, work, result)

    def test_a_new_fifo_is_caught(self, setup):
        root, work, result = setup
        os.mkfifo(work / "src" / "pipe")
        self.mismatch(root, work, result)

    def test_a_rejected_validation_cannot_be_checked(self, setup):
        root, work, _ = setup
        with pytest.raises(ValueError):
            check_after_apply(root, work, Validation((Rejection("format"),)))
