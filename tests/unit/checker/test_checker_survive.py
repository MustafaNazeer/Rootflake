from pathlib import Path

import pytest

from rootflake.gate.checker.check import Target, check

PATH = "src/test/java/a/FooTest.java"
TARGET = Target("a.FooTest", "testFoo")


def codes(before: str | None, after: str | None, tmp_path: Path, target=TARGET) -> set[str]:
    pristine, workspace = tmp_path / "p", tmp_path / "w"
    for root, source in ((pristine, before), (workspace, after)):
        root.mkdir()
        if source is not None:
            (root / PATH).parent.mkdir(parents=True)
            (root / PATH).write_text(source)
    return {f.code for f in check([PATH], pristine, workspace, target).findings}


def cls(body: str, header: str = "", imports: str = "import org.junit.Test;") -> str:
    return f"package a;\n{imports}\n{header}\npublic class FooTest {{\n{body}\n}}\n"


TEST = "@Test public void testFoo() { assertEquals(1, f()); }"
OTHER = "@Test public void testBar() { assertEquals(2, g()); }"


class TestRemoval:
    def test_an_unchanged_class_has_no_findings(self, tmp_path):
        assert codes(cls(TEST), cls(TEST), tmp_path) == set()

    def test_a_deleted_test_method(self, tmp_path):
        assert codes(cls(TEST + OTHER), cls(TEST), tmp_path) == {"disable/test_removed"}

    def test_a_renamed_test_method(self, tmp_path):
        assert codes(cls(TEST), cls(TEST.replace("testFoo", "testFooSorted")), tmp_path) == {
            "disable/test_removed"
        }

    def test_a_removed_test_annotation(self, tmp_path):
        assert codes(cls(TEST), cls(TEST.replace("@Test ", "")), tmp_path) == {
            "disable/test_removed"
        }

    def test_a_commented_out_test_annotation(self, tmp_path):
        assert codes(cls(TEST), cls(TEST.replace("@Test ", "// @Test\n")), tmp_path) == {
            "disable/test_removed"
        }

    def test_a_changed_test_annotation_on_the_target(self, tmp_path):
        after = cls(TEST.replace("@Test", "@RepeatedTest(3)"))
        assert codes(cls(TEST), after, tmp_path) == {"disable/test_removed"}

    def test_a_test_annotation_replaced_by_a_retry_annotation_is_not_a_removal(self, tmp_path):
        after = cls(TEST.replace("@Test", "@RetryingTest(3)"))
        assert "disable/test_removed" not in codes(cls(TEST), after, tmp_path)

    def test_a_junit3_style_test_renamed(self, tmp_path):
        before = cls("public void testFoo() { assertEquals(1, f()); }", imports="").replace(
            "class FooTest", "class FooTest extends junit.framework.TestCase"
        )
        after = before.replace("testFoo", "checkFoo")
        assert codes(before, after, tmp_path) == {"disable/test_removed"}

    def test_a_target_class_renamed_inside_its_file(self, tmp_path):
        before = cls(TEST)
        after = before.replace("class FooTest", "class FooIT")
        assert codes(before, after, tmp_path) == {"disable/test_removed"}

    def test_an_added_test_is_fine(self, tmp_path):
        assert codes(cls(TEST), cls(TEST + OTHER), tmp_path) == set()


class TestBody:
    def test_an_emptied_test_body(self, tmp_path):
        after = cls("@Test public void testFoo() { }")
        assert "disable/test_body_emptied" in codes(cls(TEST), after, tmp_path)

    def test_an_added_early_return(self, tmp_path):
        after = cls("@Test public void testFoo() { if (ci()) { return; } assertEquals(1, f()); }")
        assert "disable/early_return" in codes(cls(TEST), after, tmp_path)

    def test_a_return_inside_a_lambda_is_not_an_early_return(self, tmp_path):
        after = cls("@Test public void testFoo() { run(() -> { return; }); assertEquals(1, f()); }")
        assert "disable/early_return" not in codes(cls(TEST), after, tmp_path)


class TestDisabling:
    @pytest.mark.parametrize(
        "annotation",
        ["@Ignore", '@Ignore("flaky")', "@org.junit.Ignore", "@Disabled", "@DisabledOnOs(OS.MAC)",
         '@DisabledIfSystemProperty(named = "ci", matches = "true")', "@EnabledOnJre(JRE.JAVA_8)",
         "@AwaitsFix(bugUrl = \"x\")", "@LuceneTestCase.AwaitsFix(bugUrl = \"x\")", "@Nightly",
         "@BadApple(bugUrl = \"x\")"],
    )  # fmt: skip
    def test_an_added_disabling_annotation_on_a_method(self, tmp_path, annotation):
        after = cls(f"{annotation} " + TEST)
        assert codes(cls(TEST), after, tmp_path) == {"disable/test_disabled"}

    def test_an_added_disabling_annotation_on_a_class(self, tmp_path):
        assert codes(cls(TEST), cls(TEST, header="@Disabled"), tmp_path) == {
            "disable/test_disabled"
        }

    def test_testng_enabled_false(self, tmp_path):
        after = cls(TEST.replace("@Test", "@Test(enabled = false)"))
        assert codes(cls(TEST), after, tmp_path) == {"disable/test_disabled"}

    def test_an_existing_disabling_annotation_is_not_a_finding(self, tmp_path):
        before = cls("@Ignore " + TEST + OTHER)
        after = cls("@Ignore " + TEST + OTHER.replace("2", "3"))
        assert "disable/test_disabled" not in codes(before, after, tmp_path)


class TestOrderParallelismTags:
    @pytest.mark.parametrize(
        ("header", "code"),
        [
            ("@FixMethodOrder(MethodSorters.NAME_ASCENDING)", "disable/order_pinned"),
            ("@TestMethodOrder(MethodOrderer.OrderAnnotation.class)", "disable/order_pinned"),
            ("@TestClassOrder(ClassOrderer.OrderAnnotation.class)", "disable/order_pinned"),
            ("@Order(1)", "disable/order_pinned"),
            ("@Execution(ExecutionMode.SAME_THREAD)", "disable/parallelism_disabled"),
            ("@Isolated", "disable/parallelism_disabled"),
            ('@ResourceLock("db")', "disable/parallelism_disabled"),
            ('@Tag("flaky")', "disable/tag_changed"),
            ("@Category(Slow.class)", "disable/tag_changed"),
        ],
    )
    def test_an_added_class_annotation(self, tmp_path, header, code):
        assert codes(cls(TEST), cls(TEST, header=header), tmp_path) == {code}

    def test_concurrent_execution_is_not_disabling_parallelism(self, tmp_path):
        after = cls(TEST, header="@Execution(ExecutionMode.CONCURRENT)")
        assert codes(cls(TEST), after, tmp_path) == set()

    def test_a_removed_tag(self, tmp_path):
        assert codes(cls(TEST, header='@Tag("slow")'), cls(TEST), tmp_path) == {
            "disable/tag_changed"
        }

    @pytest.mark.parametrize(
        ("attribute", "code"),
        [
            ("priority = 2", "disable/order_pinned"),
            ('dependsOnMethods = "testBar"', "disable/order_pinned"),
            ('groups = "flaky"', "disable/tag_changed"),
            ("singleThreaded = true", "disable/parallelism_disabled"),
        ],
    )
    def test_an_added_testng_attribute(self, tmp_path, attribute, code):
        after = cls(TEST.replace("@Test", f"@Test({attribute})"))
        assert codes(cls(TEST), after, tmp_path) == {code}


class TestAssumptions:
    @pytest.mark.parametrize(
        "call",
        ["assumeTrue(ci());", "Assume.assumeFalse(ci());", "Assumptions.assumeTrue(ok);",
         "assumeThat(v, is(1));", "assumingThat(ok, () -> {});", "Assumptions.abort(\"slow\");",
         'throw new SkipException("flaky");', 'throw new org.testng.SkipException("x");',
         'throw new TestAbortedException("x");', 'throw new AssumptionViolatedException("x");'],
    )  # fmt: skip
    def test_an_added_assumption(self, tmp_path, call):
        after = cls(f"@Test public void testFoo() {{ {call} assertEquals(1, f()); }}")
        assert codes(cls(TEST), after, tmp_path) == {"disable/assumption_added"}

    def test_a_changed_assumption(self, tmp_path):
        before = cls("@Test public void testFoo() { assumeTrue(v(4, 3)); assertEquals(1, f()); }")
        assert codes(before, before.replace("v(4, 3)", "v(4, 9)"), tmp_path) == {
            "disable/assumption_added"
        }

    def test_an_assumption_in_a_helper_reached_from_a_test(self, tmp_path):
        after = cls(
            "@Test public void testFoo() { guard(); assertEquals(1, f()); } "
            'private void guard() { if (slow()) { Assumptions.abort("x"); } }'
        )
        assert codes(cls(TEST), after, tmp_path) == {"disable/assumption_added"}

    def test_an_assumption_inside_a_catch_is_a_swallowed_failure(self, tmp_path):
        after = cls(
            "@Test public void testFoo() { try { assertEquals(1, f()); } "
            "catch (Exception e) { assumeTrue(false); } }"
        )
        result = codes(cls(TEST), after, tmp_path)
        assert "swallow_failure/assumption_in_catch" in result
        assert "disable/assumption_added" not in result

    def test_an_unchanged_assumption_is_not_a_finding(self, tmp_path):
        before = cls(
            "@Test public void testFoo() { assumeTrue(ok()); assertEquals(1, f()); }",
            imports="import org.junit.Test;\nimport static org.junit.Assert.*;",
        )
        assert codes(before, before.replace("1, f()", "2, f()"), tmp_path) == set()


class TestClassShape:
    def test_a_changed_extends_on_a_test_class(self, tmp_path):
        before = cls(TEST).replace("class FooTest", "class FooTest extends Base")
        after = cls(TEST).replace("class FooTest", "class FooTest extends OtherBase")
        assert codes(before, after, tmp_path) == {"weaken_assertion/assertion_rebound"}

    def test_an_added_implements_on_a_test_class(self, tmp_path):
        after = cls(TEST).replace("class FooTest", "class FooTest implements Shared")
        assert codes(cls(TEST), after, tmp_path) == {"weaken_assertion/assertion_rebound"}

    def test_an_added_execution_condition(self, tmp_path):
        after = cls(
            TEST + " static class Skip implements ExecutionCondition { "
            "public ConditionEvaluationResult evaluateExecutionCondition(ExtensionContext c) "
            "{ return null; } }"
        )
        assert "disable/execution_condition" in codes(cls(TEST), after, tmp_path)


class TestParameterSources:
    def source(self, rows: str) -> str:
        return cls(
            '@ParameterizedTest @MethodSource("cases") public void testFoo(int n) { '
            "assertEquals(n, f(n)); } static Stream<Arguments> cases() { return Stream.of("
            + rows
            + "); }"
        )

    def test_fewer_method_source_cases(self, tmp_path):
        before = self.source("Arguments.of(1), Arguments.of(2), Arguments.of(3)")
        after = self.source("Arguments.of(1), Arguments.of(2)")
        assert codes(before, after, tmp_path) == {"disable/parameter_cases_dropped"}

    def test_more_cases_are_fine(self, tmp_path):
        before = self.source("Arguments.of(1), Arguments.of(2)")
        after = self.source("Arguments.of(1), Arguments.of(2), Arguments.of(3)")
        assert codes(before, after, tmp_path) == set()

    def test_a_changed_source_whose_count_is_unknown(self, tmp_path):
        before = self.source("product(Stream.of(1, 10, 100), booleans())").replace(
            "return Stream.of(product", "return (product"
        )
        after = self.source("Arguments.of(1), Arguments.of(10)")
        assert codes(before, after, tmp_path) == {"disable/parameter_cases_dropped"}

    @pytest.mark.parametrize(
        ("before", "after"),
        [("@ValueSource(ints = {1, 2, 3})", "@ValueSource(ints = {1, 2})"),
         ('@CsvSource({"a,1", "b,2"})', '@CsvSource({"a,1"})')],
    )  # fmt: skip
    def test_fewer_annotation_cases(self, tmp_path, before, after):
        template = cls(
            "@ParameterizedTest SRC public void testFoo(int n) { assertEquals(n, f(n)); }"
        )
        assert codes(template.replace("SRC", before), template.replace("SRC", after), tmp_path) == {
            "disable/parameter_cases_dropped"
        }


class TestComposedAndNew:
    def test_a_composed_retry_annotation_replacing_test_is_not_a_removal(self, tmp_path):
        after = cls(TEST.replace("@Test", "@Flaky")) + "@RetryingTest(3) @interface Flaky {}\n"
        assert "disable/test_removed" not in codes(cls(TEST), after, tmp_path)

    def test_a_composed_test_annotation_keeps_the_method_a_test(self, tmp_path):
        after = cls(TEST.replace("@Test", "@SlowTest")) + "@Test @interface SlowTest {}\n"
        assert "disable/test_removed" not in codes(cls(TEST), after, tmp_path)

    def test_a_composed_annotation_carrying_nothing_is_a_removal(self, tmp_path):
        after = cls(TEST.replace("@Test", "@Plain")) + "@interface Plain {}\n"
        assert codes(cls(TEST), after, tmp_path) == {"disable/test_removed"}

    def test_a_return_in_a_new_test_method_is_an_early_return(self, tmp_path):
        added = "@Test public void testNew() { if (done()) { return; } fail(); }"
        assert codes(cls(TEST), cls(TEST + added), tmp_path) == {"disable/early_return"}


class TestDomainRulings:
    """Mustafa's rulings of 2026-10-23 on the Domain verdict and the automated review."""

    def test_a_new_test_factory_may_return_its_tests(self, tmp_path):
        added = (
            "@TestFactory Stream<DynamicTest> zones() "
            '{ return Stream.of(dynamicTest("utc", () -> {})); }'
        )
        assert codes(cls(TEST), cls(TEST + added), tmp_path) == set()

    def test_a_parameter_type_change_keeps_the_test(self, tmp_path):
        before = cls("@ParameterizedTest public void testFoo(String z) { assertEquals(1, f(z)); }")
        after = before.replace("String z", "ZoneId z")
        assert "disable/test_removed" not in codes(before, after, tmp_path)

    def test_a_change_inside_an_assuming_that_body_is_not_a_new_assumption(self, tmp_path):
        before = cls("@Test public void testFoo() { assumingThat(isLinux(), () -> { f(map); }); }")
        after = before.replace("f(map)", "f(new TreeMap<>(map))")
        assert "disable/assumption_added" not in codes(before, after, tmp_path)

    def test_a_spring_order_annotation_is_not_pinned_test_order(self, tmp_path):
        imports = "import org.junit.Test;\nimport org.springframework.core.annotation.Order;"
        before = cls(TEST, imports=imports)
        after = cls(TEST + " @Order(1) @Bean Foo foo() { return new Foo(); }", imports=imports)
        assert "disable/order_pinned" not in codes(before, after, tmp_path)

    def test_an_unresolved_order_annotation_is_still_pinned_order(self, tmp_path):
        after = cls(
            "@Order(1) " + TEST, imports="import org.junit.Test;\nimport org.junit.jupiter.api.*;"
        )
        before = cls(TEST, imports="import org.junit.Test;\nimport org.junit.jupiter.api.*;")
        assert codes(before, after, tmp_path) == {"disable/order_pinned"}

    def test_a_qualified_execution_condition(self, tmp_path):
        added = (" static class S implements org.junit.jupiter.api.extension.ExecutionCondition "
                 "{ public ConditionEvaluationResult evaluateExecutionCondition(ExtensionContext c)"
                 " { return null; } }")  # fmt: skip
        assert "disable/execution_condition" in codes(cls(TEST), cls(TEST + added), tmp_path)

    def test_an_execution_condition_field(self, tmp_path):
        added = ' @RegisterExtension static ExecutionCondition off = c -> disabled("x");'
        assert "disable/execution_condition" in codes(cls(TEST), cls(TEST + added), tmp_path)

    def test_a_statically_imported_abort(self, tmp_path):
        imports = "import org.junit.Test;\nimport static org.junit.jupiter.api.Assumptions.abort;"
        after = cls('@Test public void testFoo() { abort("slow"); assertEquals(1, f()); }',
                    imports=imports)  # fmt: skip
        assert codes(cls(TEST, imports=imports), after, tmp_path) == {"disable/assumption_added"}

    def test_a_skip_exception_thrown_through_a_variable(self, tmp_path):
        after = cls('@Test public void testFoo() { RuntimeException e = new SkipException("x"); '
                    "throw e; }")  # fmt: skip
        assert "disable/assumption_added" in codes(cls(TEST), after, tmp_path)

    @pytest.mark.parametrize(
        "call",
        [
            "assumeThatCode(() -> f()).doesNotThrowAnyException();",
            "assumeThatObject(o).isNotNull();",
        ],
    )
    def test_assertj_assumptions(self, tmp_path, call):
        after = cls(f"@Test public void testFoo() {{ {call} assertEquals(1, f()); }}")
        assert codes(cls(TEST), after, tmp_path) == {"disable/assumption_added"}

    def test_enabled_boolean_false(self, tmp_path):
        after = cls(TEST.replace("@Test", "@Test(enabled = Boolean.FALSE)"))
        assert codes(cls(TEST), after, tmp_path) == {"disable/test_disabled"}

    def test_a_local_test_annotation_type_does_not_keep_a_test(self, tmp_path):
        before = cls(TEST)
        after = cls(TEST, imports="") + "@interface Test {}\n"
        assert "disable/test_removed" in codes(before, after, tmp_path)

    def test_a_provider_that_filters_its_cases_has_an_unknown_count(self, tmp_path):
        template = cls(
            '@ParameterizedTest @MethodSource("cases") public void testFoo(int n) { f(n); } '
            "static Stream<Arguments> cases() { return BODY; }"
        )
        before = template.replace("BODY", "Stream.of(Arguments.of(1), Arguments.of(2))")
        after = template.replace(
            "BODY", "Stream.of(Arguments.of(1), Arguments.of(2)).filter(a -> ok(a))"
        )
        assert codes(before, after, tmp_path) == {"disable/parameter_cases_dropped"}

    def test_cases_built_in_a_lambda_have_an_unknown_count(self, tmp_path):
        template = cls(
            '@ParameterizedTest @MethodSource("cases") public void testFoo(int n) { f(n); } '
            "static Stream<Arguments> cases() "
            "{ return IntStream.range(0, N).mapToObj(i -> Arguments.of(i)); }"
        )
        assert codes(template.replace("N", "50"), template.replace("N", "1"), tmp_path) == {
            "disable/parameter_cases_dropped"
        }

    def test_a_csv_value_attribute_beside_null_values(self, tmp_path):
        template = cls('@ParameterizedTest @CsvSource(nullValues = {"N/A"}, value = ROWS) '
                       "public void testFoo(String s) { f(s); }")  # fmt: skip
        before = template.replace("ROWS", '{"a,1", "b,2"}')
        after = template.replace("ROWS", '{"a,1"}')
        assert codes(before, after, tmp_path) == {"disable/parameter_cases_dropped"}

    def test_csv_text_block_rows(self, tmp_path):
        template = cls('@ParameterizedTest @CsvSource(textBlock = """\n ROWS\n """) '
                       "public void testFoo(String s) { f(s); }")  # fmt: skip
        before = template.replace("ROWS", "a, 1\n b, 2\n c, 3")
        after = template.replace("ROWS", "a, 1")
        assert codes(before, after, tmp_path) == {"disable/parameter_cases_dropped"}

    def test_a_trailing_comma_does_not_count_a_case(self, tmp_path):
        template = cls(
            "@ParameterizedTest @ValueSource(ints = ROWS) public void testFoo(int n) { f(n); }"
        )
        before = template.replace("ROWS", "{1, 2}")
        after = template.replace("ROWS", "{1, 2,}")
        assert codes(before, after, tmp_path) == set()


class TestNarrowedExtendsRule:
    def test_a_helper_class_gaining_an_interface_is_flagged_not_rejected(self, tmp_path):
        worker = (
            " static class Worker implements Runnable { public void run() { checkNotNull(x); } }"
        )
        after = worker.replace("implements Runnable", "implements Runnable, AutoCloseable")
        assert codes(cls(TEST + worker), cls(TEST + after), tmp_path) == {
            "flag/unknown_test_extension"
        }

    def test_a_helper_class_with_read_test_free_supertypes_gets_nothing(self, tmp_path):
        before = cls(TEST + " static class A {} static class B {} static class W extends A {}")
        after = before.replace("class W extends A", "class W extends B")
        assert codes(before, after, tmp_path) == set()

    def test_a_class_changing_an_unread_base_class_is_rejected(self, tmp_path):
        # Mustafa 2026-10-23: an extends clause naming an unread class may carry inherited
        # tests, so changing it rejects, as ADR 0007 Decision 6 says.
        before = cls(TEST + " static class L extends AbstractListener {}")
        after = cls(TEST + " static class L extends AbstractQuietListener {}")
        assert codes(before, after, tmp_path) == {"weaken_assertion/assertion_rebound"}

    def test_a_test_class_changing_its_supertype_is_still_rejected(self, tmp_path):
        before = cls(TEST).replace("class FooTest", "class FooTest extends Base")
        after = cls(TEST).replace("class FooTest", "class FooTest extends OtherBase")
        assert codes(before, after, tmp_path) == {"weaken_assertion/assertion_rebound"}


def test_a_default_method_source_is_not_the_test_itself(tmp_path):
    template = cls(
        "@ParameterizedTest @MethodSource public void testFoo(int n) { BODY } "
        "static Stream<Arguments> testFoo() { return Stream.of(Arguments.of(1), Arguments.of(2)); }"
    )
    before = template.replace("BODY", "assertEquals(n, f(n));")
    after = template.replace("BODY", "assertEquals(n, f(n, UTC));")
    assert "disable/parameter_cases_dropped" not in codes(before, after, tmp_path)


class TestSecondReview:
    def test_a_changed_boolean_supplier_in_an_assumption_is_a_new_assumption(self, tmp_path):
        before = cls("@Test public void testFoo() { assumeTrue(() -> isLinux()); f(); }")
        after = before.replace("isLinux()", "true")
        assert "disable/assumption_added" in codes(before, after, tmp_path)

    def test_a_comma_inside_a_string_is_not_a_case_separator(self, tmp_path):
        template = cls(
            "@ParameterizedTest @CsvSource(ROWS) public void testFoo(String s) { f(s); }"
        )
        before = template.replace("ROWS", '{"a , 1", "b , 2"}')
        after = template.replace("ROWS", '{"a , 1 , b , 2"}')
        assert codes(before, after, tmp_path) == {"disable/parameter_cases_dropped"}

    def test_a_value_return_in_a_new_test_is_an_early_return(self, tmp_path):
        added = "@Test public int testNew() { if (done()) { return 1; } fail(); return 0; }"
        assert "disable/early_return" in codes(cls(TEST), cls(TEST + added), tmp_path)

    def test_a_test_whose_return_type_leaves_void_is_removed(self, tmp_path):
        after = cls("@Test public int testFoo() { assertEquals(1, f()); return 0; }")
        assert "disable/test_removed" in codes(cls(TEST), after, tmp_path)


def test_a_test_turned_into_a_factory_is_removed(tmp_path):
    after = cls("@TestFactory public Stream<DynamicTest> testFoo() { return Stream.empty(); }")
    assert "disable/test_removed" in codes(cls(TEST), after, tmp_path)


def test_a_void_return_in_a_new_method_counts_whatever_its_annotations(tmp_path):
    added = "@Test @TestFactory public void testNew() { if (done()) { return; } fail(); }"
    assert "disable/early_return" in codes(cls(TEST), cls(TEST + added), tmp_path)
