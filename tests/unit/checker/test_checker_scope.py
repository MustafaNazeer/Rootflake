import pytest

from rootflake.gate.checker.lexer import tokenize
from rootflake.gate.checker.scope import Binding, Program, is_test_code
from rootflake.gate.checker.syntax import parse


def unit(source: str):
    return parse(tokenize(source))


def program(**files: str) -> Program:
    return Program({path.replace("__", "/") + ".java": unit(src) for path, src in files.items()})


class TestIsTestCode:
    @pytest.mark.parametrize(
        "path",
        [
            "src/test/java/a/FooTest.java",
            "core/src/test/java/a/Foo.java",
            "test-infra/src/main/java/a/Helper.java",
            "my-tests/src/main/java/a/Helper.java",
            "foo-testing/src/main/java/a/Helper.java",
            "src/main/java/org/example/testing/Helper.java",
            "src/main/java/org/example/Tests/Helper.java",
            "integration-test/src/main/java/a/Helper.java",
        ],
    )
    def test_a_test_path_is_test_code(self, path):
        assert is_test_code(path, unit("package a; class Helper {}"))

    @pytest.mark.parametrize(
        "source",
        [
            "import org.junit.Assert; class H {}",
            "import static org.assertj.core.api.Assertions.assertThat; class H {}",
            "import org.hamcrest.*; class H {}",
            "import junit.framework.TestCase; class H {}",
            "import org.testng.annotations.Test; class H {}",
            "import org.awaitility.Awaitility; class H {}",
            "import org.mockito.Mockito; class H {}",
            "import org.skyscreamer.jsonassert.JSONAssert; class H {}",
            "import com.google.common.truth.Truth; class H {}",
            "class H { void f() { org.junit.Assert.fail(); } }",
        ],
    )
    def test_a_test_library_reference_makes_production_paths_test_code(self, source):
        assert is_test_code("src/main/java/a/H.java", unit(source))

    @pytest.mark.parametrize(
        ("path", "source"),
        [
            ("src/main/java/a/Clock.java", "import java.util.List; class Clock {}"),
            ("src/main/java/a/Clock.java", "import org.junitx.Thing; class Clock {}"),
            ("src/main/java/a/Clock.java", "class Clock { void f() { org.junitish.X.y(); } }"),
            ("contest/src/main/java/a/Clock.java", "class Clock {}"),
            ("src/main/java/a/Attest.java", "class Attest {}"),
        ],
    )
    def test_other_production_code_is_not_test_code(self, path, source):
        assert not is_test_code(path, unit(source))


ANNOTATION_SITE = "@Override"


def at(program: Program, path: str, text: str, occurrence: int = 1) -> int:
    tokens = program.units[path].tokens
    seen = 0
    for i, token in enumerate(tokens):
        if token.text == text:
            seen += 1
            if seen == occurrence:
                return i
    raise AssertionError(f"{text} not found")


class TestTypeResolution:
    PATH = "src/test/java/a/T.java"

    def resolve(self, source: str, name: str, occurrence: int = 1, **others: str) -> Binding:
        p = program(src__test__java__a__T=source, **others)
        return p.resolve_type(self.PATH, name, at(p, self.PATH, name, occurrence))

    def test_a_single_type_import_names_the_library_type(self):
        b = self.resolve("package a; import org.junit.Ignore; class T { @Ignore void t() {} }",
                         "Ignore", 2)  # fmt: skip
        assert (b.kind, b.fqn) == ("library", "org.junit.Ignore")

    def test_a_qualified_name_resolves_without_an_import(self):
        p = program(src__test__java__a__T="package a; class T { @org.junit.Ignore void t() {} }")
        b = p.resolve_type(self.PATH, "org.junit.Ignore", 0)
        assert (b.kind, b.fqn) == ("library", "org.junit.Ignore")

    @pytest.mark.parametrize(
        "name", ["Deprecated", "FunctionalInterface", "Override", "SafeVarargs", "SuppressWarnings"]
    )
    def test_the_five_java_lang_annotations_are_java_lang(self, name):
        b = self.resolve(f"package a; class T {{ @{name} void t() {{}} }}", name)
        assert (b.kind, b.fqn) == ("java_lang", f"java.lang.{name}")

    def test_a_newer_java_lang_name_is_not_exempt(self):
        assert self.resolve("package a; class T { @Record void t() {} }", "Record").kind == (
            "unresolved"
        )

    def test_a_wildcard_import_never_shadows_java_lang(self):
        b = self.resolve("package a; import x.*; class T { @Override void t() {} }", "Override")
        assert b.kind == "java_lang"

    def test_a_wildcard_import_leaves_other_names_unresolved(self):
        b = self.resolve("package a; import org.junit.*; class T { @Ignore void t() {} }", "Ignore")
        assert b.kind == "unresolved"

    @pytest.mark.parametrize(
        ("source", "kind"),
        [
            ("package a; import x.Override; class T { @Override void t() {} }", "imported"),
            ("package a; import static x.Y.Override; class T { @Override void t() {} }",
             "imported"),
            ("package a; class T { @interface Override {} class N { @Override void t() {} } }",
             "declared"),
            ("package a; class T<Override> { @Override void t() {} }", "type_param"),
            ("package a; class T { <Override> void t(@Override int x) {} }", "type_param"),
            ("package a; class T { void t() { @interface Override {} @Override int x = 1; } }",
             "declared"),
        ],
        ids=["single_type_import", "single_static_import", "member_type", "class_type_param",
             "method_type_param", "local_type"],
    )  # fmt: skip
    def test_each_shadowing_source_hides_java_lang(self, source, kind):
        # The first occurrence declares or imports the name; the second is the use.
        assert self.resolve(source, "Override", 2).kind == kind

    def test_a_same_package_type_in_another_read_file_shadows(self):
        b = self.resolve(
            "package a; class T { @Override void t() {} }",
            "Override",
            src__test__java__a__Override="package a; @interface Override {}",
        )
        assert (b.kind, b.fqn) == ("declared", "a.Override")

    def test_an_inherited_member_type_from_a_read_supertype_shadows(self):
        b = self.resolve(
            "package a; class T extends Base { @Override void t() {} }",
            "Override",
            src__test__java__a__Base="package a; class Base { @interface Override {} }",
        )
        assert (b.kind, b.fqn) == ("declared", "a.Base.Override")

    def test_a_member_type_does_not_reach_its_own_class_header(self):
        b = self.resolve("package a; @Override class T { @interface Override {} }", "Override")
        assert b.kind == "java_lang"

    def test_a_type_declared_in_the_same_file(self):
        b = self.resolve("package a; class T { @Retry void t() {} } @interface Retry {}", "Retry")
        assert (b.kind, b.fqn) == ("declared", "a.Retry")

    def test_a_nested_annotation_type_through_its_outer_import(self):
        b = self.resolve(
            "package a; import org.apache.lucene.tests.util.LuceneTestCase; "
            "class T { @LuceneTestCase.AwaitsFix void t() {} }",
            "LuceneTestCase",
            2,
        )
        assert (b.kind, b.fqn) == ("imported", "org.apache.lucene.tests.util.LuceneTestCase")


class TestMethodResolution:
    PATH = "src/test/java/a/T.java"

    def resolve(self, source: str, name: str, occurrence: int = 1, **others: str) -> Binding:
        p = program(src__test__java__a__T=source, **others)
        return p.resolve_method(self.PATH, name, at(p, self.PATH, name, occurrence))

    def test_a_single_static_import(self):
        b = self.resolve(
            "package a; import static org.junit.Assert.assertEquals; "
            "class T { void t() { assertEquals(1, 1); } }",
            "assertEquals",
            2,
        )
        assert (b.kind, b.fqn) == ("library", "org.junit.Assert.assertEquals")

    def test_a_static_wildcard_import_of_a_library_class(self):
        b = self.resolve(
            "package a; import static org.junit.jupiter.api.Assertions.*; "
            "class T { void t() { assertTrue(x); } }",
            "assertTrue",
        )
        assert (b.kind, b.fqn) == ("library", "org.junit.jupiter.api.Assertions.assertTrue")

    def test_a_qualified_receiver_through_a_type_import(self):
        b = self.resolve(
            "package a; import org.junit.Assert; class T { void t() { Assert.fail(); } }",
            "fail",
        )
        assert (b.kind, b.fqn) == ("library", "org.junit.Assert.fail")

    def test_a_method_declared_in_a_read_file_wins(self):
        b = self.resolve(
            "package a; import static org.junit.Assert.*; class T extends Base { "
            "void t() { assertTrue(x); } }",
            "assertTrue",
            src__test__java__a__Base="package a; class Base { void assertTrue(boolean b) {} }",
        )
        assert b.kind == "declared"

    def test_an_unimported_name_is_unresolved(self):
        assert self.resolve("package a; class T { void t() { check(); } }", "check").kind == (
            "unresolved"
        )


def test_an_anonymous_class_with_a_qualified_supertype_resolves_without_recursion():
    p = program(
        src__test__java__a__T=(
            "package a; import x.AsyncCallbacks; class T { void t() { "
            "f(new AsyncCallbacks.DeleteCallback() { public void done() { h(); } }); } }"
        )
    )
    anonymous = next(d for d in p.all_types("src/test/java/a/T.java") if d.kind == "anonymous")
    assert p.supertypes("src/test/java/a/T.java", anonymous) == []
    assert (
        p.resolve_type("src/test/java/a/T.java", "Inner", anonymous.body_start + 1).kind
        == "unresolved"
    )
