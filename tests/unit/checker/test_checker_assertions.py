from pathlib import Path

import pytest

from rootflake.gate.checker.check import Target, check, target_paths

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


def jupiter_body(statements: str, helpers: str = "") -> str:
    """A JUnit 5 test, whose assertions take the message last."""
    return body_of(statements, helpers).replace(
        "import static org.junit.Assert.*;", "import static org.junit.jupiter.api.Assertions.*;"
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
        after = body_of("assertFalse(g());")
        assert REMOVED in codes(before, after, tmp_path)


class TestCredit:
    def test_an_added_assertion_with_a_shadowing_declaration_gets_no_credit(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("assertEquals(2, f());", "  static void assertEquals(int a, int b) {}")
        assert "weaken_assertion/assertion_rebound" in codes(before, after, tmp_path)

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
    def test_an_existing_exit_under_a_changed_condition_counts_as_newly_guarded(self, tmp_path):
        # Mustafa 2026-10-23: an exit is identified by its statement and its full guard text.
        before = body_of("if (x()) { return; } assertTrue(ok());")
        after = body_of("if (true) { return; } assertTrue(ok());")
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_exit_that_becomes_unconditional_is_new(self, tmp_path):
        before = body_of("if (done()) { return; } assertTrue(ok());")
        after = body_of("return; ")
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_formerly_unguarded_exit_now_guarded_counts_as_new(self, tmp_path):
        before = body_of("assertTrue(ok()); return;")
        after = body_of("if (slow()) { return; } assertTrue(ok());")
        assert REMOVED in codes(before, after, tmp_path)


class TestReviewFindings:
    HELPER = "  private void check(int v) { assertEquals(1, v); }"

    def test_a_helper_call_newly_placed_under_a_guard(self, tmp_path):
        before = body_of("check(f());", self.HELPER)
        after = body_of("if (ci()) { check(f()); }", self.HELPER)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_new_exit_before_a_helper_call(self, tmp_path):
        before = body_of("check(f());", self.HELPER)
        after = body_of("if (slow()) { return; } check(f());", self.HELPER)
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_assertion_reached_by_name_from_another_helper_does_not_stand_in(self, tmp_path):
        helpers = (
            "  private void check(int v) { assertEquals(1, v); }\n"
            "  static class Other { void check(int v) { assertEquals(1, v); } }"
        )
        before = body_of("check(f());", helpers)
        after = body_of("other.check(f());", helpers)
        assert REMOVED in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertTrue(ok(v));", "assertTrue(1 == 1);"),
         ("assertTrue(ok(v));", "assertTrue((true));"),
         ("assertTrue(ok(v));", "assertTrue(v == v);"),
         ("assertEquals(1, v);", "assertEquals((v), v);"),
         ("assertEquals(1, v);", "assertEquals(1 + 0, 1);")],
    )  # fmt: skip
    def test_more_trivially_true_forms(self, tmp_path, before, after):
        old = body_of(f"int v = f(); {before}")
        assert REMOVED in codes(old, body_of(f"int v = f(); {after}"), tmp_path)

    def test_a_real_kind_one_change_is_still_a_match(self, tmp_path):
        old = body_of("int v = f(); assertEquals(1, v);")
        assert REMOVED not in codes(old, body_of("int v = f(); assertEquals(2, v);"), tmp_path)


@pytest.mark.parametrize("literal", ['"a b"', "'x'", '"if (x) y"'])
def test_a_string_literal_with_spaces_is_constant_for_assert_not_null(tmp_path, literal):
    before = body_of("int v = f(); assertNotNull(obj(v));")
    after = body_of(f"int v = f(); assertNotNull({literal});")
    assert REMOVED in codes(before, after, tmp_path)


IMPORTS_7B = (
    "import org.junit.Test;\n"
    "import static org.junit.Assert.*;\n"
    "import static org.assertj.core.api.Assertions.*;\n"
    "import static org.hamcrest.Matchers.*;\n"
    "import static org.mockito.Mockito.*;\n"
    "import static org.awaitility.Awaitility.await;\n"
    "import org.skyscreamer.jsonassert.JSONAssert;\n"
    "import org.skyscreamer.jsonassert.JSONCompareMode;\n"
)


def body_7b(statements: str) -> str:
    return (
        f"package a;\n{IMPORTS_7B}public class FooTest {{\n"
        f"  @Test public void testFoo() {{\n    {statements}\n  }}\n}}\n"
    )


def codes_7b(before: str, after: str, tmp_path: Path) -> set[str]:
    return codes(body_7b(before), body_7b(after), tmp_path)


class TestKindTwo:
    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThat(xs).containsExactly(1, 2);",
          "assertThat(xs).containsExactlyInAnyOrder(1, 2);"),
         ("assertThat(xs).containsExactlyElementsOf(ys);",
          "assertThat(xs).containsExactlyInAnyOrderElementsOf(ys);"),
         ("assertThat(m).containsExactlyEntriesOf(n);",
          "assertThat(m).containsExactlyInAnyOrderEntriesOf(n);"),
         ("assertThat(xs, contains(1, 2));", "assertThat(xs, containsInAnyOrder(1, 2));"),
         ("assertThat(xs).containsExactly(1, 2).inOrder();",
          "assertThat(xs).containsExactly(1, 2);"),
         ("JSONAssert.assertEquals(e, a, JSONCompareMode.STRICT);",
          "JSONAssert.assertEquals(e, a, JSONCompareMode.NON_EXTENSIBLE);")],
        ids=["assertj", "elements_of", "entries_of", "hamcrest", "truth_in_order", "jsonassert"],
    )  # fmt: skip
    def test_an_allowlisted_order_relaxation_is_a_match(self, tmp_path, before, after):
        assert codes_7b(before, after, tmp_path) == set()

    def test_a_relaxation_on_other_values_is_not_a_match(self, tmp_path):
        before = "assertThat(xs).containsExactly(1, 2);"
        after = "assertThat(xs).containsExactlyInAnyOrder(1);"
        assert REMOVED in codes_7b(before, after, tmp_path)

    def test_jsonassert_lenient_is_not_a_match(self, tmp_path):
        before = "JSONAssert.assertEquals(e, a, JSONCompareMode.STRICT);"
        after = "JSONAssert.assertEquals(e, a, JSONCompareMode.LENIENT);"
        assert codes_7b(before, after, tmp_path) != set()


class TestKindThree:
    def test_an_assertion_moved_into_until_asserted(self, tmp_path):
        before = "assertEquals(1, f());"
        after = "await().atMost(5, SECONDS).untilAsserted(() -> assertEquals(1, f()));"
        assert REMOVED not in codes_7b(before, after, tmp_path)

    @pytest.mark.parametrize(
        "condition", ["() -> collect.size() == count", "() -> count == collect.size()",
                      "() -> collect.size().equals(count)"]
    )  # fmt: skip
    def test_an_equality_moved_into_until(self, tmp_path, condition):
        before = "assertEquals(count, collect.size());"
        after = f"await().atMost(5, SECONDS).until({condition});"
        assert REMOVED not in codes_7b(before, after, tmp_path)

    def test_an_assertion_moved_into_some_other_lambda_is_removed(self, tmp_path):
        before = "assertEquals(1, f());"
        after = "later(() -> assertEquals(1, f()));"
        assert REMOVED in codes_7b(before, after, tmp_path)


RELAXED = "weaken_assertion/equality_relaxed"


class TestNamedWeakenings:
    @pytest.mark.parametrize(
        "after",
        ["assertThat(row.get()).isGreaterThanOrEqualTo(1L);", "assertNotNull(row.get());",
         "assertThat(row.get()).isNotNull();", "assertThat(row.get(), notNullValue());",
         'assertTrue(row.get().startsWith("CREATE"));', 'assertTrue(row.get().contains("x"));'],
    )  # fmt: skip
    def test_an_equality_relaxed(self, tmp_path, after):
        assert codes_7b("assertEquals(1L, row.get());", after, tmp_path) == {RELAXED}

    def test_an_equality_relaxed_to_a_summary(self, tmp_path):
        before = "assertEquals(expected, list);"
        after = "assertEquals(expected.size(), list.size());"
        assert codes_7b(before, after, tmp_path) == {RELAXED}

    def test_a_disjunction_added_to_assert_true(self, tmp_path):
        before = "assertTrue(v == 1);"
        assert codes_7b(before, "assertTrue(v == 1 || v == 2);", tmp_path) == {RELAXED}

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertEquals(1.0, d, 0.01);", "assertEquals(1.0, d, 0.1);"),
         ("assertEquals(1.0, d);", "assertEquals(1.0, d, 0.1);"),
         ("assertThat(c).isBetween(n - 100, n + 100);",
          "assertThat(c).isBetween(n - 1000, n + 1000);"),
         ("assertThat(d).isCloseTo(1.0, within(0.01));",
          "assertThat(d).isCloseTo(1.0, within(0.5));"),
         ("assertThat(d, closeTo(1.0, 0.01));", "assertThat(d, closeTo(1.0, 0.5));")],
    )  # fmt: skip
    def test_a_widened_tolerance(self, tmp_path, before, after):
        assert codes_7b(before, after, tmp_path) == {"weaken_assertion/tolerance_widened"}

    def test_a_narrower_tolerance_is_a_match(self, tmp_path):
        assert (
            codes_7b("assertEquals(1.0, d, 0.1);", "assertEquals(1.0, d, 0.01);", tmp_path) == set()
        )

    @pytest.mark.parametrize("mode", ["atLeastOnce()", "atLeast(1)", "atMost(3)", "atMostOnce()"])
    def test_a_relaxed_verification_mode(self, tmp_path, mode):
        before = "verify(listener, times(2)).onRead(x);"
        after = f"verify(listener, {mode}).onRead(x);"
        assert codes_7b(before, after, tmp_path) == {"weaken_assertion/verification_mode_relaxed"}

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThrows(IllegalArgumentException.class, () -> f());",
          "assertThrows(Exception.class, () -> f());"),
         ("assertThatThrownBy(() -> f()).isInstanceOf(RateLimitException.class);",
          "assertThatThrownBy(() -> f()).isInstanceOf(RuntimeException.class);"),
         ("assertThatThrownBy(() -> f()).isExactlyInstanceOf(IOException.class);",
          "assertThatThrownBy(() -> f()).isInstanceOf(IOException.class);")],
    )  # fmt: skip
    def test_a_broadened_expected_exception(self, tmp_path, before, after):
        assert codes_7b(before, after, tmp_path) == {
            "weaken_assertion/expected_exception_broadened"
        }

    def test_unchecked_soft_assertions(self, tmp_path):
        before = "assertThat(results).hasSize(3);"
        after = (
            "SoftAssertions softly = new SoftAssertions(); softly.assertThat(results).hasSize(3);"
        )
        assert codes_7b(before, after, tmp_path) == {"weaken_assertion/soft_assertions_unchecked"}

    def test_soft_assertions_checked_with_assert_all_are_a_match(self, tmp_path):
        before = "assertThat(results).hasSize(3);"
        after = ("SoftAssertions softly = new SoftAssertions(); "
                 "softly.assertThat(results).hasSize(3);"
                 " softly.assertAll();")  # fmt: skip
        assert codes_7b(before, after, tmp_path) == set()


REBOUND = "weaken_assertion/assertion_rebound"


class TestRebinding:
    def test_a_local_method_shadowing_a_used_assertion(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = body_of("assertEquals(1, f());", "  static void assertEquals(int a, int b) {}")
        assert REBOUND in codes(before, after, tmp_path)

    def test_a_changed_import_of_a_used_assertion(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = before.replace(
            "import static org.junit.Assert.*;", "import static org.example.Lenient.assertEquals;"
        )
        assert REBOUND in codes(before, after, tmp_path)

    def test_a_new_same_package_type_named_like_a_used_library_class(self, tmp_path):
        source = body_of("Assert.assertEquals(1, f());").replace(
            "import org.junit.Test;", "import org.junit.Test;\nimport org.junit.*;"
        )
        created = {
            "src/test/java/a/Assert.java":
                "package a; class Assert { static void assertEquals(int a, int b) {} }"
        }  # fmt: skip
        assert REBOUND in _codes_with_new_file(source, created, tmp_path)

    def test_an_added_import_unused_by_existing_assertions_is_fine(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = before.replace(
            "import org.junit.Test;", "import org.junit.Test;\nimport java.util.List;"
        )
        assert codes(before, after, tmp_path) == set()

    def test_a_changed_package_declaration(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = before.replace("package a;", "package b;")
        assert REBOUND in codes(before, after, tmp_path)

    def test_a_class_with_assertions_changing_its_supertype(self, tmp_path):
        helper = "  static class Checker extends Base { void check() { assertTrue(ok()); } }"
        before = body_of("new Checker().check();", helper)
        after = body_of("new Checker().check();", helper.replace("extends Base", "extends Other"))
        assert REBOUND in codes(before, after, tmp_path)


def _codes_with_new_file(source: str, created: dict[str, str], root: Path) -> set[str]:
    pristine, workspace = root / "p", root / "w"
    for side in (pristine, workspace):
        (side / PATH).parent.mkdir(parents=True, exist_ok=True)
        (side / PATH).write_text(source)
    for rel, text in created.items():
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text(text)
    result = check(sorted([PATH, *created]), pristine, workspace, TARGET)
    return {f.code for f in result.findings}


def test_a_created_file_whose_package_does_not_match_its_directory(tmp_path):
    created = {"src/test/java/a/support/Waits.java": "package a.other; class Waits {}"}
    assert REBOUND in _codes_with_new_file(body_of("assertEquals(1, f());"), created, tmp_path)


class TestAssertionFlags:
    EQUALS = (
        "  static class Plan { int v; public boolean equals(Object o) { "
        "return o instanceof Plan && ((Plan) o).v == v; } }"
    )

    def test_an_added_equals_beside_a_changed_assertion_is_flagged(self, tmp_path):
        before = body_of('assertEquals("{v:1}", json());', "  static class Plan { int v; }")
        after = body_of("assertEquals(expectedPlan, actualPlan);", self.EQUALS)
        assert "flag/equals_changed" in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        "body", ["return true;", "return o instanceof Plan;", "return getClass() == o.getClass();"]
    )
    def test_an_equals_that_reads_no_state_is_rejected(self, tmp_path, body):
        before = body_of('assertEquals("{v:1}", json());', "  static class Plan { int v; }")
        tautology = f"  static class Plan {{ int v; public boolean equals(Object o) {{ {body} }} }}"
        after = body_of("assertEquals(expectedPlan, actualPlan);", tautology)
        assert "weaken_assertion/tautological_equals" in codes(before, after, tmp_path)

    def test_a_replaced_subject_is_flagged(self, tmp_path):
        before = body_of("assertEquals(1, counter.records());")
        after = body_of("assertEquals(1, other.size());")
        assert "flag/assertion_subject_replaced" in codes(before, after, tmp_path)

    def test_a_corrected_literal_is_not_a_replaced_subject(self, tmp_path):
        before = body_of("assertEquals(1, counter.records());")
        after = body_of("assertEquals(2, counter.records());")
        assert codes(before, after, tmp_path) == set()


class TestThirdReview:
    @pytest.mark.parametrize(
        "condition",
        ["() -> a == b | ok()", "() -> !a == b", "() -> a == b == c"],
        ids=["bitwise_or", "negation", "chained"],
    )
    def test_a_compound_until_condition_is_not_an_equality(self, tmp_path, condition):
        before = "assertEquals(a, b);"
        after = f"await().atMost(5, SECONDS).until({condition});"
        assert REMOVED in codes_7b(before, after, tmp_path)

    def test_a_locally_declared_until_is_not_a_wait(self, tmp_path):
        before = body_7b("assertEquals(1, f());")
        after = body_7b("until(() -> assertEquals(1, f()));").replace(
            "  }\n}\n", "  }\n  static void until(Runnable r) {}\n}\n"
        )
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_assert_all_before_the_soft_assertion_does_not_check_it(self, tmp_path):
        before = "assertThat(results).hasSize(3);"
        after = ("SoftAssertions softly = new SoftAssertions(); softly.assertAll(); "
                 "softly.assertThat(results).hasSize(3);")  # fmt: skip
        assert codes_7b(before, after, tmp_path) == {"weaken_assertion/soft_assertions_unchecked"}

    def test_a_guarded_assert_all_does_not_check(self, tmp_path):
        before = "assertThat(results).hasSize(3);"
        after = (
            "SoftAssertions softly = new SoftAssertions(); "
            "softly.assertThat(results).hasSize(3); if (ci()) { softly.assertAll(); }"
        )
        assert codes_7b(before, after, tmp_path) == {"weaken_assertion/soft_assertions_unchecked"}


class TestSubjectFlagPrecision:
    def test_a_local_defined_only_by_literals_has_no_subject_to_replace(self, tmp_path):
        before = body_of(
            "boolean found = false; for (Row r : rows) { found = true; } assertTrue(found);"
        )
        after = body_of(
            "int idx = -1; for (Row r : rows) { idx = r.index(); } assertTrue(idx >= 0);"
        )
        assert "flag/assertion_subject_replaced" not in codes(before, after, tmp_path)

    def test_a_subject_followed_through_a_local_definition_is_kept(self, tmp_path):
        before = body_of(
            "Long[] ns = map.keySet().toArray(new Long[0]); assertEquals(1, p.parse(ns[0]));"
        )
        after = body_of("assertEquals(0, other.get(4L)); assertEquals(1, p.parse(4));")
        assert "flag/assertion_subject_replaced" not in codes(before, after, tmp_path)


class TestFourthReview:
    def test_an_assert_all_after_an_exit_does_not_check(self, tmp_path):
        before = "assertThat(results).hasSize(3);"
        after = (
            "SoftAssertions softly = new SoftAssertions(); "
            "softly.assertThat(results).hasSize(3); if (ci()) { return; } softly.assertAll();"
        )
        assert "weaken_assertion/soft_assertions_unchecked" in codes_7b(before, after, tmp_path)

    def test_a_changed_equals_is_flagged_even_with_unchanged_assertions(self, tmp_path):
        helper = (
            "  static class P { int v; public boolean equals(Object o) { return ((P) o).v == v; } }"
        )
        before = body_of("assertEquals(a(), b());", helper)
        after = body_of(
            "assertEquals(a(), b());", helper.replace("((P) o).v == v", "((P) o).v >= v")
        )
        assert "flag/equals_changed" in codes(before, after, tmp_path)

    def test_a_variable_named_like_await_is_not_a_wait_root(self, tmp_path):
        before = body_7b("assertEquals(1, f());")
        after = body_7b("Waiter await = waiter(); await.until(() -> assertEquals(1, f()));")
        assert REMOVED in codes(before, after, tmp_path)


def test_a_deleted_assertion_using_a_rebound_name_is_removed_not_rebound(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = body_of("f();", "  static void assertEquals(int a, int b) {}")
    result = codes(before, after, tmp_path)
    assert REMOVED in result and REBOUND not in result


def test_a_skip_exception_thrown_through_a_variable_is_not_an_exit(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = body_of('if (ci()) { SkipException flaky = new SkipException("x"); throw flaky; } '
                    "assertEquals(1, f());")  # fmt: skip
    assert REMOVED not in codes(before, after, tmp_path)


def test_a_swapped_type_wildcard_rebinds_when_a_supertype_comes_through_one(tmp_path):
    source = (
        body_of("assertEquals(1, f());")
        .replace("public class FooTest {", "public class FooTest extends Base {")
        .replace("import org.junit.Test;", "import org.junit.Test;\nimport x.*;")
    )
    after = source.replace("import x.*;", "import y.*;")
    assert REBOUND in codes(source, after, tmp_path)


def test_a_reassigned_skip_local_is_still_an_exit(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = body_of('RuntimeException e = new SkipException("x"); e = new IllegalStateException(); '
                    "if (ci()) { throw e; } assertEquals(1, f());")  # fmt: skip
    assert REMOVED in codes(before, after, tmp_path)


def test_a_qualified_skip_local_thrown_later_is_not_an_exit(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = body_of('final org.testng.SkipException e = new org.testng.SkipException("x"); '
                    "if (ci()) { throw e; } assertEquals(1, f());")  # fmt: skip
    assert REMOVED not in codes(before, after, tmp_path)


@pytest.mark.parametrize(
    "middle",
    ['if ((e = new IllegalStateException()) != null) { g(); }',
     "for (int i = 0; i < 1; e = new IllegalStateException()) { i++; }",
     "e += null;"],
    ids=["if_header", "for_update", "compound"],
)  # fmt: skip
def test_a_skip_local_reassigned_anywhere_is_still_an_exit(tmp_path, middle):
    before = body_of("assertEquals(1, f());")
    after = body_of(f'RuntimeException e = new SkipException("x"); {middle} '
                    "if (ci()) { throw e; } assertEquals(1, f());")  # fmt: skip
    assert REMOVED in codes(before, after, tmp_path)


def test_a_skip_local_with_more_after_the_constructor_is_still_an_exit(tmp_path):
    before = body_of("assertEquals(1, f());")
    initializer = 'ok() ? new SkipException("x") : new IllegalStateException()'
    after = body_of(
        f"RuntimeException e = {initializer}; if (ci()) {{ throw e; }} assertEquals(1, f());"
    )
    assert REMOVED in codes(before, after, tmp_path)


@pytest.mark.parametrize(
    "middle",
    ["try { g(); } catch (IllegalStateException e) { if (ci()) { throw e; } }",
     "for (RuntimeException e : errors()) { if (ci()) { throw e; } }",
     "if (thing() instanceof IllegalStateException e && ci()) { throw e; }",
     "run((a, e) -> g()); if (ci()) { throw e; }",
     "Runnable r = () -> { Object e = g(); }; if (ci()) { throw e; }"],
    ids=["catch", "for_each", "instanceof", "lambda", "lambda_local"],
)  # fmt: skip
def test_a_skip_local_name_bound_again_is_still_an_exit(tmp_path, middle):
    before = body_of("assertEquals(1, f());")
    after = body_of(f'if (ok()) {{ RuntimeException e = new SkipException("x"); g(e); }} {middle} '
                    "assertEquals(1, f());")  # fmt: skip
    assert REMOVED in codes(before, after, tmp_path)


def test_a_skip_local_out_of_scope_at_the_throw_is_still_an_exit(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = body_of('if (ok()) { RuntimeException e = new SkipException("x"); } '
                    "if (ci()) { throw e; } assertEquals(1, f());")  # fmt: skip
    assert REMOVED in codes(before, after, tmp_path)


def test_a_skip_local_thrown_before_its_declaration_is_still_an_exit(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = body_of('if (ci()) { throw e; } RuntimeException e = new SkipException("x"); '
                    "assertEquals(1, f());")  # fmt: skip
    assert REMOVED in codes(before, after, tmp_path)


AWAIT_IMPORTS = (
    "import static org.awaitility.Awaitility.await;\nimport static org.hamcrest.Matchers.equalTo;\n"
)
MOCKITO_IMPORTS = "import static org.mockito.Mockito.*;\n"


def with_imports(source: str, imports: str) -> str:
    return source.replace("import org.junit.Test;\n", f"import org.junit.Test;\n{imports}")


class TestLegitimateChangesKeepTheirMatch:
    def test_a_changed_exact_assertion_pairs_before_a_later_unchanged_twin(self, tmp_path):
        tail = 'assertTrue(s().contains("2020"));'
        before = body_of(f'assertEquals("2020-01-01 10:00", s()); {tail}')
        after = body_of(f'assertEquals("2020-01-01 05:00", s()); {tail}')
        assert codes(before, after, tmp_path) == set()

    @pytest.mark.parametrize(
        ("after", "side"),
        [('assertEquals("why", e(), a());', body_of),
         ('assertEquals(e(), a(), "why");', jupiter_body),
         ('assertEquals(e(), a(), () -> "why " + a());', jupiter_body),
         ('assertEquals("why not " + n(), e(), a());', body_of)],
        ids=["junit4_message", "junit5_message", "junit5_supplier", "concatenated"],
    )  # fmt: skip
    def test_an_added_message_is_not_a_tolerance(self, tmp_path, after, side):
        assert codes(side("assertEquals(e(), a());"), side(after), tmp_path) == set()

    def test_a_message_beside_a_condition_is_not_trivially_true(self, tmp_path):
        before = jupiter_body('assertTrue(x().isDone(), "m");')
        after = jupiter_body('assertTrue(y().isDone(), "m");')
        assert REMOVED not in codes(before, after, tmp_path)

    @pytest.mark.parametrize("call", ['as("x")', 'describedAs("x")', 'withFailMessage("x")'])
    def test_an_added_description_keeps_the_assertion(self, tmp_path, call):
        before = body_of("assertThat(a()).isEqualTo(1);")
        after = body_of(f"assertThat(a()).{call}.isEqualTo(1);")
        assert codes(before, after, tmp_path) == set()

    def test_a_timeout_wrapped_verification_mode_is_kept(self, tmp_path):
        before = with_imports(body_of("run(); verify(m(), times(1)).call();"), MOCKITO_IMPORTS)
        after = with_imports(
            body_of("run(); verify(m(), timeout(1000).times(1)).call();"), MOCKITO_IMPORTS
        )
        assert codes(before, after, tmp_path) == set()

    def test_a_timeout_wrapped_loose_mode_is_relaxed(self, tmp_path):
        before = with_imports(body_of("run(); verify(m()).call();"), MOCKITO_IMPORTS)
        after = with_imports(
            body_of("run(); verify(m(), timeout(1000).atLeastOnce()).call();"), MOCKITO_IMPORTS
        )
        assert "weaken_assertion/verification_mode_relaxed" in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        "after",
        ["await().until(() -> server().isRunning());", "await().until(server()::isRunning);"],
        ids=["lambda", "method_reference"],
    )  # fmt: skip
    def test_assert_true_moved_into_a_wait_is_kept(self, tmp_path, after):
        before = with_imports(body_of("assertTrue(server().isRunning());"), AWAIT_IMPORTS)
        after_source = with_imports(body_of(after), AWAIT_IMPORTS)
        if "::" in after:
            before = before.replace("server().isRunning()", "server.isRunning()")
            after_source = after_source.replace("server()::", "server::")
        assert codes(before, after_source, tmp_path) == set()

    def test_until_with_an_equal_to_matcher_is_kept(self, tmp_path):
        before = with_imports(body_of("assertEquals(3, count());"), AWAIT_IMPORTS)
        after = with_imports(body_of("await().until(() -> count(), equalTo(3));"), AWAIT_IMPORTS)
        assert codes(before, after, tmp_path) == set()

    def test_a_changed_helper_is_not_reached_by_a_call_of_another_arity(self, tmp_path):
        helper = (
            "  static void parse() { assertEquals(1, f()); }\n"
            '  @Test public void other() { parser().parse("x"); }'
        )
        before = body_of("g();", helper)
        after = body_of("g();", helper.replace("assertEquals(1, f())", "assertEquals(2, f())"))
        assert codes(before, after, tmp_path) == set()


BROADENED = "weaken_assertion/expected_exception_broadened"
TOLERANCE = "weaken_assertion/tolerance_widened"
RELAXED = "weaken_assertion/equality_relaxed"
HAMCREST_IMPORTS = (
    "import static org.hamcrest.Matchers.*;\nimport static org.hamcrest.MatcherAssert.assertThat;\n"
)


class TestWeakeningsTheTableNames:
    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThatExceptionOfType(java.io.IOException.class).isThrownBy(() -> f());",
          "assertThatExceptionOfType(Exception.class).isThrownBy(() -> f());"),
         ("assertThrows(java.io.IOException.class, () -> f());",
          "assertThrows(java.lang.Exception.class, () -> f());"),
         ("assertThatThrownBy(() -> f()).isInstanceOf(java.io.IOException.class);",
          "assertThatThrownBy(() -> f()).isInstanceOf(java.lang.RuntimeException.class);")],
        ids=["exception_of_type", "qualified_assert_throws", "qualified_is_instance_of"],
    )  # fmt: skip
    def test_a_broadened_exception_type(self, tmp_path, before, after):
        assert BROADENED in codes(body_of(before), body_of(after), tmp_path)

    def test_a_broadened_hamcrest_instance_of(self, tmp_path):
        before = body_of("assertThat(error(), instanceOf(java.io.IOException.class));")
        after = body_of("assertThat(error(), instanceOf(Throwable.class));")
        before, after = (
            s.replace("import static org.assertj.core.api.Assertions.assertThat;\n", "")
            for s in (before, after)
        )
        before, after = (with_imports(s, HAMCREST_IMPORTS) for s in (before, after))
        assert BROADENED in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        ("old", "new"),
        [("@Test(expected = java.io.IOException.class)", "@Test(expected = Exception.class)"),
         ("@Test(expectedExceptions = java.io.IOException.class)",
          "@Test(expectedExceptions = Throwable.class)")],
        ids=["junit4_expected", "testng_expected_exceptions"],
    )  # fmt: skip
    def test_a_broadened_annotation_expectation(self, tmp_path, old, new):
        before = body_of("f();").replace("@Test public", f"{old} public")
        after = body_of("f();").replace("@Test public", f"{new} public")
        assert BROADENED in codes(before, after, tmp_path)

    def test_a_removed_annotation_expectation_is_removed(self, tmp_path):
        before = body_of("f();").replace("@Test public", "@Test(expected = E.class) public")
        assert REMOVED in codes(before, body_of("f();"), tmp_path)

    def test_an_annotation_expectation_moved_into_assert_throws_is_kept(self, tmp_path):
        before = body_of("f();").replace("@Test public", "@Test(expected = E.class) public")
        after = body_of("assertThrows(E.class, () -> f());")
        assert codes(before, after, tmp_path) == set()

    def test_a_broadened_expected_exception_rule(self, tmp_path):
        rule = "  @org.junit.Rule public ExpectedException thrown = ExpectedException.none();"
        imports = "import org.junit.rules.ExpectedException;\n"
        before = with_imports(
            body_of("thrown.expect(java.io.IOException.class); f();", rule), imports
        )
        after = with_imports(body_of("thrown.expect(Exception.class); f();", rule), imports)
        assert BROADENED in codes(before, after, tmp_path)

    def test_a_broadened_caught_type_before_fail(self, tmp_path):
        before = body_of('try { f(); fail("x"); } catch (java.io.IOException e) { }')
        after = body_of('try { f(); fail("x"); } catch (Exception e) { }')
        assert BROADENED in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThat(a()).isBetween(5, 10);", "assertThat(a()).isBetween(0, 10);"),
         ("assertTrue(a() > 5 && a() < 10);", "assertTrue(a() > 0 && a() < 100);"),
         ("assertThat(a()).isGreaterThan(5).isLessThan(10);",
          "assertThat(a()).isGreaterThan(0).isLessThan(100);"),
         ('assertEquals("m", 1.0, d(), 0.01);', 'assertEquals("m", 1.0, d(), 0.5);'),
         ('assertEquals("m", 1.0, d());', 'assertEquals("m", 1.0, d(), 0.5);'),
         ("assertEquals(1.0, d(), 0.01);", "assertEquals(1.0, d(), EPS);"),
         ("assertThat(d()).isCloseTo(1.0, within(0.01));",
          "double tol = 0.5; assertThat(d()).isCloseTo(1.0, within(tol));")],
        ids=["interval", "two_sided_condition", "two_sided_chain", "message_delta",
             "message_added_delta", "constant", "local"],
    )  # fmt: skip
    def test_a_widened_tolerance(self, tmp_path, before, after):
        helper = "  static final double EPS = 0.5;"
        assert TOLERANCE in codes(body_of(before, helper), body_of(after, helper), tmp_path)

    def test_a_tolerance_named_by_an_equal_constant_is_kept(self, tmp_path):
        helper = "  static final double EPS = 0.01;"
        before = body_of("assertEquals(1.0, d(), 0.01);", helper)
        after = body_of("assertEquals(1.0, d(), EPS);", helper)
        assert codes(before, after, tmp_path) == set()

    def test_both_sides_converted_to_a_set(self, tmp_path):
        before = body_of("List<String> a = e(); List<String> b = f(); assertEquals(a, b);")
        after = body_of(
            "List<String> a = e(); List<String> b = f(); "
            "assertEquals(new HashSet<>(a), new HashSet<>(b));"
        )
        assert RELAXED in codes(before, after, tmp_path)

    def test_a_set_conversion_of_declared_sets_is_kept(self, tmp_path):
        before = body_of("Set<String> a = e(); Set<String> b = f(); assertEquals(a, b);")
        after = body_of(
            "Set<String> a = e(); Set<String> b = f(); "
            "assertEquals(new HashSet<>(a), new HashSet<>(b));"
        )
        assert codes(before, after, tmp_path) == set()

    def test_json_assert_strict_true_to_false(self, tmp_path):
        imports = "import org.skyscreamer.jsonassert.JSONAssert;\n"
        before = with_imports(body_of("JSONAssert.assertEquals(e(), a(), true);"), imports)
        after = with_imports(body_of("JSONAssert.assertEquals(e(), a(), false);"), imports)
        assert codes(before, after, tmp_path) & {REMOVED, RELAXED}

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThat(v()).isEqualTo(3);", "assertThat(v()).isEqualTo(v());"),
         ("assertThat(ok()).isTrue();", "assertThat(true).isTrue();"),
         ('assertEquals("m", a(), b());', 'assertEquals("m", a(), a());'),
         ("assertTrue(x().equals(y()));", "assertTrue(x().equals(y()) | true);")],
        ids=["assertj_self", "assertj_literal", "message_self", "single_bar_true"],
    )  # fmt: skip
    def test_a_trivially_true_replacement_is_refused(self, tmp_path, before, after):
        assert REMOVED in codes(body_of(before), body_of(after), tmp_path)

    def test_a_trivially_true_hamcrest_replacement_is_refused(self, tmp_path):
        before, after = (
            with_imports(
                body_of(s).replace(
                    "import static org.assertj.core.api.Assertions.assertThat;\n", ""
                ),
                HAMCREST_IMPORTS,
            )
            for s in ("assertThat(v(), is(3));", "assertThat(v(), is(v()));")
        )
        assert REMOVED in codes(before, after, tmp_path)


@pytest.mark.parametrize(
    "delta", ['"0.5".length()', '"abc".length() + 1'], ids=["member", "member_then_plus"]
)
def test_a_delta_that_starts_with_a_string_literal_is_not_a_message(tmp_path, delta):
    before = body_of("assertEquals(1.0, d());")
    after = body_of(f"assertEquals(1.0, d(), {delta});")
    assert TOLERANCE in codes(before, after, tmp_path)


class TestSynthesizedFactsFailClosed:
    def test_a_try_ending_in_a_project_fail_earns_no_credit(self, tmp_path):
        helper = "  static void fail(String m) {}"
        before = body_of("assertThrows(java.io.IOException.class, () -> f());", helper)
        after = body_of('try { f(); fail("x"); } catch (java.io.IOException e) { }', helper)
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_unresolved_test_annotation_expectation_earns_no_credit(self, tmp_path):
        before = body_of("assertThrows(E.class, () -> f());")
        after = (
            body_of("f();")
            .replace("import org.junit.Test;\n", "import x.Test;\n")
            .replace("@Test public", "@Test(expected = E.class) public")
        )
        assert REMOVED in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        "helper",
        ["  static double EPS = 0.01;\n  static void grow() { EPS = 0.5; }",
         "  static double EPS = 0.01;\n  static void grow() { EPS++; }"],
        ids=["reassigned_elsewhere", "incremented_elsewhere"],
    )  # fmt: skip
    def test_a_tolerance_constant_changed_elsewhere_has_no_value(self, tmp_path, helper):
        before = body_of("assertEquals(1.0, d(), 0.01);", helper)
        after = body_of("assertEquals(1.0, d(), EPS);", helper)
        assert TOLERANCE in codes(before, after, tmp_path)

    def test_a_local_list_shadowing_a_set_field_is_not_a_set(self, tmp_path):
        helper = "  static Set<String> a = null;\n  static Set<String> b = null;"
        before = body_of("List<String> a = e(); assertEquals(a, b);", helper)
        after = body_of(
            "List<String> a = e(); assertEquals(new HashSet<>(a), new HashSet<>(b));", helper
        )
        assert RELAXED in codes(before, after, tmp_path)


def test_a_tolerance_field_assigned_through_this_has_no_value(tmp_path):
    helper = "  double eps = 0.01;\n  void grow() { this.eps = 0.5; }"
    before = body_of("assertEquals(1.0, d(), 0.01);", helper)
    after = body_of("assertEquals(1.0, d(), eps);", helper)
    assert TOLERANCE in codes(before, after, tmp_path)


@pytest.mark.parametrize(
    "middle",
    ["for (List<String> a : lists()) { g(a); }", "try (Res a = res()) { g(a); }",
     "if (o() instanceof List<?> a) { g(a); }"],
    ids=["for_each", "try_resource", "pattern"],
)  # fmt: skip
def test_a_name_declared_again_in_a_header_is_not_a_set(tmp_path, middle):
    helper = "  static Set<String> a = null;\n  static Set<String> b = null;"
    before = body_of(f"{middle} assertEquals(a, b);", helper)
    after = body_of(f"{middle} assertEquals(new HashSet<>(a), new HashSet<>(b));", helper)
    assert RELAXED in codes(before, after, tmp_path)


SINGLE_IMPORTS = (
    "import org.junit.Test;\nimport static org.junit.Assert.assertEquals;\n"
    "import static org.junit.Assert.assertTrue;\n"
)


def single_body(statements: str, extra_imports: str = "") -> str:
    return (
        f"package a;\n{SINGLE_IMPORTS}{extra_imports}public class FooTest {{\n"
        f"  @Test public void testFoo() {{\n    {statements}\n  }}\n}}\n"
    )


class TestPerNameRebinding:
    def test_a_static_wildcard_added_beside_single_imports_rebinds_nothing(self, tmp_path):
        before = single_body("assertEquals(1, f());")
        after = single_body("assertEquals(1, f());", "import static org.awaitility.Awaitility.*;\n")
        assert codes(before, after, tmp_path) == set()

    def test_a_type_wildcard_replaced_by_its_single_imports_rebinds_nothing(self, tmp_path):
        statements = 'assertEquals(List.of("x"), names(String.valueOf(1)));'
        before = single_body(statements, "import java.util.*;\n")
        after = single_body(statements, "import java.util.List;\n")
        assert codes(before, after, tmp_path) == set()

    def test_a_static_wildcard_replaced_by_singles_from_the_same_class_rebinds_nothing(
        self, tmp_path
    ):
        before = body_of("assertEquals(1, f());")
        after = before.replace(
            "import static org.junit.Assert.*;", "import static org.junit.Assert.assertEquals;"
        )
        assert codes(before, after, tmp_path) == set()

    def test_a_type_wildcard_added_alone_rebinds_no_type_name(self, tmp_path):
        before = single_body("assertEquals(Limits.MAX, f());")
        after = single_body("assertEquals(Limits.MAX, f());", "import x.*;\n")
        assert codes(before, after, tmp_path) == set()

    def test_a_static_wildcard_added_beside_another_rebinds_its_member_names(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = before.replace(
            "import org.junit.Test;\n", "import org.junit.Test;\nimport static x.Lenient.*;\n"
        )
        assert REBOUND in codes(before, after, tmp_path)

    def test_a_single_type_import_moved_to_another_package_rebinds_it(self, tmp_path):
        before = single_body("assertEquals(Limits.MAX, f());", "import x.Limits;\n")
        after = single_body("assertEquals(Limits.MAX, f());", "import y.Limits;\n")
        assert REBOUND in codes(before, after, tmp_path)


class TestMoreAssertionForms:
    @pytest.mark.parametrize(
        ("imports", "field", "statement"),
        [("import org.junit.rules.ErrorCollector;\nimport static org.hamcrest.Matchers.is;\n",
          "  @org.junit.Rule public ErrorCollector collector = new ErrorCollector();",
          "collector.checkThat(f(), is(1));"),
         ("import org.junit.rules.ErrorCollector;\n",
          "  @org.junit.Rule public ErrorCollector collector = new ErrorCollector();",
          "collector.checkSucceeds(() -> f());"),
         ("import static org.assertj.core.api.BDDAssertions.then;\n", "",
          "then(f()).isEqualTo(1);"),
         ("import org.assertj.core.api.BDDAssertions;\n", "",
          "BDDAssertions.then(f()).isEqualTo(1);"),
         ("import static org.mockito.BDDMockito.then;\nimport static org.mockito.Mockito.times;\n",
          "", "then(m()).should(times(1)).call();"),
         ("import com.google.common.truth.Expect;\n",
          "  @org.junit.Rule public final Expect expect = Expect.create();",
          "expect.that(f()).isEqualTo(1);")],
        ids=["error_collector_check_that", "error_collector_check_succeeds", "bdd_then_static",
             "bdd_then_qualified", "bdd_mockito_should", "truth_expect"],
    )  # fmt: skip
    def test_a_deleted_assertion_of_another_form_is_removed(
        self, tmp_path, imports, field, statement
    ):
        before = with_imports(body_of(f"{statement} g();", field), imports)
        after = with_imports(body_of("g();", field), imports)
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_unresolved_then_is_not_an_assertion(self, tmp_path):
        before = single_body("then(f()).isEqualTo(1); g();")
        assert codes(before, single_body("g();"), tmp_path) == set()

    def test_a_bdd_mockito_mode_relaxed(self, tmp_path):
        imports = (
            "import static org.mockito.BDDMockito.then;\nimport static org.mockito.Mockito.*;\n"
        )
        before = with_imports(body_of("then(m()).should(times(1)).call();"), imports)
        after = with_imports(body_of("then(m()).should(atLeastOnce()).call();"), imports)
        assert "weaken_assertion/verification_mode_relaxed" in codes(before, after, tmp_path)


def test_a_static_wildcard_change_rebinds_a_name_another_file_declares(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = before.replace(
        "import org.junit.Test;\n", "import org.junit.Test;\nimport static x.Lenient.*;\n"
    )
    other = {"src/test/java/b/Other.java": "package b; class Other { void assertEquals() {} }"}
    assert REBOUND in codes(before, after, tmp_path, other)


def test_a_single_import_that_shadows_a_same_package_type_rebinds_it(tmp_path):
    before = single_body("assertEquals(Limits.MAX, f());", "import x.*;\n")
    after = single_body("assertEquals(Limits.MAX, f());", "import x.Limits;\n")
    same_package = {
        "src/test/java/a/Limits.java": "package a; class Limits { static int MAX = 1; }"
    }
    assert REBOUND in codes(before, after, tmp_path, same_package)


def test_a_removed_assertion_named_like_a_method_elsewhere_is_removed(tmp_path):
    other = {"src/test/java/b/Other.java": "package b; class Other { void assertEquals() {} }"}
    before = body_of("assertEquals(1, f()); g();")
    assert REMOVED in codes(before, body_of("g();"), tmp_path, other)


class TestCollectionTextToOrderFree:
    @pytest.mark.parametrize(
        ("before", "after", "imports"),
        [('assertEquals("[a, b]", names().toString());',
          'assertThat(names()).containsExactlyInAnyOrder("b", "a");', ""),
         ('assertEquals("[1, 2, 2]", ids().toString());',
          "assertThat(ids()).containsExactlyInAnyOrder(2, 1, 2);", ""),
         ('assertEquals("{a=1, b=2}", map().toString());',
          'assertThat(map()).containsOnly(entry("b", 2), entry("a", 1));',
          "import static org.assertj.core.api.Assertions.entry;\n")],
        ids=["list", "duplicates", "map"],
    )  # fmt: skip
    def test_the_same_elements_in_any_order_are_kept(self, tmp_path, before, after, imports):
        old, new = (with_imports(body_of(s), imports) for s in (before, after))
        assert codes(old, new, tmp_path) == set()

    def test_hamcrest_contains_in_any_order_is_kept(self, tmp_path):
        old = body_of('assertEquals("[a, b]", names().toString());')
        new = with_imports(
            body_of('assertThat(names(), containsInAnyOrder("a", "b"));').replace(
                "import static org.assertj.core.api.Assertions.assertThat;\n", ""
            ),
            HAMCREST_IMPORTS,
        )
        assert codes(old, new, tmp_path) == set()

    @pytest.mark.parametrize(
        "after",
        ['assertThat(names()).containsExactlyInAnyOrder("a");',
         'assertThat(names()).contains("a", "b");',
         'assertThat(others()).containsExactlyInAnyOrder("a", "b");',
         'assertThat(names()).containsExactlyInAnyOrder("a", "b", "c");'],
        ids=["fewer", "contains_only_some", "other_subject", "more"],
    )  # fmt: skip
    def test_anything_else_is_removed(self, tmp_path, after):
        before = body_of('assertEquals("[a, b]", names().toString());')
        assert codes(before, body_of(after), tmp_path) & {REMOVED, RELAXED}

    def test_elements_the_split_cannot_read_are_removed(self, tmp_path):
        before = body_of('assertEquals("[x, y]", names().toString());')
        after = body_of('assertThat(names()).containsExactlyInAnyOrder("x, y");')
        assert REMOVED in codes(before, after, tmp_path)


def test_a_condition_moved_into_a_wait_keeps_its_subject(tmp_path):
    helper = "  Cursor cursor;"
    before = with_imports(body_of('assertTrue("m", cursor.isReady());', helper), AWAIT_IMPORTS)
    after = with_imports(body_of("await().until(() -> cursor.isReady());", helper), AWAIT_IMPORTS)
    assert codes(before, after, tmp_path) == set()


QUALIFIED = (
    "import org.mockito.Mockito;\nimport org.hamcrest.Matchers;\n"
    "import org.hamcrest.CoreMatchers;\nimport org.hamcrest.MatcherAssert;\n"
)


class TestSecondReview:
    @pytest.mark.parametrize(
        ("before", "after", "code"),
        [("Mockito.verify(m(), Mockito.times(1)).foo();",
          "Mockito.verify(m(), Mockito.atLeastOnce()).foo();",
          "weaken_assertion/verification_mode_relaxed"),
         ("MatcherAssert.assertThat(v(), Matchers.equalTo(1));",
          "MatcherAssert.assertThat(v(), Matchers.anything());", None),
         ('MatcherAssert.assertThat(l(), Matchers.contains("a", "b"));',
          'MatcherAssert.assertThat(l(), Matchers.hasItem("a"));', None),
         ("MatcherAssert.assertThat(v(), CoreMatchers.is(1));",
          "MatcherAssert.assertThat(v(), CoreMatchers.not(CoreMatchers.nullValue()));", None)],
        ids=["mockito_mode", "hamcrest_anything", "hamcrest_has_item", "core_matchers_not_null"],
    )  # fmt: skip
    def test_a_qualified_factory_change_is_judged(self, tmp_path, before, after, code):
        result = codes(with_imports(body_of(before), QUALIFIED),
                       with_imports(body_of(after), QUALIFIED), tmp_path)  # fmt: skip
        assert (code in result) if code else (result & {REMOVED, RELAXED})

    def test_a_removed_qualified_bdd_then_is_removed(self, tmp_path):
        before = single_body("org.assertj.core.api.BDDAssertions.then(v()).isEqualTo(1); g();")
        assert REMOVED in codes(before, single_body("g();"), tmp_path)

    def test_a_qualified_timed_mode_keeps_the_mode(self, tmp_path):
        before = with_imports(body_of("verify(m(), times(1)).foo();"), MOCKITO_IMPORTS + QUALIFIED)
        after = with_imports(
            body_of("verify(m(), Mockito.timeout(1000).times(1)).foo();"),
            MOCKITO_IMPORTS + QUALIFIED,
        )
        assert codes(before, after, tmp_path) == set()

    @pytest.mark.parametrize(
        ("before", "after"),
        [('try { f(); fail("x"); } catch (java.io.IOException e) { }',
          'try { f(); fail("x"); } catch (java.io.IOException | RuntimeException e) { }'),
         ("@Test(expectedExceptions = IllegalStateException.class)",
          "@Test(expectedExceptions = {IllegalStateException.class, RuntimeException.class})")],
        ids=["multi_catch", "expected_exceptions_array"],
    )  # fmt: skip
    def test_a_broad_type_added_beside_the_old_one_is_broadened(self, tmp_path, before, after):
        if before.startswith("@"):
            old = body_of("f();").replace("@Test public", f"{before} public")
            new = body_of("f();").replace("@Test public", f"{after} public")
        else:
            old, new = body_of(before), body_of(after)
        assert BROADENED in codes(old, new, tmp_path)

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThat(f().exists()).isTrue();",
          'assertThat(new java.io.File(d(), "x").exists()).isTrue();'),
         ("assertNotNull(svc().call());", "assertNotNull(new Svc().call());")],
        ids=["assertj_is_true", "assert_not_null"],
    )  # fmt: skip
    def test_a_call_on_a_new_object_is_not_trivially_true(self, tmp_path, before, after):
        assert codes(body_of(before), body_of(after), tmp_path) == set()

    @pytest.mark.parametrize(
        ("before", "after", "helper"),
        [("assertTrue(a().equals(b()));", "assertTrue(b().equals(b()));", ""),
         ("assertTrue(Objects.equals(a(), b()));", "assertTrue(Objects.equals(b(), b()));", ""),
         ("collector.checkThat(v(), equalTo(1));", "collector.checkThat(v(), equalTo(v()));",
          "  @org.junit.Rule public ErrorCollector collector = new ErrorCollector();")],
        ids=["equals_self", "objects_equals_self", "error_collector_self"],
    )  # fmt: skip
    def test_a_self_comparison_is_trivially_true(self, tmp_path, before, after, helper):
        imports = (
            "import org.junit.rules.ErrorCollector;\nimport static org.hamcrest.Matchers.equalTo;\n"
        )
        old = with_imports(body_of(before, helper), imports)
        new = with_imports(body_of(after, helper), imports)
        assert REMOVED in codes(old, new, tmp_path)

    def test_a_json_assert_mode_is_not_a_tolerance(self, tmp_path):
        imports = "import org.skyscreamer.jsonassert.JSONAssert;\n"
        before = with_imports(body_of("assertEquals(e(), a());"), imports)
        after = with_imports(body_of("JSONAssert.assertEquals(e(), a(), true);"), imports)
        assert TOLERANCE not in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertEquals(e(), a());", "assertEquals(e(), a() == null ? e() : a());"),
         ("assertTrue(ok());", "assertTrue(flaky() ? true : ok());"),
         ("assertFalse(bad());", "assertFalse(flaky() ? false : bad());"),
         ("assertThat(a()).isEqualTo(e());", "assertThat(flaky() ? e() : a()).isEqualTo(e());")],
        ids=["assert_equals", "assert_true", "assert_false", "assertj"],
    )  # fmt: skip
    def test_a_conditional_that_can_pick_a_passing_value_is_trivially_true(
        self, tmp_path, before, after
    ):
        assert REMOVED in codes(body_of(before), body_of(after), tmp_path)

    def test_a_conditional_expected_value_is_kept(self, tmp_path):
        before = body_of('assertEquals("\\n", sep());')
        after = body_of('assertEquals(isWin() ? "\\r\\n" : "\\n", sep());')
        assert codes(before, after, tmp_path) == set()


@pytest.mark.parametrize(
    ("before", "after"),
    [("verify(m()).foo();", "verify(m(), timeout(1000).times(1)).foo();"),
     ("verify(m(), times(1)).foo();", "verify(m(), timeout(1000)).foo();"),
     ("verify(m()).foo();", "verify(m(), after(100)).foo();")],
    ids=["default_to_timed_times", "times_to_timeout", "default_to_after"],
)  # fmt: skip
def test_the_default_verification_mode_is_times_one(tmp_path, before, after):
    old = with_imports(body_of(before), MOCKITO_IMPORTS)
    new = with_imports(body_of(after), MOCKITO_IMPORTS)
    assert codes(old, new, tmp_path) == set()


def test_jsonassert_text_in_a_message_does_not_hide_a_tolerance(tmp_path):
    before = body_of("assertEquals(1.0, d());")
    after = body_of("assertEquals(1.0, d() + JSONAssert.offset(), 0.5);")
    assert TOLERANCE in codes(before, after, tmp_path)


@pytest.mark.parametrize(
    "created",
    ["new Object() { }", "new int[] { 1 }", "new int[3]",
     "new java.util.HashMap<String, Integer>()", "new String[2][3]"],
    ids=["anonymous_class", "array_initializer", "array", "generic", "two_dimensions"],
)  # fmt: skip
def test_a_creation_expression_is_never_null(tmp_path, created):
    before = body_of("assertNotNull(f());")
    assert REMOVED in codes(before, body_of(f"assertNotNull({created});"), tmp_path)


SUBJECT = "flag/assertion_subject_replaced"


class TestSubjectRoots:
    @pytest.mark.parametrize(
        ("before", "after", "imports"),
        [("assertEquals(3, q.size());", "assertEquals(3, Arrays.asList(1, 2, g()).size());", ""),
         ("assertTrue(s.isRunning());", "await().until(() -> t.isRunning());", AWAIT_IMPORTS),
         ('String s = f(); assertEquals("x", s);', 'String s = "x"; assertEquals("x", s);', "")],
        ids=["method_name_only", "wait_on_another_receiver", "local_now_literal"],
    )  # fmt: skip
    def test_a_replaced_subject_is_flagged(self, tmp_path, before, after, imports):
        helper = "  Queue q; Server s; Server t;"
        old = with_imports(body_of(before, helper), imports)
        new = with_imports(body_of(after, helper), imports)
        assert SUBJECT in codes(old, new, tmp_path)

    def test_the_same_root_through_another_method_is_not_flagged(self, tmp_path):
        helper = "  Queue q;"
        old = body_of("assertEquals(3, q.size());", helper)
        new = body_of("assertEquals(3, q.count());", helper)
        assert SUBJECT not in codes(old, new, tmp_path)

    def test_a_local_still_defined_by_a_call_is_not_flagged(self, tmp_path):
        old = body_of('String s = f(); assertEquals("x", s);')
        new = body_of('String s = g(); assertEquals("x", s);')
        assert SUBJECT not in codes(old, new, tmp_path)


def test_a_reject_carries_no_flags(tmp_path):
    helper = "  Queue q;"
    before = body_of("assertEquals(3, q.size()); assertTrue(ok());", helper)
    after = body_of("assertEquals(3, Arrays.asList(1, 2, 3).size());", helper)
    assert codes(before, after, tmp_path) == {REMOVED}


def test_type_names_are_not_roots():
    from rootflake.gate.checker.assertions import roots_of

    assert roots_of(("Collections . synchronizedList ( new ArrayList < String > ( ) )",)) == set()
    assert roots_of(("ScheduledTask . TaskStatus . COMPLETED , task . getStatus ( )",)) == {"task"}


def test_an_assertion_pairs_with_the_one_sharing_its_root():
    from rootflake.gate.checker.assertions import Assertion, _match

    def made(text, subject, line):
        return Assertion(text, ("assertEquals",), ("1", "x"), (), frozenset(), True, line, line,
                         subject=frozenset(subject))  # fmt: skip

    old_a, old_b = made("assertEquals ( 1 , a . size ( ) )", {"a"}, 1), made("b1", {"b"}, 2)
    new_b = made("assertEquals ( 2 , b . first ( ) )", {"b"}, 3)
    results = _match([old_a, old_b], [new_b])
    assert [(old.line, code) for old, code, _ in results] == [(1, REMOVED)]


def test_an_unknown_verification_mode_is_not_times_one(tmp_path):
    before = with_imports(body_of("verify(m(), times(1)).foo();"), MOCKITO_IMPORTS)
    after = with_imports(body_of("verify(m(), customMode()).foo();"), MOCKITO_IMPORTS)
    assert REMOVED in codes(before, after, tmp_path)


def test_a_constructor_reference_does_not_merge_arguments(tmp_path):
    before = body_of("assertEquals(1.0, d());")
    after = body_of("assertEquals(1.0, g(Foo::new) < 2 ? 1.0 : 0.0, 0.5);")
    assert TOLERANCE in codes(before, after, tmp_path)


class TestOneSidedBounds:
    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThat(x()).isGreaterThan(5);", "assertThat(x()).isGreaterThan(0);"),
         ("assertThat(x()).isLessThan(10);", "assertThat(x()).isLessThan(1000);"),
         ("assertTrue(x() > 5);", "assertTrue(x() > 0);"),
         ("assertTrue(x() > 5);", "assertTrue(x() >= 5);"),
         ("assertTrue(x() < 5);", "assertTrue(x() < LIMIT);"),
         ("assertThat(x()).isGreaterThan(5);", "assertThat(x()).isGreaterThanOrEqualTo(5);"),
         ("assertTrue(x() > 5);", "assertThat(x()).isGreaterThan(1);")],
        ids=["lower_lowered", "upper_raised", "condition_lowered", "made_inclusive",
             "unresolved_bound", "chain_made_inclusive", "condition_to_chain"],
    )  # fmt: skip
    def test_a_looser_bound_is_widened(self, tmp_path, before, after):
        assert TOLERANCE in codes(body_of(before), body_of(after), tmp_path)

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertThat(x()).isGreaterThan(5);", "assertThat(x()).isGreaterThan(6);"),
         ("assertTrue(x() >= 5);", "assertTrue(x() > 5);"),
         ("assertTrue(x() < 10);", "assertTrue(x() < 10 && x() > 0);")],
        ids=["raised_lower", "made_strict", "bound_added"],
    )  # fmt: skip
    def test_a_tighter_or_equal_bound_is_kept(self, tmp_path, before, after):
        assert TOLERANCE not in codes(body_of(before), body_of(after), tmp_path)

    @pytest.mark.parametrize(
        ("before", "after", "code"),
        [("assertTrue(x() > 5 && y() < 3);", "assertTrue(x() > 0);", REMOVED),
         ("assertTrue(x() > 5 && y() < 3);", "assertTrue(x() > 5);", REMOVED),
         ("assertTrue(x() > 5);", "assertTrue(ok());", REMOVED),
         ("assertTrue(x() > -1 && x() > t());", "assertTrue(x() > t());", TOLERANCE),
         ("assertTrue(x() > 0 && x() < 10);", "assertThat(x()).isGreaterThan(0);", TOLERANCE)],
        ids=["other_subject_dropped_and_lowered", "other_subject_dropped", "all_bounds_dropped",
             "second_bound_on_a_side_dropped", "side_dropped_across_forms"],
    )  # fmt: skip
    def test_a_dropped_bound_is_rejected(self, tmp_path, before, after, code):
        assert codes(body_of(before), body_of(after), tmp_path) == {code}

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertTrue(x() > 5);", "assertTrue(x() > 5 && z() > 1);"),
         ("assertTrue(x() > 5 && y() < 3);", "assertTrue(y() < 3 && x() > 6);")],
        ids=["subject_added", "reordered_and_raised"],
    )  # fmt: skip
    def test_bounds_kept_on_every_subject_pass(self, tmp_path, before, after):
        assert codes(body_of(before), body_of(after), tmp_path) == set()

    def test_a_hamcrest_bound_lowered(self, tmp_path):
        before, after = (
            with_imports(
                body_of(s).replace(
                    "import static org.assertj.core.api.Assertions.assertThat;\n", ""
                ),
                HAMCREST_IMPORTS,
            )
            for s in ("assertThat(x(), greaterThan(5));", "assertThat(x(), greaterThan(0));")
        )
        assert TOLERANCE in codes(before, after, tmp_path)


class TestTypedMessagesAndMigration:
    @pytest.mark.parametrize(
        ("after", "helper", "side"),
        [('assertEquals(e(), a(), String.format("got %s", a()));', "", jupiter_body),
         ('assertEquals(String.format("got %s", a()), e(), a());', "", body_of),
         ('assertEquals(e(), a(), java.text.MessageFormat.format("got {0}", a()));', "",
          jupiter_body),
         ('assertEquals(e(), a(), "got %s".formatted(a()));', "", jupiter_body),
         ("assertEquals(e(), a(), why);", "  static final String why = reason();", jupiter_body),
         ('String why = reason(); assertEquals(e(), a(), why);', "", jupiter_body),
         ('String why = reason(); assertEquals(why, e(), a());', "", body_of)],
        ids=["format_last", "format_first", "message_format", "formatted", "string_field",
             "string_local", "string_local_first"],
    )  # fmt: skip
    def test_a_typed_message_is_not_a_tolerance(self, tmp_path, after, helper, side):
        before = side("assertEquals(e(), a());", helper)
        assert codes(before, side(after, helper), tmp_path) == set()

    def test_a_double_local_is_still_a_tolerance(self, tmp_path):
        before = jupiter_body("assertEquals(e(), a());")
        after = jupiter_body("double why = delta(); assertEquals(e(), a(), why);")
        assert TOLERANCE in codes(before, after, tmp_path)

    def test_expected_exception_moved_into_assert_throws_is_kept(self, tmp_path):
        rule = "  @org.junit.Rule public ExpectedException thrown = ExpectedException.none();"
        imports = "import org.junit.rules.ExpectedException;\n"
        before = with_imports(
            body_of("thrown.expect(java.io.IOException.class); f();", rule), imports
        )
        after = with_imports(
            body_of("assertThrows(java.io.IOException.class, () -> f());", rule), imports
        )
        assert codes(before, after, tmp_path) == set()


JSON_IMPORTS = "import org.skyscreamer.jsonassert.JSONAssert;\n"
IS_IMPORTS = AWAIT_IMPORTS + "import static org.hamcrest.Matchers.is;\n"


class TestMoreKindsTwoAndThree:
    @pytest.mark.parametrize(
        ("before", "after", "imports"),
        [('assertEquals("[a, b]", names().toString());',
          'assertThat(names()).as("names").containsExactlyInAnyOrder("b", "a");', ""),
         ('assertThat(names().toString()).isEqualTo("[a, b]");',
          'assertThat(names()).containsExactlyInAnyOrder("b", "a");', ""),
         ("assertEquals(e(), a());", "JSONAssert.assertEquals(e(), a(), true);", JSON_IMPORTS),
         ("assertEquals(e(), a());",
          "JSONAssert.assertEquals(a(), e(), JSONCompareMode.NON_EXTENSIBLE);",
          JSON_IMPORTS + "import org.skyscreamer.jsonassert.JSONCompareMode;\n")],
        ids=["description_before_order_free", "assertj_to_string", "json_strict",
             "json_non_extensible_swapped"],
    )  # fmt: skip
    def test_a_kind_two_form_is_kept(self, tmp_path, before, after, imports):
        old, new = (with_imports(body_of(s), imports) for s in (before, after))
        assert codes(old, new, tmp_path) == set()

    def test_string_equality_to_lenient_json_is_still_refused(self, tmp_path):
        before = with_imports(body_of("assertEquals(e(), a());"), JSON_IMPORTS)
        after = with_imports(body_of("JSONAssert.assertEquals(e(), a(), false);"), JSON_IMPORTS)
        assert codes(before, after, tmp_path) & {REMOVED, RELAXED}

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertFalse(q().isEmpty());", "await().until(() -> !q().isEmpty());"),
         ("assertTrue(q().isReady());", "await().until(() -> { return q().isReady(); });"),
         ("assertEquals(3, q.size());", "await().until(q::size, equalTo(3));"),
         ("assertEquals(3, q.size());", "await().until(() -> q.size(), is(3));")],
        ids=["negated", "block_lambda", "method_reference_supplier", "is_matcher"],
    )  # fmt: skip
    def test_a_kind_three_form_is_kept(self, tmp_path, before, after):
        helper = "  Queue q;"
        old, new = (with_imports(body_of(s, helper), IS_IMPORTS) for s in (before, after))
        assert codes(old, new, tmp_path) == set()


class TestKnownStaticMembers:
    def add_import(self, line: str) -> str:
        before = body_of("assertEquals(1, f());")
        return before.replace("import org.junit.Test;\n", f"import org.junit.Test;\n{line}\n")

    @pytest.mark.parametrize(
        "line",
        ["import static org.awaitility.Awaitility.*;", "import static org.mockito.Mockito.*;"],
        ids=["awaitility", "mockito"],
    )
    def test_a_listed_class_without_the_name_rebinds_nothing(self, tmp_path, line):
        before = body_of("assertEquals(1, f());")
        assert codes(before, self.add_import(line), tmp_path) == set()

    def test_a_listed_class_declaring_the_name_rebinds_it(self, tmp_path):
        before = body_of("assertEquals(1, f());")
        after = self.add_import("import static org.junit.jupiter.api.Assertions.*;")
        assert REBOUND in codes(before, after, tmp_path)

    def test_the_table_lists_the_pinned_members(self):
        from rootflake.gate.checker.assertions import LIBRARY_MEMBERS

        assert "assertEquals" in LIBRARY_MEMBERS["org.junit.Assert"]
        assert "await" in LIBRARY_MEMBERS["org.awaitility.Awaitility"]
        assert "assertEquals" not in LIBRARY_MEMBERS["org.awaitility.Awaitility"]


def test_a_listed_class_name_declared_in_a_read_file_is_not_trusted(tmp_path):
    before = body_of("assertEquals(1, f());")
    after = before.replace(
        "import org.junit.Test;\n",
        "import org.junit.Test;\nimport static org.awaitility.Awaitility.*;\n",
    )
    shadow = {
        "src/test/java/org/awaitility/Awaitility.java":
            "package org.awaitility; public class Awaitility "
            "{ public static void assertEquals(int a, int b) {} }"
    }  # fmt: skip
    assert REBOUND in codes(before, after, tmp_path, shadow)


class TestMessageSlotByOwner:
    def test_a_junit4_expected_value_change_beside_a_string_actual_is_kept(self, tmp_path):
        before = body_of('String s = fmt(); assertEquals("format", "01/01/2020", s);')
        after = body_of('String s = fmt(); assertEquals("format", "1/1/2020", s);')
        assert codes(before, after, tmp_path) == set()

    def test_a_junit4_actual_replaced_by_the_expected_string_is_refused(self, tmp_path):
        before = body_of('String e = x(); String s = y(); assertEquals("m", e, s);')
        after = body_of('String e = x(); String s = y(); assertEquals("m", e, e);')
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_string_name_is_no_message_where_the_owner_is_unknown(self, tmp_path):
        source = body_of("assertEquals(e(), a());").replace(
            "import static org.junit.Assert.*;", "import static x.Checks.*;"
        )
        after = source.replace(
            "assertEquals(e(), a());", "String why = r(); assertEquals(e(), a(), why);"
        )
        assert codes(source, after, tmp_path) & {TOLERANCE, REMOVED}


class TestLiteralSubjects:
    @pytest.mark.parametrize(
        ("before", "after", "helper"),
        [('assertEquals("x", f());', 'String s = "x"; assertEquals("x", s);', ""),
         ("assertThat(f()).isNotEmpty();", 'assertThat("x").isNotEmpty();', ""),
         ("assertNotEquals(e(), a());", "assertNotEquals(1, 2);", ""),
         ("assertNull(a());", "assertNull(null);", ""),
         ("assertTrue(f().isEmpty());", 'assertTrue("".isEmpty());', ""),
         ('assertThat(f()).contains("b");', 'assertThat("abc").contains("b");', ""),
         ("assertThat(f()).hasSize(2);", "assertThat(Arrays.asList(1, 2)).hasSize(2);", ""),
         ('assertFalse(f().equals("b"));', 'assertFalse("a".equals("b"));', ""),
         ("assertThat(f()).isNotEqualTo(2);", "assertThat(1).isNotEqualTo(2);", ""),
         ('assertEquals("x", f());', 'assertEquals("x", X);', '  static final String X = "x";'),
         ("assertEquals(List.of(1), f());",
          "List<Integer> l = List.of(1); assertEquals(List.of(1), l);", ""),
         ("assertTrue(Objects.equals(a(), b()));",
          "assertTrue(java.util.Objects.equals(b(), b()));", "")],
        ids=["literal_local", "assertj_literal_subject", "not_equals_literals", "null_null",
             "call_on_literal", "assertj_contains_literal", "literal_factory", "equals_literals",
             "assertj_not_equal_literals", "literal_constant", "factory_local",
             "qualified_objects_equals"],
    )  # fmt: skip
    def test_compared_values_that_are_all_literals_are_refused(
        self, tmp_path, before, after, helper
    ):
        assert REMOVED in codes(body_of(before, helper), body_of(after, helper), tmp_path)

    def test_hamcrest_reason_and_literal_boolean_is_refused(self, tmp_path):
        before, after = (
            with_imports(
                body_of(s).replace(
                    "import static org.assertj.core.api.Assertions.assertThat;\n", ""
                ),
                HAMCREST_IMPORTS,
            )
            for s in ('assertThat("c", ok());', 'assertThat("c", true);')
        )
        assert REMOVED in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        ("before", "after"),
        [('assertEquals("x", f());', 'assertEquals("y", f());'),
         ("assertEquals(List.of(1), f());", "assertEquals(List.of(1, 2), f());"),
         ("assertTrue(f().isEmpty());", "assertTrue(g().isEmpty());")],
        ids=["expected_literal_changed", "expected_factory_changed", "subject_still_a_call"],
    )  # fmt: skip
    def test_a_literal_expectation_beside_a_real_value_is_kept(self, tmp_path, before, after):
        assert REMOVED not in codes(body_of(before), body_of(after), tmp_path)


def test_a_project_class_named_like_junit_does_not_set_the_message_slot(tmp_path):
    shadow = {"src/test/java/org/junit/Assert.java": "package org.junit; public class Assert {}"}
    before = body_of("org.junit.Assert.assertEquals(e(), a());")
    after = body_of("String why = r(); org.junit.Assert.assertEquals(why, e(), a());")
    assert codes(before, after, tmp_path, shadow) & {TOLERANCE, REMOVED}


class TestRuleActivation:
    COLLECTOR = (
        "import org.junit.rules.ErrorCollector;\nimport static org.hamcrest.Matchers.is;\n",
        "ErrorCollector collector = new ErrorCollector();",
        "collector.checkThat(f(), is(1));",
    )
    THROWN = (
        "import org.junit.rules.ExpectedException;\n",
        "ExpectedException thrown = ExpectedException.none();",
        "thrown.expect(java.io.IOException.class); f();",
    )

    @pytest.mark.parametrize(
        "form", [COLLECTOR, THROWN], ids=["error_collector", "expected_exception"]
    )
    def test_removing_the_rule_annotation_removes_its_assertions(self, tmp_path, form):
        imports, field, statement = form
        before = with_imports(body_of(statement, f"  @org.junit.Rule public {field}"), imports)
        after = with_imports(body_of(statement, f"  public {field}"), imports)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_local_shadowing_the_rule_field_removes_its_assertions(self, tmp_path):
        imports, field, statement = self.COLLECTOR
        before = with_imports(body_of(statement, f"  @org.junit.Rule public {field}"), imports)
        after = with_imports(
            body_of(f"{field} {statement}", f"  @org.junit.Rule public {field}"), imports
        )
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_truth_expect_without_its_rule_is_not_a_shortcut(self, tmp_path):
        imports = "import com.google.common.truth.Expect;\n"
        statement = "expect.that(f()).isEqualTo(1);"
        before = with_imports(
            body_of(statement, "  @org.junit.Rule public final Expect expect = Expect.create();"),
            imports,
        )
        after = with_imports(
            body_of(statement, "  public final Expect expect = Expect.create();"), imports
        )
        assert codes(before, after, tmp_path) == set()


class TestValuesBehindUnchangedText:
    @pytest.mark.parametrize(
        ("before", "after"),
        [("double eps = 0.01; assertEquals(1.0, d(), eps);",
          "double eps = 0.5; assertEquals(1.0, d(), eps);"),
         ("double eps = 0.01; assertThat(d()).isCloseTo(1.0, within(eps));",
          "double eps = 0.5; assertThat(d()).isCloseTo(1.0, within(eps));"),
         ("long max = 100; assertTrue(elapsed() < max);",
          "long max = 1000; assertTrue(elapsed() < max);"),
         ("long lo = 5; assertTrue(n() >= lo);", "long lo = 1; assertTrue(n() >= lo);")],
        ids=["delta", "within", "upper_bound", "lower_bound"],
    )  # fmt: skip
    def test_a_widened_value_behind_the_same_text(self, tmp_path, before, after):
        assert TOLERANCE in codes(body_of(before), body_of(after), tmp_path)

    @pytest.mark.parametrize(
        ("before", "after"),
        [("boolean strict = true; JSONAssert.assertEquals(e(), a(), strict);",
          "boolean strict = false; JSONAssert.assertEquals(e(), a(), strict);"),
         ("JSONAssert.assertEquals(e(), a(), true);",
          "boolean strict = false; JSONAssert.assertEquals(e(), a(), strict);"),
         ("JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT);",
          "JSONAssert.assertEquals(e(), a(), LENIENT);"),
         ("JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT);",
          "JSONAssert.assertEquals(e(), a(), new CustomComparator(JSONCompareMode.STRICT, c()));"),
         ("JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT);",
          "JSONCompareMode mode = JSONCompareMode.LENIENT; "
          "JSONAssert.assertEquals(e(), a(), mode);")],
        ids=["strict_local", "literal_to_local", "static_imported_lenient", "custom_comparator",
             "mode_local"],
    )  # fmt: skip
    def test_a_lenient_json_mode_in_any_form_is_refused(self, tmp_path, before, after):
        old, new = (with_imports(body_of(s), JSON_IMPORTS) for s in (before, after))
        assert codes(old, new, tmp_path) & {REMOVED, RELAXED}

    @pytest.mark.parametrize(
        ("before", "after"),
        [("assertEquals(e(), a(), true);", "assertEquals(e(), a(), false);"),
         ("assertEquals(e(), a(), JSONCompareMode.STRICT);",
          "assertEquals(e(), a(), JSONCompareMode.LENIENT);"),
         ("assertEquals(e(), a(), NON_EXTENSIBLE);", "assertEquals(e(), a(), mode());")],
        ids=["boolean", "mode_constant", "mode_to_call"],
    )  # fmt: skip
    def test_a_lenient_mode_through_ambiguous_wildcards_is_refused(self, tmp_path, before, after):
        imports = (
            "import static org.skyscreamer.jsonassert.JSONAssert.*;\n"
            "import org.skyscreamer.jsonassert.JSONCompareMode;\n"
        )
        old, new = (with_imports(body_of(s), imports) for s in (before, after))
        assert codes(old, new, tmp_path) & {REMOVED, RELAXED}

    def test_an_exact_assertion_made_lenient_through_ambiguous_wildcards_is_refused(self, tmp_path):
        imports = "import static org.skyscreamer.jsonassert.JSONAssert.*;\n"
        before = with_imports(body_of("assertEquals(e(), a());"), imports)
        after = with_imports(body_of("assertEquals(e(), a(), false);"), imports)
        assert codes(before, after, tmp_path) & {REMOVED, RELAXED}

    def test_an_unchanged_mode_through_ambiguous_wildcards_passes(self, tmp_path):
        imports = "import static org.skyscreamer.jsonassert.JSONAssert.*;\n"
        before = with_imports(body_of("assertEquals(e(), a(), true);"), imports)
        after = with_imports(body_of("assertEquals(e(), a(), true); f();"), imports)
        assert codes(before, after, tmp_path) == set()

    def test_an_unchanged_local_keeps_the_twin(self, tmp_path):
        source = body_of("double eps = 0.01; g(); assertEquals(1.0, d(), eps);")
        changed = source.replace("g();", "h();")
        assert codes(source, changed, tmp_path) == set()


def _codes_creating(before: str, after: str, created: dict[str, str], root: Path) -> set[str]:
    pristine, workspace = root / "p", root / "w"
    for side, source in ((pristine, before), (workspace, after)):
        (side / PATH).parent.mkdir(parents=True, exist_ok=True)
        (side / PATH).write_text(source)
    for rel, text in created.items():
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text(text)
    result = check(sorted([PATH, *created]), pristine, workspace, TARGET)
    return {f.code for f in result.findings}


class TestDeclaredLibraryNames:
    """A fully qualified test library name that a read file declares is that file's class."""

    def test_a_rule_field_of_a_created_library_type_is_not_credited(self, tmp_path):
        fake = {
            "src/test/java/org/junit/rules/ErrorCollector.java": (
                "package org.junit.rules;\npublic class ErrorCollector {\n"
                "  public <T> void checkThat(T value, Object matcher) {}\n}\n"
            )
        }
        field = (
            "  @org.junit.Rule public org.junit.rules.ErrorCollector c ="
            " new org.junit.rules.ErrorCollector();"
        )
        before = body_of("c.checkThat(r(), org.hamcrest.Matchers.is(3));", field)
        after = body_of("c.checkThat(q(), org.hamcrest.Matchers.is(3));", field)
        assert _codes_creating(before, after, fake, tmp_path) == {REBOUND}

    def test_a_then_on_a_created_library_class_is_not_credited(self, tmp_path):
        fake = {
            "src/test/java/org/assertj/core/api/BDDAssertions.java": (
                "package org.assertj.core.api;\npublic class BDDAssertions {\n"
                "  public static Object then(Object actual) { return actual; }\n}\n"
            )
        }
        before = body_of("org.assertj.core.api.BDDAssertions.then(r()).isEqualTo(3);")
        after = body_of("org.assertj.core.api.BDDAssertions.then(q()).isEqualTo(3);")
        assert REMOVED in _codes_creating(before, after, fake, tmp_path)


class TestRuleAnnotationResolution:
    """Only org.junit.Rule and org.junit.ClassRule, as resolved, make a rule field."""

    RULE_IMPORTS = "import org.junit.rules.ExpectedException;\nimport java.io.IOException;\n"

    def _sides(self, annotation: str, imports: str):
        field = f"  {annotation} public ExpectedException thrown = ExpectedException.none();"
        before = body_of("assertThrows(IOException.class, () -> f());", field)
        after = body_of("thrown.expect(IOException.class); f();", field)
        return (with_imports(s, self.RULE_IMPORTS + imports) for s in (before, after))

    def test_the_junit_rule_annotation_keeps_the_expectation(self, tmp_path):
        before, after = self._sides("@Rule", "import org.junit.Rule;\n")
        assert codes(before, after, tmp_path) == set()

    def test_another_library_annotation_named_rule_is_not_a_rule(self, tmp_path):
        before, after = self._sides("@Rule", "import org.mockito.junit.Rule;\n")
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_read_file_declaring_org_junit_rule_is_not_the_rule(self, tmp_path):
        fake = {"src/test/java/org/junit/Rule.java": "package org.junit;\n@interface Rule {}\n"}
        before, after = self._sides("@org.junit.Rule", "")
        assert REMOVED in codes(before, after, tmp_path, fake)


class TestRuleFieldShadowing:
    """A name hides the rule field only where a declaration of it is in scope."""

    FIELD = (
        "  @org.junit.Rule public org.junit.rules.ErrorCollector c ="
        " new org.junit.rules.ErrorCollector();"
    )
    CALL = "c.checkThat(q(), org.hamcrest.Matchers.is(3));"

    def test_an_untyped_lambda_parameter_is_not_the_rule(self, tmp_path):
        before = body_of(self.CALL, self.FIELD)
        after = body_of(f"java.util.List.of(other()).forEach(c -> {self.CALL});", self.FIELD)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_helper_parameter_is_not_the_rule(self, tmp_path):
        helper = f"{self.FIELD}\n  void check(Checker c) {{ {self.CALL} }}"
        before = body_of(self.CALL, helper)
        after = body_of("check(other());", helper)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_declaration_after_the_call_does_not_hide_it(self, tmp_path):
        later = "if (x()) { Object c = null; g(c); }"
        before = body_of(f"{self.CALL} {later}", self.FIELD)
        after = body_of(later, self.FIELD)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_declaration_in_another_block_does_not_hide_it(self, tmp_path):
        other = "if (x()) { Object c = null; g(c); }"
        before = body_of(f"{other} {self.CALL}", self.FIELD)
        after = body_of(other, self.FIELD)
        assert REMOVED in codes(before, after, tmp_path)

    @pytest.mark.parametrize(
        "shadow",
        ["Checker c = make(); CALL",
         "int n = 0, c = n; CALL",
         "for (Checker c : all()) { CALL }",
         "for (Checker c = make(); c != null; c = null) { CALL }",
         "try (Checker c = make()) { CALL }",
         "try { f(); } catch (CheckerError c) { CALL }"],
        ids=["local", "primitive_second_declarator", "foreach", "for_init", "resource", "catch"],
    )  # fmt: skip
    def test_a_declaration_in_scope_hides_the_rule(self, tmp_path, shadow):
        before = body_of(self.CALL, self.FIELD)
        after = body_of(shadow.replace("CALL", self.CALL), self.FIELD)
        assert REMOVED in codes(before, after, tmp_path)

    def test_the_rule_call_itself_is_kept(self, tmp_path):
        before = body_of(self.CALL, self.FIELD)
        after = body_of(f"f(); {self.CALL}", self.FIELD)
        assert codes(before, after, tmp_path) == set()


class TestBoundNamesAfterAComparison:
    """A name read after `>` is a value, not a declaration, and a local declared out of scope
    of the assertion does not stand for the field it reads."""

    @pytest.mark.parametrize(
        ("before", "after", "helper_before", "helper_after"),
        [("long t = run(); assertTrue(t > MIN_MS);", "long t = run(); assertTrue(t > MIN_MS);",
          "  static final long MIN_MS = 100;", "  static final long MIN_MS = 0;"),
         ("int lim = 5; assertTrue(x() > lim);", "int lim = 1; assertTrue(x() > lim);", "", ""),
         ("{ double EPS = 1; g(EPS); } assertEquals(1.0, f(), EPS);",
          "{ double EPS = 1; g(EPS); } assertEquals(1.0, f(), EPS);",
          "  static final double EPS = 0.001;", "  static final double EPS = 100;"),
         ("assertEquals(1.0, f(), EPS); double EPS = 1; g(EPS);",
          "assertEquals(1.0, f(), EPS); double EPS = 1; g(EPS);",
          "  static final double EPS = 0.001;", "  static final double EPS = 100;"),
         ("run(() -> { double EPS = 1; g(EPS); }); assertEquals(1.0, f(), EPS);",
          "run(() -> { double EPS = 1; g(EPS); }); assertEquals(1.0, f(), EPS);",
          "  static final double EPS = 0.001;", "  static final double EPS = 100;"),
         ("if (k()) { int LIMIT = 2; g(LIMIT); } assertTrue(x() > LIMIT);",
          "if (k()) { int LIMIT = 2; g(LIMIT); } assertTrue(x() > LIMIT);",
          "  static final int LIMIT = 10;", "  static final int LIMIT = 0;")],
        ids=["field_after_gt", "local_after_gt", "sibling_block_local", "later_local",
             "lambda_local", "sibling_local_after_gt"],
    )  # fmt: skip
    def test_a_widened_value_is_caught(self, tmp_path, before, after, helper_before, helper_after):
        old, new = body_of(before, helper_before), body_of(after, helper_after)
        assert codes(old, new, tmp_path) == {TOLERANCE}

    def test_a_changed_value_source_for_the_subject_still_passes(self, tmp_path):
        before = body_of("int v = f(); assertEquals(1, v);")
        after = body_of("int v = g(); assertEquals(1, v);")
        assert codes(before, after, tmp_path) == set()

    def test_a_declared_generic_local_has_no_value(self, tmp_path):
        before = body_of("List<Integer> lim = 5; assertTrue(x() > lim);")
        after = body_of("List<Integer> lim = 5; assertTrue(x() > lim); f();")
        assert codes(before, after, tmp_path) == set()


class TestRuleUnderAnOnDemandImport:
    """`import org.junit.*;` supplies Rule on the old side, so its checks are seen removed; the
    new side still needs the annotation resolved."""

    IMPORTS = "import org.junit.*;\nimport org.junit.rules.ErrorCollector;\n"
    FIELD = "  @Rule public ErrorCollector c = new ErrorCollector();"
    CALL = "c.checkThat(f(), org.hamcrest.Matchers.is(1));"

    def test_a_removed_check_is_seen(self, tmp_path):
        before = with_imports(body_of(f"{self.CALL} g();", self.FIELD), self.IMPORTS)
        after = with_imports(body_of("g();", self.FIELD), self.IMPORTS)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_new_check_earns_no_credit(self, tmp_path):
        before = with_imports(body_of("assertEquals(1, f());", self.FIELD), self.IMPORTS)
        after = with_imports(body_of(self.CALL, self.FIELD), self.IMPORTS)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_shadowing_rule_type_is_not_the_rule(self, tmp_path):
        helper = f"{self.FIELD}\n  @interface Rule {{}}"
        before = with_imports(body_of(f"{self.CALL} g();", helper), self.IMPORTS)
        after = with_imports(body_of("g();", helper), self.IMPORTS)
        assert codes(before, after, tmp_path) == set()


class TestNewJsonComparators:
    """A comparator, or any mode that is not a plain one, new to the after side can skip any
    field, so it is refused whatever mode the old assertion had."""

    IMPORTS = (
        "import org.skyscreamer.jsonassert.JSONAssert;\n"
        "import org.skyscreamer.jsonassert.JSONCompareMode;\n"
        "import org.skyscreamer.jsonassert.Customization;\n"
        "import org.skyscreamer.jsonassert.comparator.CustomComparator;\n"
    )
    ANY_ID = (
        'new CustomComparator(JSONCompareMode.LENIENT, new Customization("id", (a, b) -> true))'
    )

    @pytest.mark.parametrize(
        "old_mode", ["JSONCompareMode.LENIENT", "false", "JSONCompareMode.STRICT_ORDER"],
        ids=["lenient", "false", "strict_order"],
    )  # fmt: skip
    @pytest.mark.parametrize("new_mode", ["ANY_ID", "mode()"], ids=["comparator", "unknown"])
    def test_a_new_comparator_is_refused(self, tmp_path, old_mode, new_mode):
        new_mode = self.ANY_ID if new_mode == "ANY_ID" else new_mode
        before = with_imports(body_of(f"JSONAssert.assertEquals(e(), a(), {old_mode});"),
                              self.IMPORTS)  # fmt: skip
        after = with_imports(body_of(f"JSONAssert.assertEquals(e(), a(), {new_mode});"),
                             self.IMPORTS)  # fmt: skip
        assert REMOVED in codes(before, after, tmp_path)

    def test_an_unchanged_comparator_passes(self, tmp_path):
        call = f"JSONAssert.assertEquals(e(), a(), {self.ANY_ID});"
        before = with_imports(body_of(call), self.IMPORTS)
        after = with_imports(body_of(f"{call} f();"), self.IMPORTS)
        assert codes(before, after, tmp_path) == set()

    def test_strict_order_to_lenient_is_an_order_only_relaxation(self, tmp_path):
        call = "JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT_ORDER);"
        before = with_imports(body_of(call), self.IMPORTS)
        after = with_imports(
            body_of("JSONAssert.assertEquals(e(), a(), JSONCompareMode.LENIENT);"), self.IMPORTS
        )
        assert codes(before, after, tmp_path) == set()


class TestTrustedJsonModeNames:
    """A mode name is a JSONCompareMode constant only where it resolves to one, and a call that
    may be JSONAssert's is judged whatever its last argument looks like."""

    IMPORTS = (
        "import org.skyscreamer.jsonassert.JSONAssert;\n"
        "import org.skyscreamer.jsonassert.JSONCompareMode;\n"
    )

    def _codes(self, tmp_path, before, after, imports=IMPORTS, helper=""):
        old, new = (with_imports(body_of(s, helper), imports) for s in (before, after))
        return codes(old, new, tmp_path)

    def test_a_field_named_like_a_mode_is_not_the_mode(self, tmp_path):
        helper = (
            "  static org.skyscreamer.jsonassert.comparator.JSONComparator LENIENT;\n"
            "  static { LENIENT = a1(); if (k()) LENIENT = a2(); }"
        )
        imports = self.IMPORTS + "import static org.skyscreamer.jsonassert.JSONCompareMode.*;\n"
        before = "JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT_ORDER);"
        after = "JSONAssert.assertEquals(e(), a(), LENIENT);"
        assert REMOVED in self._codes(tmp_path, before, after, imports, helper)

    def test_a_bare_mode_name_from_another_class_is_not_the_mode(self, tmp_path):
        imports = self.IMPORTS + "import static com.example.Modes.*;\n"
        before = "JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT_ORDER);"
        after = "JSONAssert.assertEquals(e(), a(), LENIENT);"
        assert REMOVED in self._codes(tmp_path, before, after, imports)

    def test_a_statically_imported_mode_is_the_mode(self, tmp_path):
        imports = (
            self.IMPORTS + "import static org.skyscreamer.jsonassert.JSONCompareMode.LENIENT;\n"
        )
        before = "JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT_ORDER);"
        after = "JSONAssert.assertEquals(e(), a(), LENIENT);"
        assert self._codes(tmp_path, before, after, imports) == set()

    def test_a_possible_json_assert_with_an_unknown_mode_is_refused(self, tmp_path):
        imports = self.IMPORTS + "import static org.skyscreamer.jsonassert.JSONAssert.*;\n"
        before = "assertEquals(e(), a(), JSONCompareMode.LENIENT);"
        after = "Object cmp = k() ? x() : y(); assertEquals(e(), a(), cmp);"
        assert REMOVED in self._codes(tmp_path, before, after, imports)

    def test_an_unknown_mode_whose_value_changed_is_refused(self, tmp_path):
        helper = "  static Object cmp;"
        before = "cmp = x(); cmp = y(); JSONAssert.assertEquals(e(), a(), cmp);"
        after = "cmp = x(); cmp = z(); JSONAssert.assertEquals(e(), a(), cmp);"
        assert REMOVED in self._codes(tmp_path, before, after, helper=helper)


class TestModeNamesTheCallCannotVouchFor:
    IMPORTS = (
        "import org.skyscreamer.jsonassert.JSONAssert;\n"
        "import org.skyscreamer.jsonassert.JSONCompareMode;\n"
        "import static org.skyscreamer.jsonassert.JSONCompareMode.LENIENT;\n"
    )
    BEFORE = "JSONAssert.assertEquals(e(), a(), JSONCompareMode.STRICT_ORDER);"

    def _codes(self, tmp_path, after, source=None):
        source = source or body_of
        old, new = (with_imports(source(s), self.IMPORTS) for s in (self.BEFORE, after))
        return codes(old, new, tmp_path)

    def test_a_bare_name_under_an_unread_supertype_is_unknown(self, tmp_path):
        def extending(s):
            return body_of(s).replace(
                "public class FooTest {", "public class FooTest extends Base {"
            )

        after = "JSONAssert.assertEquals(e(), a(), LENIENT);"
        assert REMOVED in self._codes(tmp_path, after, extending)

    @pytest.mark.parametrize(
        ("base", "expected"),
        [("public class Base {}", set()), ("public class Base extends ExternalBase {}", {REMOVED})],
    )
    def test_an_unread_supertype_of_a_read_supertype_counts(self, tmp_path, base, expected):
        old, new = (
            with_imports(body_of(s), self.IMPORTS).replace(
                "public class FooTest {", "public class FooTest extends Base {"
            )
            for s in (self.BEFORE, "JSONAssert.assertEquals(e(), a(), LENIENT);")
        )
        extra = {"src/test/java/a/Base.java": f"package a;\n{base}\n"}
        assert codes(old, new, tmp_path, extra) == expected

    def test_a_local_hiding_the_class_name_is_unknown(self, tmp_path):
        after = (
            "Object JSONCompareMode = k(); "
            "JSONAssert.assertEquals(e(), a(), JSONCompareMode.LENIENT);"
        )
        assert REMOVED in self._codes(tmp_path, after)

    def test_a_mode_read_through_a_local_is_unknown(self, tmp_path):
        after = "JSONCompareMode m = JSONCompareMode.LENIENT; JSONAssert.assertEquals(e(), a(), m);"
        assert REMOVED in self._codes(tmp_path, after)

    def test_an_unchanged_mode_local_passes(self, tmp_path):
        call = "JSONCompareMode m = JSONCompareMode.LENIENT; JSONAssert.assertEquals(e(), a(), m);"
        old, new = (with_imports(body_of(s), self.IMPORTS) for s in (call, f"{call} f();"))
        assert codes(old, new, tmp_path) == set()


class TestUnchangedRuleUnderAnOnDemandImport:
    """An unchanged rule field the old side counted under `import org.junit.*;` still counts on
    the new side, so its unchanged checks are not seen removed."""

    IMPORTS = (
        "import org.junit.*;\nimport org.junit.rules.ErrorCollector;\n"
        "import org.junit.rules.ExpectedException;\n"
    )
    FIELDS = (
        "  @Rule public ErrorCollector collector = new ErrorCollector();\n"
        "  @Rule public ExpectedException thrown = ExpectedException.none();\n"
        "  @Test public void other() {\n"
        "    collector.checkThat(a(), org.hamcrest.Matchers.equalTo(1)); }\n"
        "  @Test public void third() { thrown.expect(IllegalStateException.class); g(); }"
    )

    def test_a_benign_change_elsewhere_passes(self, tmp_path):
        before = with_imports(body_of("int v = f(); Assert.assertEquals(1, v);", self.FIELDS),
                              self.IMPORTS)  # fmt: skip
        after = with_imports(body_of("reset(); int v = f(); Assert.assertEquals(1, v);",
                                     self.FIELDS), self.IMPORTS)  # fmt: skip
        assert codes(before, after, tmp_path) == set()

    def test_a_changed_rule_field_is_not_trusted(self, tmp_path):
        changed = self.FIELDS.replace(
            "@Rule public ErrorCollector collector = new ErrorCollector();",
            "@Rule public ErrorCollector collector = make();",
        )
        before = with_imports(body_of("f();", self.FIELDS), self.IMPORTS)
        after = with_imports(body_of("f();", changed), self.IMPORTS)
        assert REMOVED in codes(before, after, tmp_path)

    def test_a_created_rule_type_is_still_rejected(self, tmp_path):
        before = with_imports(body_of("f();", self.FIELDS), self.IMPORTS)
        after = with_imports(body_of("f(); g();", self.FIELDS), self.IMPORTS)
        created = {"src/test/java/a/Rule.java": "package a;\npublic @interface Rule {}\n"}
        assert _codes_creating(before, after, created, tmp_path) & {REMOVED, REBOUND}


def _codes_files(files: dict[str, tuple[str, str]], root: Path) -> set[str]:
    pristine, workspace = root / "p", root / "w"
    for side, index in ((pristine, 0), (workspace, 1)):
        for rel, sources in files.items():
            (side / rel).parent.mkdir(parents=True, exist_ok=True)
            (side / rel).write_text(sources[index])
    result = check(sorted(files), pristine, workspace, TARGET)
    return {f.code for f in result.findings}


def _limits(value: str, extra: str = "") -> str:
    return (
        "package a;\npublic final class Limits {\n"
        f"  public static final int MAX = {value};\n{extra}}}\n"
    )


class TestConstantsBehindAnotherName:
    """A bound, tolerance or mode keeps its strength only while every name behind it, in
    another read file or behind another constant, keeps its assignments."""

    LIMITS = "src/test/java/a/Limits.java"
    MAIN_LIMITS = "src/main/java/a/Limits.java"

    @pytest.mark.parametrize(
        ("statements", "imports"),
        [("assertTrue(x() < Limits.MAX);", ""),
         ("assertTrue(x() < a.Limits.MAX);", ""),
         ("assertTrue(x() < MAX);", "import static a.Limits.MAX;\n"),
         ("assertEquals(1.0, d(), Limits.MAX);", "")],
        ids=["qualified", "fully_qualified", "static_import", "tolerance"],
    )  # fmt: skip
    def test_a_constant_widened_in_another_read_file(self, tmp_path, statements, imports):
        before = with_imports(body_of(f"f(); {statements}"), imports)
        after = with_imports(body_of(f"g(); {statements}"), imports)
        files = {PATH: (before, after), self.LIMITS: (_limits("5"), _limits("50"))}
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_constant_widened_behind_a_static_wildcard_import(self, tmp_path):
        """The assertion's own name is ambiguous under the wildcard, so the changed one earns
        no credit and the old one is left removed: a reject either way."""
        imports = "import static a.Limits.*;\n"
        before = with_imports(body_of("f(); assertTrue(x() < MAX);"), imports)
        after = with_imports(body_of("g(); assertTrue(x() < MAX);"), imports)
        files = {PATH: (before, after), self.LIMITS: (_limits("5"), _limits("50"))}
        assert _codes_files(files, tmp_path) == {REMOVED}
        files[self.LIMITS] = (_limits("5"), _limits("5"))
        assert _codes_files(files, tmp_path) == set()

    def test_a_production_constant_counts_like_a_test_constant(self, tmp_path):
        before = body_of("f(); assertTrue(x() < Limits.MAX);")
        after = body_of("g(); assertTrue(x() < Limits.MAX);")
        files = {PATH: (before, after), self.MAIN_LIMITS: (_limits("5"), _limits("50"))}
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_constant_behind_a_second_constant(self, tmp_path):
        fields = "  static final int OTHER = {};\n  static final int LIMIT = OTHER;"
        before = body_of("assertTrue(x() < LIMIT);", fields.format(5))
        after = body_of("assertTrue(x() < LIMIT);", fields.format(50))
        assert codes(before, after, tmp_path) == {TOLERANCE}

    def test_a_chain_that_crosses_files(self, tmp_path):
        fields = "  static final int LIMIT = Limits.MAX;"
        before = body_of("f(); assertTrue(x() < LIMIT);", fields)
        after = body_of("g(); assertTrue(x() < LIMIT);", fields)
        files = {PATH: (before, after), self.LIMITS: (_limits("5"), _limits("50"))}
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_local_behind_another_local(self, tmp_path):
        before = body_of("long base = 100; long max = base; assertTrue(elapsed() < max);")
        after = body_of("long base = 1000; long max = base; assertTrue(elapsed() < max);")
        assert codes(before, after, tmp_path) == {TOLERANCE}

    def test_a_widened_constant_through_a_qualified_mode_local(self, tmp_path):
        fields = (
            "  static final JSONCompareMode OTHER = JSONCompareMode.{};\n"
            "  static final JSONCompareMode MODE = OTHER;"
        )
        imports = JSON_IMPORTS + "import org.skyscreamer.jsonassert.JSONCompareMode;\n"
        old = with_imports(body_of("JSONAssert.assertEquals(e(), a(), MODE);",
                                   fields.format("STRICT")), imports)  # fmt: skip
        new = with_imports(body_of("JSONAssert.assertEquals(e(), a(), MODE);",
                                   fields.format("LENIENT")), imports)  # fmt: skip
        assert codes(old, new, tmp_path) & {REMOVED, RELAXED}

    def test_an_unchanged_constant_in_another_file_passes(self, tmp_path):
        before = body_of("f(); assertTrue(x() < Limits.MAX);")
        after = body_of("g(); assertTrue(x() < Limits.MAX);")
        limits = _limits("5", "  public static final int MIN = 1;\n")
        moved = _limits("5", "  public static final int MIN = 2;\n")
        files = {PATH: (before, after), self.LIMITS: (limits, moved)}
        assert _codes_files(files, tmp_path) == set()

    def test_an_unread_constant_class_keeps_the_twin(self, tmp_path):
        before = body_of("f(); assertTrue(x() < Limits.MAX);")
        after = body_of("g(); assertTrue(x() < Limits.MAX);")
        assert codes(before, after, tmp_path) == set()

    def test_a_long_chain_terminates(self, tmp_path):
        chain = "\n".join(f"  static final int C{i} = C{i + 1};" for i in range(300))
        fields = f"{chain}\n  static final int C300 = {{}};"
        before = body_of("assertTrue(x() < C0);", fields.format(5))
        after = body_of("assertTrue(x() < C0);", fields.format(50))
        assert codes(before, after, tmp_path) == {TOLERANCE}

    def test_a_cyclic_chain_terminates(self, tmp_path):
        fields = "  static int A = B;\n  static int B = A;"
        before = body_of("f(); assertTrue(x() < A);", fields)
        after = body_of("g(); assertTrue(x() < A);", fields)
        assert codes(before, after, tmp_path) == set()


class TestHelpersInABoundPosition:
    """A call whose body changed, sitting where a tolerance, bound or mode goes, no longer
    stands for the same value."""

    @pytest.mark.parametrize(
        ("statement", "helper_before", "helper_after"),
        [("assertTrue(x() < limit());", "  int limit() { return 5; }",
          "  int limit() { return 50; }"),
         ("assertEquals(1.0, d(), eps());", "  double eps() { return 0.01; }",
          "  double eps() { return 0.5; }"),
         ("assertThat(d()).isCloseTo(1.0, within(eps()));", "  double eps() { return 0.01; }",
          "  double eps() { return 0.5; }"),
         ("assertTrue(x() < limit());", "  int limit() { return LIMIT; }\n"
          "  static final int LIMIT = 5;", "  int limit() { return LIMIT; }\n"
          "  static final int LIMIT = 50;")],
        ids=["bound", "delta", "within", "constant_behind_helper"],
    )  # fmt: skip
    def test_a_changed_helper_widens_the_bound(self, tmp_path, statement, helper_before,
                                               helper_after):  # fmt: skip
        before = body_of(statement, helper_before)
        after = body_of(statement, helper_after)
        assert codes(before, after, tmp_path) == {TOLERANCE}

    def test_a_changed_comparator_helper_is_refused(self, tmp_path):
        imports = JSON_IMPORTS + "import org.skyscreamer.jsonassert.JSONCompareMode;\n"
        helper = "  Object cmp() {{ return {}; }}"
        before = with_imports(
            body_of(
                "JSONAssert.assertEquals(e(), a(), cmp());",
                helper.format("make(JSONCompareMode.STRICT)"),
            ),
            imports,
        )
        after = with_imports(
            body_of(
                "JSONAssert.assertEquals(e(), a(), cmp());",
                helper.format("make(JSONCompareMode.LENIENT)"),
            ),
            imports,
        )
        assert codes(before, after, tmp_path) & {REMOVED, RELAXED}

    def test_a_changed_subject_helper_still_passes(self, tmp_path):
        before = body_of("assertTrue(x() < 5);", "  int x() { return compute(); }")
        after = body_of("assertTrue(x() < 5);", "  int x() { return computeSorted(); }")
        assert codes(before, after, tmp_path) == set()

    def test_a_changed_expected_value_helper_still_passes(self, tmp_path):
        before = body_of("assertEquals(expected(), actual());", "  int actual() { return f(); }")
        after = body_of("assertEquals(expected(), actual());", "  int actual() { return g(); }")
        assert codes(before, after, tmp_path) == set()

    def test_an_unchanged_bound_helper_passes(self, tmp_path):
        helper = "  int limit() { return 5; }"
        before = body_of("f(); assertTrue(x() < limit());", helper)
        after = body_of("g(); assertTrue(x() < limit());", helper)
        assert codes(before, after, tmp_path) == set()

    def test_mutually_recursive_bound_helpers_terminate(self, tmp_path):
        helpers = "  int limit() { return other(); }\n  int other() { return limit(); }"
        before = body_of("f(); assertTrue(x() < limit());", helpers)
        after = body_of("g(); assertTrue(x() < limit());", helpers)
        assert codes(before, after, tmp_path) == set()


class TestOutOfScopeLocalsNamedLikeAField:
    def test_a_new_inner_block_local_leaves_the_field_assertion_alone(self, tmp_path):
        field = "  static final double EPS = 0.001;"
        before = body_of("assertEquals(1.0, f(), EPS);", field)
        added = "{ double EPS = 1; assertEquals(2.0, g(), EPS); }"
        after = body_of(f"assertEquals(1.0, f(), EPS); {added}", field)
        assert codes(before, after, tmp_path) == set()

    def test_the_field_widened_beside_a_new_local_is_still_caught(self, tmp_path):
        before = body_of("assertEquals(1.0, f(), EPS);", "  static final double EPS = 0.001;")
        after = body_of("assertEquals(1.0, f(), EPS); { double EPS = 1; g(EPS); }",
                        "  static final double EPS = 100;")  # fmt: skip
        assert codes(before, after, tmp_path) == {TOLERANCE}


def _class_file(package: str, head: str, body: str) -> str:
    return f"package {package};\n{head} {{\n{body}\n}}\n"


class TestBoundsBehindInheritedAndImportedNames:
    """Names in a bound reached through a read supertype, an on demand import, a call, a write
    from another file, or the left side of a comparison are followed like any other."""

    BASE = "src/test/java/a/BaseTest.java"
    LIMITS = "src/test/java/a/Limits.java"

    def _files(self, before, after, extra_before, extra_after, path=None):
        return {PATH: (before, after), (path or self.LIMITS): (extra_before, extra_after)}

    def test_an_inherited_constant(self, tmp_path):
        def test(s):
            return body_of(s).replace("public class FooTest {",
                                      "public class FooTest extends BaseTest {")  # fmt: skip

        base = "public abstract class BaseTest"
        field = "  protected static final double EPSILON = {};"
        files = self._files(test("f(); assertEquals(1.0, d(), EPSILON);"),
                            test("g(); assertEquals(1.0, d(), EPSILON);"),
                            _class_file("a", base, field.format("0.001")),
                            _class_file("a", base, field.format("0.5")), self.BASE)  # fmt: skip
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_qualified_constant_declared_in_a_read_supertype(self, tmp_path):
        base = _class_file("a", "public class Base", "  public static final int MAX = {};")
        files = {
            PATH: (body_of("f(); assertTrue(x() < Limits.MAX);"),
                   body_of("g(); assertTrue(x() < Limits.MAX);")),
            self.LIMITS: (_class_file("a", "public class Limits extends Base", ""),) * 2,
            self.BASE: (base.replace("{}", "5"), base.replace("{}", "50")),
        }  # fmt: skip
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_qualifier_under_an_on_demand_import(self, tmp_path):
        before = with_imports(body_of("f(); assertTrue(x() < Limits.MAX);"), "import b.*;\n")
        after = with_imports(body_of("g(); assertTrue(x() < Limits.MAX);"), "import b.*;\n")
        limits = (
            "package b;\npublic final class Limits {{\n  public static final int MAX = {};\n}}\n"
        )
        files = self._files(before, after, limits.format(5), limits.format(50),
                            "src/test/java/b/Limits.java")  # fmt: skip
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    @pytest.mark.parametrize(
        ("statement", "helpers"),
        [("assertTrue(x() < MAX);", "  static final int MAX = compute();\n"
          "  static int compute() {{ return {}; }}"),
         ("long max = limit(); assertTrue(x() < max);", "  static int limit() {{ return {}; }}")],
        ids=["constant", "local"],
    )  # fmt: skip
    def test_a_call_behind_a_bound_name(self, tmp_path, statement, helpers):
        before = body_of(statement, helpers.format(5))
        after = body_of(statement, helpers.format(50))
        assert codes(before, after, tmp_path) == {TOLERANCE}

    def test_a_qualified_write_from_the_test(self, tmp_path):
        limits = _class_file("a", "public final class Limits", "  public static int MAX = 5;")
        files = self._files(body_of("f(); assertTrue(x() < Limits.MAX);"),
                            body_of("Limits.MAX = 50; assertTrue(x() < Limits.MAX);"),
                            limits, limits)  # fmt: skip
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_constant_on_the_left_of_the_comparison(self, tmp_path):
        files = self._files(body_of("f(); assertTrue(Limits.MAX > x());"),
                            body_of("g(); assertTrue(Limits.MAX > x());"),
                            _limits("5"), _limits("50"))  # fmt: skip
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_statically_imported_helper(self, tmp_path):
        imports = "import static a.Limits.limit;\n"
        limits = _class_file("a", "public final class Limits",
                             "  public static int limit() { return VALUE; }")  # fmt: skip
        files = self._files(with_imports(body_of("f(); assertTrue(x() < limit());"), imports),
                            with_imports(body_of("g(); assertTrue(x() < limit());"), imports),
                            *(limits.replace("VALUE", v) for v in ("5", "50")))  # fmt: skip
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_a_helper_on_an_unknown_receiver(self, tmp_path):
        limits = _class_file("a", "public final class Limits",
                             "  public int limit() { return VALUE; }")  # fmt: skip
        files = self._files(body_of("f(); assertTrue(x() < l().limit());"),
                            body_of("g(); assertTrue(x() < l().limit());"),
                            *(limits.replace("VALUE", v) for v in ("5", "50")))  # fmt: skip
        assert _codes_files(files, tmp_path) == {TOLERANCE}

    def test_unchanged_inherited_and_imported_names_pass(self, tmp_path):
        imports = "import static a.Limits.limit;\n"
        limits = _class_file("a", "public final class Limits",
                             "  public static int limit() { return 5; }\n"
                             "  public static final int MAX = 5;")  # fmt: skip
        moved = limits.replace("{\n  public", "{\n  static int unused() { return 1; }\n  public", 1)
        stmt = "assertTrue(x() < limit()); assertTrue(Limits.MAX > x());"
        files = self._files(with_imports(body_of(f"f(); {stmt}"), imports),
                            with_imports(body_of(f"g(); {stmt}"), imports),
                            limits, moved)  # fmt: skip
        assert _codes_files(files, tmp_path) == set()

    def test_a_changed_subject_local_beside_a_helper_bound_passes(self, tmp_path):
        helper = "  int limit() { return 5; }"
        before = body_of("int v = f(); assertTrue(v < limit());", helper)
        after = body_of("int v = g(); assertTrue(v < limit());", helper)
        assert codes(before, after, tmp_path) == set()

    def test_an_unrelated_member_of_the_qualifying_type_passes(self, tmp_path):
        files = self._files(body_of("f(); assertTrue(x() < Limits.MAX);"),
                            body_of("g(); assertTrue(x() < Limits.MAX);"),
                            _limits("5", "  Limits() { int other = OTHER; }\n"
                                         "  static int OTHER = 1;\n"),
                            _limits("5", "  Limits() { int other = OTHER; }\n"
                                         "  static int OTHER = 2;\n"))  # fmt: skip
        assert _codes_files(files, tmp_path) == set()


class TestTheTargetFileIsAlwaysRead:
    """The target class's file is read even when the patch leaves it alone, so a patch that
    changes only a constant its unchanged assertions read is still judged."""

    LIMITS = "src/main/java/a/Limits.java"

    def _codes(self, root, limits, target=TARGET, test_path=PATH):
        pristine, workspace = root / "p", root / "w"
        test = body_of("assertTrue(x() < Limits.MAX);")
        for side, value in ((pristine, limits[0]), (workspace, limits[1])):
            for rel, text in ((test_path, test), (self.LIMITS, value)):
                (side / rel).parent.mkdir(parents=True, exist_ok=True)
                (side / rel).write_text(text)
        return {f.code for f in check([self.LIMITS], pristine, workspace, target).findings}

    def test_a_constant_widened_with_the_test_untouched(self, tmp_path):
        assert self._codes(tmp_path, (_limits("5"), _limits("50"))) == {TOLERANCE}

    def test_a_benign_change_with_the_test_untouched_passes(self, tmp_path):
        moved = (_limits("5"), _limits("5", "  public static final int MIN = 1;\n"))
        assert self._codes(tmp_path, moved) == set()

    def test_a_named_module_is_used(self, tmp_path):
        target = Target(TARGET.class_name, TARGET.method, "core")
        limits = (_limits("5"), _limits("50"))
        assert self._codes(tmp_path, limits, target, f"core/{PATH}") == {TOLERANCE}

    @pytest.mark.parametrize(
        ("paths", "target", "expected"),
        [(["m/src/main/java/a/L.java"], Target("a.FooTest", "t"),
          ["m/src/test/java/a/FooTest.java"]),
         (["src/main/java/a/L.java"], Target("a.FooTest$Inner", "t"),
          ["src/test/java/a/FooTest.java"]),
         (["m/src/test/java/a/FooTest.java"], Target("a.FooTest", "t"), []),
         (["m/src/main/java/a/L.java"], Target("a.FooTest", "t", "n"),
          ["n/src/test/java/a/FooTest.java"]),
         (["m/src/main/java/a/L.java"], Target("a/../FooTest", "t"), []),
         (["m/src/main/java/a/L.java"], None, [])],
        ids=["module_of_path", "nested", "already_read", "named_module", "unsafe", "no_target"],
    )  # fmt: skip
    def test_target_paths(self, paths, target, expected):
        assert target_paths(paths, target) == expected
