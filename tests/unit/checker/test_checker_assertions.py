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
            "Long[] ns = map.keySet().toArray(new Long[0]); assertEquals(1, parse(ns[0]));"
        )
        after = body_of("assertEquals(0, other.get(4L)); assertEquals(1, parse(4));")
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
