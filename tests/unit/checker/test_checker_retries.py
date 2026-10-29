import os
from pathlib import Path

import pytest

from rootflake.gate.checker.check import Target, check
from rootflake.gate.checker.retries import (
    CLIENT,
    EXISTING,
    RETRY_ANNOTATION,
    RETRY_RULE,
    UNDER_TEST,
    UNKNOWN,
    client_package,
    retry_named,
)

PATH = "src/test/java/a/FooTest.java"
TARGET = Target("a.FooTest", "testFoo")
HEAD = (
    "package a;\nimport org.junit.Assert;\nimport org.junit.Rule;\nimport org.junit.Test;\n"
    "import org.junit.rules.TemporaryFolder;\nimport org.junit.rules.TestRule;\n"
)
RETRYING_RULE = """package a;
import org.junit.rules.TestRule;
import org.junit.runner.Description;
import org.junit.runners.model.Statement;
public class {name} implements TestRule {{
    public Statement apply(Statement base, Description d) {{
        return new Statement() {{
            public void evaluate() throws Throwable {{
                for (int i = 0; i < 3; i++) {{
                    try {{ base.evaluate(); return; }} catch (Throwable t) {{ }}
                }}
            }}
        }};
    }}
}}
"""


def java_class(fields: str = "", imports: str = "", header: str = "") -> str:
    return (
        f"{HEAD}{imports}\n{header}\npublic class FooTest {{\n{fields}\n"
        "@Test public void testFoo() { Assert.assertTrue(true); }\n}\n"
    )


def run(
    before: str,
    after: str,
    tmp_path: Path,
    pristine_files: dict[str, str] | None = None,
    link: Path | None = None,
) -> set[str]:
    pristine, workspace = tmp_path / "p", tmp_path / "w"
    for root in (pristine, workspace):
        (root / "src/test/java/a").mkdir(parents=True)
        (root / "pom.xml").write_text("<project/>")
    (pristine / PATH).write_text(before)
    (workspace / PATH).write_text(after)
    for path, text in (pristine_files or {}).items():
        (pristine / path).parent.mkdir(parents=True, exist_ok=True)
        (pristine / path).write_text(text)
    if link is not None:
        os.symlink(link, pristine / "src/test/java/a/Guard.java")
    return {f.code for f in check([PATH], pristine, workspace, TARGET).findings}


def rule_field(type_name: str) -> str:
    return f"@Rule public {type_name} rule = new {type_name}();"


class TestNames:
    @pytest.mark.parametrize("name", ["RetryRule", "rerunner", "RepeatOnFail", "FlakyGuard",
                                      "IgnoreOnCi"])  # fmt: skip
    def test_retry_words_in_any_case(self, name):
        assert retry_named(name)

    @pytest.mark.parametrize("name", ["TemporaryFolder", "Timeout", "Retained"])
    def test_other_names(self, name):
        assert not retry_named(name)


class TestPackages:
    @pytest.mark.parametrize(
        ("fqn", "package"),
        [("org.springframework.retry.support.RetryTemplate", "org.springframework.retry"),
         ("dev.failsafe.RetryPolicy", "dev.failsafe"),
         ("okhttp3.internal.http.RetryAndFollowUpInterceptor", "okhttp3")],
    )  # fmt: skip
    def test_a_library_package_matches_on_whole_components(self, fqn, package):
        assert client_package(fqn) == package

    @pytest.mark.parametrize(
        "fqn", ["org.springframework.retryx.Template", "dev.failsafes.Policy", "okhttp3x.Client"]
    )
    def test_a_package_that_only_starts_the_same_does_not(self, fqn):
        assert client_package(fqn) is None

    def test_a_retry_template_in_a_test_is_a_client_retry(self, tmp_path):
        imports = "import org.springframework.retry.support.RetryTemplate;\n"
        after = java_class("RetryTemplate t = new RetryTemplate();", imports)
        assert CLIENT in run(java_class(imports=imports), after, tmp_path)

    def test_a_project_type_sharing_the_name_is_not(self, tmp_path):
        after = java_class("RetryPolicy p = new RetryPolicy();")
        assert CLIENT not in run(java_class(), after, tmp_path)


class TestSteps:
    def test_step_1_rejects_an_existing_retry_named_type(self, tmp_path):
        imports = "import com.example.RetryOnFailure;\n"
        after = java_class(rule_field("RetryOnFailure"), imports)
        assert run(java_class(imports=imports), after, tmp_path) == {EXISTING}

    def test_step_2_accepts_an_allowlisted_type(self, tmp_path):
        after = java_class(rule_field("TemporaryFolder"))
        assert run(java_class(), after, tmp_path) == set()

    def test_step_2_needs_a_single_import(self, tmp_path):
        imports = "import com.example.*;\n"
        after = java_class("@Rule public Foo rule = new Foo();", imports)
        assert run(java_class(imports=imports), after, tmp_path) == {UNKNOWN}

    def test_step_3_judges_a_created_type_by_its_code(self, tmp_path):
        created = "static class FlakyGuard implements TestRule { public Statement apply(" \
            "Statement b, Description d) { return b; } }"  # fmt: skip
        imports = (
            "import org.junit.runner.Description;\nimport org.junit.runners.model.Statement;\n"
        )
        after = java_class(f"{created}\n@Rule public FlakyGuard g = new FlakyGuard();", imports)
        assert run(java_class(imports=imports), after, tmp_path) == set()

    def test_step_3_rejects_a_created_type_that_retries(self, tmp_path):
        body = RETRYING_RULE.format(name="Guard").split("public class", 1)[1]
        imports = (
            "import org.junit.runner.Description;\nimport org.junit.runners.model.Statement;\n"
        )
        after = java_class(f"static class{body}\n@Rule public Guard g = new Guard();", imports)
        assert RETRY_RULE in run(java_class(imports=imports), after, tmp_path)

    def test_step_4_flags_an_unknown_type(self, tmp_path):
        imports = "import com.example.Watcher;\n"
        after = java_class(rule_field("Watcher"), imports)
        assert run(java_class(imports=imports), after, tmp_path) == {UNKNOWN}

    def test_a_retry_annotation_is_rejected(self, tmp_path):
        imports = "import org.junitpioneer.jupiter.RetryingTest;\n"
        after = java_class(imports=imports).replace("@Test public", "@RetryingTest(3) public")
        assert RETRY_ANNOTATION in run(java_class(imports=imports), after, tmp_path)

    def test_a_reference_already_there_is_not_judged(self, tmp_path):
        imports = "import com.example.Watcher;\n"
        source = java_class(rule_field("Watcher"), imports)
        after = source.replace("testFoo() {", "testFoo() { int x = 1;")
        assert run(source, after, tmp_path) == set()


class TestWildcards:
    def test_a_wildcard_imported_rule_still_names_its_field_type(self, tmp_path):
        imports = "import org.junit.*;\nimport com.example.RetryOnFailure;\n"
        before = java_class(imports=imports).replace(HEAD, "package a;\n")
        after = java_class(rule_field("RetryOnFailure"), imports).replace(HEAD, "package a;\n")
        assert run(before, after, tmp_path) == {EXISTING}

    def test_a_name_only_test_library_wildcards_can_supply_is_a_library_type(self, tmp_path):
        imports = "import org.junit.*;\nimport org.junit.rules.*;\n"
        before = java_class(imports=imports).replace(HEAD, "package a;\n")
        after = java_class(rule_field("ExternalResource"), imports).replace(HEAD, "package a;\n")
        assert run(before, after, tmp_path) == set()

    def test_another_wildcard_leaves_it_unknown(self, tmp_path):
        imports = "import org.junit.*;\nimport com.example.*;\n"
        before = java_class(imports=imports).replace(HEAD, "package a;\n")
        after = java_class(rule_field("Watcher"), imports).replace(HEAD, "package a;\n")
        assert run(before, after, tmp_path) == {UNKNOWN}


class TestPristineRead:
    def rule(self, name: str = "Guard") -> dict[str, str]:
        return {f"src/test/java/a/{name}.java": RETRYING_RULE.format(name=name)}

    def test_a_same_package_rule_is_read_from_test_code(self, tmp_path):
        after = java_class(rule_field("Guard"))
        assert run(java_class(), after, tmp_path, self.rule()) == {RETRY_RULE}

    def test_then_from_main_code(self, tmp_path):
        files = {"src/main/java/a/Guard.java": RETRYING_RULE.format(name="Guard")}
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path, files) == {RETRY_RULE}

    def test_a_pristine_type_still_gets_the_name_rule(self, tmp_path):
        plain = "package a;\nimport org.junit.rules.ExternalResource;\n"
        plain += "public class RetryGuard extends ExternalResource { }\n"
        files = {"src/test/java/a/RetryGuard.java": plain}
        assert run(java_class(), java_class(rule_field("RetryGuard")), tmp_path, files) == {
            EXISTING
        }

    def test_a_pristine_wrapper_around_a_retry_named_delegate(self, tmp_path):
        wrapper = """package a;
import org.junit.rules.TestRule;
import org.junit.runner.Description;
import org.junit.runners.model.Statement;
import com.example.RetryRule;
public class Guard implements TestRule {
    private final RetryRule inner = new RetryRule(3);
    public Statement apply(Statement base, Description d) { return inner.apply(base, d); }
}
"""
        files = {"src/test/java/a/Guard.java": wrapper}
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path, files) == {EXISTING}

    def test_an_unread_supertype_is_flagged(self, tmp_path):
        rule = "package a;\nimport com.example.Base;\npublic class Guard extends Base { }\n"
        files = {"src/test/java/a/Guard.java": rule}
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path, files) == {UNKNOWN}

    def test_a_missing_file_is_flagged(self, tmp_path):
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path) == {UNKNOWN}

    def test_a_type_in_both_roots_is_flagged_not_guessed(self, tmp_path):
        files = {**self.rule(), "src/main/java/a/Guard.java": RETRYING_RULE.format(name="Guard")}
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path, files) == {UNKNOWN}

    def test_the_package_must_match(self, tmp_path):
        files = {"src/test/java/a/Guard.java": RETRYING_RULE.format(name="Guard").replace(
            "package a;", "package b;")}  # fmt: skip
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path, files) == {UNKNOWN}

    def test_the_declaration_must_match(self, tmp_path):
        files = {"src/test/java/a/Guard.java": RETRYING_RULE.format(name="Other")}
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path, files) == {UNKNOWN}

    def test_another_module_is_never_read(self, tmp_path):
        files = {"other/src/test/java/a/Guard.java": RETRYING_RULE.format(name="Guard")}
        assert run(java_class(), java_class(rule_field("Guard")), tmp_path, files) == {UNKNOWN}

    def test_a_symlinked_file_is_a_checker_error(self, tmp_path):
        outside = tmp_path / "outside.java"
        outside.write_text(RETRYING_RULE.format(name="Guard"))
        found = run(java_class(), java_class(rule_field("Guard")), tmp_path, link=outside)
        assert found == {"checker/checker_error"}

    def test_an_unparsable_file_is_unparsable(self, tmp_path):
        files = {"src/test/java/a/Guard.java": "package a; public class Guard {"}
        found = run(java_class(), java_class(rule_field("Guard")), tmp_path, files)
        assert found == {"checker/unparsable"}

    def test_a_read_past_the_file_bound_is_a_checker_error(self, tmp_path):
        names = [f"Guard{n}" for n in range(21)]
        imports = "".join(f"import a.{n};\n" for n in names)
        fields = "\n".join(f"@Rule public {n} r{i} = new {n}();" for i, n in enumerate(names))
        files = {f"src/test/java/a/{n}.java": RETRYING_RULE.format(name=n) for n in names}
        found = run(java_class(imports=imports), java_class(fields, imports), tmp_path, files)
        assert found == {"checker/checker_error"}


WILDCARD = "package a;\nimport org.junit.*;\nimport org.junit.rules.*;\n"


def wildcard_class(fields: str = "") -> str:
    return java_class(fields).replace(HEAD, WILDCARD)


class TestUnreadNamesAreNeverAccepted:
    @pytest.mark.parametrize("name", ["Con", "Aux", "Nul", "Com1", "Guard$X"])
    def test_a_name_the_path_rules_refuse_is_flagged(self, tmp_path, name):
        files = {f"src/test/java/a/{name}.java": RETRYING_RULE.format(name=name)}
        found = run(wildcard_class(), wildcard_class(rule_field(name)), tmp_path, files)
        assert found == {UNKNOWN}

    def test_a_non_ascii_name_is_flagged(self, tmp_path):
        found = run(wildcard_class(), wildcard_class(rule_field("W\u00e4chter")), tmp_path)
        assert found == {UNKNOWN}

    def test_the_name_rule_comes_before_the_wildcard_reading(self, tmp_path):
        found = run(wildcard_class(), wildcard_class(rule_field("FlakyGuard")), tmp_path)
        assert found == {EXISTING}

    def test_a_repeated_test_from_a_wildcard_is_not_a_retry(self, tmp_path):
        head = "package a;\nimport org.junit.jupiter.api.*;\n"
        before = head + "public class FooTest {\n@Test void testFoo() { }\n}\n"
        after = before.replace("@Test void", "@RepeatedTest(3) void")
        assert EXISTING not in run(before, after, tmp_path)

    SPIN = """package a;
import org.junit.jupiter.api.extension.InvocationInterceptor;
public class Spin implements InvocationInterceptor { }
"""
    SHADOW = """package a;
import org.junit.jupiter.api.extension.ExtendWith;
@ExtendWith(Spin.class)
public @interface RepeatedTest { int value(); }
"""

    def test_a_repeated_test_shadowed_in_both_roots_is_flagged(self, tmp_path):
        head = "package a;\nimport org.junit.jupiter.api.*;\n"
        before = head + "public class FooTest {\n@Test void testFoo() { }\n}\n"
        after = before.replace("}\n}", "}\n@RepeatedTest(2) void other() { }\n}")
        files = {
            f"src/{root}/java/a/{name}.java": text
            for root in ("test", "main")
            for name, text in (("RepeatedTest", self.SHADOW), ("Spin", self.SPIN))
        }
        assert run(before, after, tmp_path, files) == {UNKNOWN}

    def test_an_ignore_rule_shadowed_in_both_roots_is_flagged(self, tmp_path):
        files = {
            f"src/{root}/java/a/Ignore.java": RETRYING_RULE.format(name="Ignore")
            for root in ("test", "main")
        }
        found = run(wildcard_class(), wildcard_class(rule_field("Ignore")), tmp_path, files)
        assert found == {UNKNOWN}

    def test_a_file_gone_at_the_read_is_a_checker_error(self, tmp_path, monkeypatch):
        from rootflake.gate.checker import check as check_module

        real = check_module.read_side

        def vanished(root, path):
            return None if path.endswith("Guard.java") else real(root, path)

        monkeypatch.setattr(check_module, "read_side", vanished)
        files = {"src/test/java/a/Guard.java": RETRYING_RULE.format(name="Guard")}
        found = run(wildcard_class(), wildcard_class(rule_field("Guard")), tmp_path, files)
        assert found == {"checker/checker_error"}


class TestClassLiterals:
    EXTENSION = """package a;
import org.junit.jupiter.api.extension.InvocationInterceptor;
public class Guard implements InvocationInterceptor { }
"""

    @pytest.mark.parametrize(
        "annotation", ["@ExtendWith(Guard.class)", "@ExtendWith({Guard.class})",
                       "@ExtendWith(value = Guard.class)"],
    )  # fmt: skip
    def test_each_form_names_the_type(self, tmp_path, annotation):
        imports = "import org.junit.jupiter.api.extension.ExtendWith;\n"
        files = {"src/test/java/a/Guard.java": self.EXTENSION}
        before = java_class(imports=imports)
        after = java_class(imports=imports, header=annotation)
        assert run(before, after, tmp_path, files) == {"retry/invocation_interceptor"}

    @pytest.mark.parametrize(
        ("annotation", "imports"),
        [("@RunWith(Unknown.class)", "import org.junit.runner.RunWith;\nimport b.Unknown;\n"),
         ("@Listeners(Unknown.class)",
          "import org.testng.annotations.Listeners;\nimport b.Unknown;\n")],
    )  # fmt: skip
    def test_an_unknown_runner_or_listener_is_flagged(self, tmp_path, annotation, imports):
        before = java_class(imports=imports)
        after = java_class(imports=imports, header=annotation)
        assert run(before, after, tmp_path) == {UNKNOWN}

    def test_a_retry_named_runner_is_rejected(self, tmp_path):
        imports = "import org.junit.runner.RunWith;\nimport b.RetryRunner;\n"
        before = java_class(imports=imports)
        after = java_class(imports=imports, header="@RunWith(RetryRunner.class)")
        assert run(before, after, tmp_path) == {EXISTING}


class TestUnderTest:
    def test_a_file_inside_the_library_it_uses_is_flagged(self, tmp_path):
        head = "package dev.failsafe.internal;\nimport org.junit.Test;\n"
        head += "import dev.failsafe.RetryPolicy;\n"
        path = "src/test/java/dev/failsafe/internal/PolicyTest.java"
        before = f"{head}public class PolicyTest {{\n@Test public void testFoo() {{ }}\n}}\n"
        after = before.replace("{ }", "{ RetryPolicy p = RetryPolicy.ofDefaults(); }")
        pristine, workspace = tmp_path / "p", tmp_path / "w"
        for root, text in ((pristine, before), (workspace, after)):
            (root / path).parent.mkdir(parents=True)
            (root / "pom.xml").write_text("<project/>")
            (root / path).write_text(text)
        target = Target("dev.failsafe.internal.PolicyTest", "testFoo")
        found = {f.code for f in check([path], pristine, workspace, target).findings}
        assert UNDER_TEST in found and CLIENT not in found
