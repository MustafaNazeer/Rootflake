from pathlib import Path

import pytest

from rootflake.gate.checker.check import Target, check

PATH = "src/test/java/a/FooTest.java"
TARGET = Target("a.FooTest", "testFoo")
IMPORTS = (
    "import org.junit.Test;\n"
    "import static org.junit.Assert.*;\n"
    "import static org.assertj.core.api.Assertions.assertThat;\n"
)


def codes(before: str, after: str, tmp_path: Path, extra: dict[str, str] | None = None) -> set[str]:
    pristine, workspace = tmp_path / "p", tmp_path / "w"
    files = {PATH: (before, after), **{k: (v, v) for k, v in (extra or {}).items()}}
    for root, side in ((pristine, 0), (workspace, 1)):
        for rel, sources in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(sources[side])
    result = check(sorted(files), pristine, workspace, TARGET)
    return {f.code for f in result.findings}


def body_of(statements: str, helpers: str = "") -> str:
    return (
        f"package a;\n{IMPORTS}public class FooTest {{\n"
        f"  @Test public void testFoo() {{\n    {statements}\n  }}\n{helpers}\n}}\n"
    )


REMOVED = "weaken_assertion/assertion_removed"


class TestRemoval:
    def test_unchanged_assertions_are_fine(self, tmp_path):
        body = body_of("int v = f(); assertEquals(1, v);")
        assert codes(body, body, tmp_path) == set()

    def test_a_deleted_assertion(self, tmp_path):
        before = body_of("int v = f(); assertEquals(1, v); assertTrue(ok());")
        after = body_of("int v = f(); assertEquals(1, v);")
        assert codes(before, after, tmp_path) == {REMOVED}

    def test_a_commented_out_assertion(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("// assertEquals(1, f());\n f();")
        assert codes(before, after, tmp_path) == {REMOVED}

    def test_a_deleted_chain_step(self, tmp_path):
        before = body_of("assertThat(list).hasSize(2).contains(x);")
        after = body_of("assertThat(list).hasSize(2);")
        assert codes(before, after, tmp_path) == {REMOVED}

    def test_a_deleted_java_assert_statement(self, tmp_path):
        before = body_of("assert f() == 1;")
        assert codes(before, body_of("f();"), tmp_path) == {REMOVED}

    def test_a_removed_verify(self, tmp_path):
        before = body_of("run(); verify(mock).call();")
        assert codes(before, body_of("run();"), tmp_path) == {REMOVED}

    def test_a_removed_call_to_an_unresolved_assertion_name_still_counts(self, tmp_path):
        before = body_of("assertSomethingCustom(f());")
        assert codes(before, body_of("f();"), tmp_path) == {REMOVED}

    def test_a_moved_unconditional_assertion_is_fine(self, tmp_path):
        before = body_of("assertEquals(1, f()); g();")
        after = body_of("g(); assertEquals(1, f());")
        assert codes(before, after, tmp_path) == set()

    def test_an_added_assertion_alone_is_fine(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("assertEquals(1, f()); assertTrue(ok());")
        assert codes(before, after, tmp_path) == set()


class TestNewGuards:
    @pytest.mark.parametrize(
        "after",
        ["if (ci()) { assertEquals(1, f()); }",
         "for (int i = 0; i < n; i++) { assertEquals(1, f()); }",
         "run(() -> assertEquals(1, f()));",
         "try { assertEquals(1, f()); } catch (AssertionError e) { log(e); }"],
        ids=["if", "loop", "lambda", "swallowing_try"],
    )  # fmt: skip
    def test_an_assertion_newly_placed_under_a_guard(self, tmp_path, after):
        before = body_of("assertEquals(1, f());")
        assert REMOVED in codes(before, body_of(after), tmp_path)

    def test_a_try_whose_catch_rethrows_is_not_a_guard(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("try { assertEquals(1, f()); } catch (IOException e) { throw e; }")
        assert codes(before, after, tmp_path) == set()

    def test_an_assertion_moved_out_of_a_guard_is_fine(self, tmp_path):
        before = body_of("if (ci()) { assertEquals(1, f()); }")
        after = body_of("assertEquals(1, f());")
        assert codes(before, after, tmp_path) == set()

    def test_a_changed_guard_condition_counts_as_removed(self, tmp_path):
        before = body_of("if (ci()) { assertEquals(1, f()); }")
        after = body_of("if (never()) { assertEquals(1, f()); }")
        assert REMOVED in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        "exit_", ["if (slow()) { return; }", "if (slow()) throw new IllegalStateException();"]
    )
    def test_a_new_exit_before_an_assertion(self, tmp_path, exit_):
        before = body_of("int v = f(); assertEquals(1, v);")
        after = body_of(f"int v = f(); {exit_} assertEquals(1, v);")
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_new_exit_after_the_assertions_is_fine_here(self, tmp_path):
        before = body_of("assertEquals(1, f()); g();")
        after = body_of("assertEquals(1, f()); if (x) { return; } g();")
        assert REMOVED not in codes(before, after, tmp_path)


class TestKindOne:
    def test_changed_arguments_of_the_same_assertion(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("assertEquals(2, f());")
        assert codes(before, after, tmp_path) == set()

    def test_changed_arguments_of_the_same_chain(self, tmp_path):
        before = body_of("assertThat(f()).isEqualTo(1);")
        after = body_of("assertThat(g()).isEqualTo(1);")
        assert REMOVED not in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        "after",
        ["assertEquals(v, v);", "assertEquals(1, 1);", "assertTrue(true);", "assertFalse(false);",
         "assertNotNull(new Object());", 'assertNotNull("x");'],
    )  # fmt: skip
    def test_trivially_true_arguments_are_refused(self, tmp_path, after):
        before = body_of("int v = f(); assertEquals(1, v); assertTrue(ok()); assertFalse(bad());"
                           " assertNotNull(obj());")  # fmt: skip
        kept = {
            "assertEquals": "assertTrue(ok()); assertFalse(bad()); assertNotNull(obj());",
            "assertTrue": "assertEquals(1, v); assertFalse(bad()); assertNotNull(obj());",
            "assertFalse": "assertEquals(1, v); assertTrue(ok()); assertNotNull(obj());",
            "assertNotNull": "assertEquals(1, v); assertTrue(ok()); assertFalse(bad());",
        }[after.split("(")[0]]
        assert REMOVED in codes(before, body_of(f"int v = f(); {kept} {after}"), tmp_path)

    def test_a_different_assertion_does_not_match(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("assertNotNull(f());")
        assert REMOVED in codes(before, after, tmp_path)


class TestCredit:
    def test_an_added_assertion_with_a_shadowing_declaration_gets_no_credit(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("assertEquals(2, f());", "  static void assertEquals(int a, int b) {}")
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_unimported_assertion_gets_no_credit(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("myAssertEquals(2, f());")
        assert REMOVED in codes(before, after, tmp_path)


class TestHelpers:
    def test_an_assertion_removed_from_a_helper(self, tmp_path):
        before = body_of("check(f());", "  private void check(int v) { assertEquals(1, v); }")
        after = body_of("check(f());", "  private void check(int v) { log(v); }")
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_removed_call_to_an_asserting_helper(self, tmp_path):
        helper = "  private void check(int v) { assertEquals(1, v); }"
        before = body_of("check(f());", helper)
        after = body_of("f();", helper)
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_assertion_moved_into_a_helper_keeps_its_credit(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("check(f());", "  private void check(int v) { assertEquals(1, v); }")
        assert REMOVED not in codes(before, after, tmp_path)

    def test_a_deleted_helper_that_held_assertions(self, tmp_path):
        before = body_of("check(f());", "  private void check(int v) { assertEquals(1, v); }")
        after = body_of("f();")
        assert REMOVED in codes(before, after, tmp_path)


class TestScanReachedAssertions:
    HELPER = "  static class H { void send(int v) { assertEquals(1, v); } }"

    def test_an_unchanged_method_reaching_a_helper_by_name_is_fine(self, tmp_path):
        before = body_of("pipeline().send(f());", self.HELPER)
        after = body_of("pipeline().send(f());", self.HELPER + " int other() { return 2; }")
        assert codes(before, after, tmp_path) == set()

    def test_a_helper_reached_only_by_name_gives_no_credit_for_a_changed_assertion(self, tmp_path):
        before = body_of("assertEquals(1, f());", self.HELPER)
        after = body_of("pipeline().send(f());", self.HELPER.replace("assertEquals(1, v)",
                        "assertEquals(2, v)"))  # fmt: skip
        assert REMOVED in codes(before, after, tmp_path)


class TestExitIdentity:
    def test_an_existing_exit_under_a_changed_condition_is_not_new(self, tmp_path):
        before = body_of("for (int i = 0; i < 10; i++) { if (done()) break; } assertTrue(ok());")
        after = body_of("for (int i = 0; i < 50; i++) { if (done()) break; } assertTrue(ok());")
        assert REMOVED not in codes(before, after, tmp_path)

    def test_an_exit_that_becomes_unconditional_is_new(self, tmp_path):
        before = body_of("if (done()) { return; } assertTrue(ok());")
        after = body_of("return; ")
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_formerly_unguarded_exit_now_guarded_counts_as_new(self, tmp_path):
        before = body_of("assertTrue(ok()); return;")
        after = body_of("if (slow()) { return; } assertTrue(ok());")
        assert REMOVED in codes(before, after, tmp_path)
