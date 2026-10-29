import time

from test_checker_timing import IMPORTS, cls, codes, judged, method

from rootflake.gate.checker.swallow import CATCH, EXPECTED, HANDLER, RETRY_LOOP

JUNIT4 = """import org.junit.*;
import org.junit.rules.*;
import org.junit.runners.model.Statement;
import static org.junit.Assert.*;
"""
JUPITER = IMPORTS + "import org.junit.jupiter.api.extension.*;\n"


def rejected(found: set[str]) -> bool:
    return any(not c.startswith("flag/") for c in found)


class TestCatchesThatStopRethrowing:
    RETHROW = "try { process(); } catch (IOException e) { throw new UncheckedIOException(e); }"

    def test_a_rethrow_turned_swallow_that_ends_the_test(self, tmp_path):
        after = self.RETHROW.replace("throw new UncheckedIOException(e);", "")
        assert CATCH in judged(self.RETHROW, after, tmp_path)

    def test_a_rethrow_turned_swallow_around_a_value_read_later(self, tmp_path):
        before = (
            "int n = 0; try { n = count(); } catch (IOException e) { throw new"
            " UncheckedIOException(e); } assertEquals(0, n);"
        )
        after = before.replace("throw new UncheckedIOException(e);", "")
        assert CATCH in judged(before, after, tmp_path)

    def test_a_fail_turned_print_around_an_assertion(self, tmp_path):
        before = (
            "try { process(); assertEquals(1, count()); } catch (IOException e) {"
            " fail(e.toString()); }"
        )
        after = before.replace("fail(e.toString());", "e.printStackTrace();")
        assert CATCH in judged(before, after, tmp_path)

    def test_a_swallowing_catch_left_as_it_was_is_not(self, tmp_path):
        before = "try { connect(); } catch (Exception e) { } assertTrue(done());"
        after = before.replace("assertTrue(done());", "log(); assertTrue(done());")
        assert CATCH not in judged(before, after, tmp_path)


class TestFailResolved:
    def test_a_fail_the_class_declares_swallows(self, tmp_path):
        body = "try { assertTrue(done()); } catch (AssertionError e) { fail(); }"
        before = cls(method("assertTrue(done());"))
        after = cls("static void fail() { }\n" + method(body))
        assert CATCH in codes(before, after, tmp_path)

    def test_a_fail_imported_from_a_project_class_swallows(self, tmp_path):
        imports = "import org.junit.jupiter.api.*;\nimport static a.Util.fail;\n"
        body = "try { process(); org.junit.jupiter.api.Assertions.assertTrue(done()); }"
        before = cls(method("process(); org.junit.jupiter.api.Assertions.assertTrue(done());"),
                     imports)  # fmt: skip
        after = cls(method(body + ' catch (AssertionError e) { fail("x"); }'), imports)
        assert CATCH in codes(before, after, tmp_path)

    def test_the_library_fail_still_rethrows(self, tmp_path):
        after = 'try { process(); assertTrue(done()); } catch (Exception e) { fail("x"); }'
        assert CATCH not in judged("process(); assertTrue(done());", after, tmp_path)


class TestAssumptionSubclasses:
    BEFORE = "process(); assertTrue(done());"

    def test_a_read_subclass_of_an_assumption_aborts_the_test(self, tmp_path):
        skip = "static class Skip extends org.opentest4j.TestAbortedException { }\n"
        body = (
            "try { process(); assertTrue(done()); } catch (AssertionError e) { throw new Skip(); }"
        )
        found = codes(cls(skip + method(self.BEFORE)), cls(skip + method(body)), tmp_path)
        assert CATCH in found

    def test_a_subclass_two_levels_down(self, tmp_path):
        types = (
            "static class Skip extends org.junit.AssumptionViolatedException {"
            ' Skip() { super("s"); } }\nstatic class Later extends Skip { }\n'
        )
        body = (
            "try { process(); assertTrue(done()); } catch (AssertionError e) { throw new Later(); }"
        )
        found = codes(cls(types + method(self.BEFORE)), cls(types + method(body)), tmp_path)
        assert CATCH in found

    def test_a_project_runtime_exception_is_a_rethrow(self, tmp_path):
        own = "static class Broke extends RuntimeException { }\n"
        body = "try { process(); assertTrue(done()); } catch (Exception e) { throw new Broke(); }"
        found = codes(cls(own + method(self.BEFORE)), cls(own + method(body)), tmp_path)
        assert CATCH not in found


class TestRetryLoopsWithoutACheck:
    def test_a_loop_retrying_a_pristine_call(self, tmp_path):
        after = "for (int i = 0; i < 3; i++) { try { process(); break; } catch (Exception e) { } }"
        assert {CATCH, RETRY_LOOP} <= judged("process();", after, tmp_path)

    def test_a_loop_retrying_a_pristine_call_with_an_unrelated_assertion_after(self, tmp_path):
        before = "connect(); assertTrue(isOpen());"
        after = (
            "for (int i = 0; i < 3; i++) { try { connect(); break; } catch (Exception e) { } }"
            " assertTrue(isOpen());"
        )
        assert {CATCH, RETRY_LOOP} <= judged(before, after, tmp_path)

    def test_a_loop_over_new_cleanup_calls_is_not(self, tmp_path):
        before = "assertTrue(done());"
        after = (
            "for (Object o : leftovers()) { try { release(o); } catch (Exception e) { } }"
            " assertTrue(done());"
        )
        assert CATCH not in judged(before, after, tmp_path)


class TestHandlersOfOtherShapes:
    def test_a_handler_lambda(self, tmp_path):
        field = "@RegisterExtension static TestExecutionExceptionHandler quiet = (c, t) -> { };\n"
        before = cls(method("compute();"), JUPITER)
        after = cls(field + method("compute();"), JUPITER)
        assert HANDLER in codes(before, after, tmp_path)

    def test_a_handler_anonymous_class(self, tmp_path):
        field = (
            "@RegisterExtension static Extension quiet = new TestExecutionExceptionHandler() {"
            " public void handleTestExecutionException(ExtensionContext c, Throwable t) { } };\n"
        )
        before = cls(method("compute();"), JUPITER)
        after = cls(field + method("compute();"), JUPITER)
        assert HANDLER in codes(before, after, tmp_path)


class TestRulesThatRunTheTest:
    def test_a_rule_lambda_that_swallows_the_test(self, tmp_path):
        rule = (
            "static int count;\n@Rule public TestRule quiet = (base, d) -> new Statement() {"
            " public void evaluate() { try { base.evaluate(); } catch (Throwable t) { }"
            " count++; } };\n"
        )
        before = cls("static int count;\n" + method("assertTrue(done());"), JUNIT4)
        after = cls(rule + method("assertTrue(done());"), JUNIT4)
        assert CATCH in codes(before, after, tmp_path)

    def test_a_rule_that_rethrows_is_not(self, tmp_path):
        rule = (
            "@Rule public TestRule loud = (base, d) -> new Statement() {"
            " public void evaluate() throws Throwable { try { base.evaluate(); }"
            " catch (Throwable t) { log(t); throw t; } } };\n"
        )
        before = cls(method("assertTrue(done());"), JUNIT4)
        after = cls(rule + method("assertTrue(done());"), JUNIT4)
        assert CATCH not in codes(before, after, tmp_path)

    def test_a_retrying_rule_is_left_to_the_retry_rule(self, tmp_path):
        rule = (
            "@Rule public TestRule again = (base, d) -> new Statement() {"
            " public void evaluate() throws Throwable { Throwable last = null;"
            " for (int i = 0; i < 3; i++) { try { base.evaluate(); return; }"
            " catch (Throwable t) { last = t; } } throw last; } };\n"
        )
        before = cls(method("assertTrue(done());"), JUNIT4)
        after = cls(rule + method("assertTrue(done());"), JUNIT4)
        found = codes(before, after, tmp_path)
        assert CATCH not in found
        assert any(c.startswith("retry/") for c in found)


class TestHelperIdioms:
    EXPECT = (
        'static void expectIae(Runnable r) { try { r.run(); fail("no IAE"); }'
        " catch (IllegalArgumentException e) { } }\n"
    )
    AWAIT = (
        "static boolean awaitQuietly(CountDownLatch l) { try { return l.await(5,"
        " TimeUnit.SECONDS); } catch (InterruptedException e) {"
        " Thread.currentThread().interrupt(); return false; } }\n"
    )

    def test_a_new_call_to_a_pristine_try_and_fail_helper(self, tmp_path):
        before = cls(self.EXPECT + method("assertTrue(done());"))
        after = cls(self.EXPECT + method('assertTrue(done()); expectIae(() -> parse("x"));'))
        assert CATCH not in codes(before, after, tmp_path)

    def test_a_new_try_and_fail_helper_and_a_call(self, tmp_path):
        before = cls(method("assertTrue(done());"))
        after = cls(self.EXPECT + method('assertTrue(done()); expectIae(() -> parse("x"));'))
        assert CATCH not in codes(before, after, tmp_path)

    def test_a_helper_with_the_interrupt_idiom_whose_result_is_checked(self, tmp_path):
        before = cls(self.AWAIT + method("assertTrue(done());"))
        after = cls(self.AWAIT + method("assertTrue(awaitQuietly(latch())); assertTrue(done());"))
        assert CATCH not in codes(before, after, tmp_path)


class TestInterruptIdiomAroundAWait:
    def test_a_wait_result_checked_later(self, tmp_path):
        before = "CountDownLatch l = start(); Thread.sleep(1000); assertEquals(1, count());"
        after = (
            "CountDownLatch l = start(); boolean ok = false; try { ok = l.await(10,"
            " TimeUnit.SECONDS); } catch (InterruptedException e) {"
            " Thread.currentThread().interrupt(); } assertTrue(ok); assertEquals(1, count());"
        )
        assert CATCH not in judged(before, after, tmp_path)

    def test_a_wait_result_dropped(self, tmp_path):
        before = "CountDownLatch l = start(); l.await(); assertEquals(1, count());"
        after = (
            "CountDownLatch l = start(); try { l.await(10, TimeUnit.SECONDS); } catch"
            " (InterruptedException e) { Thread.currentThread().interrupt(); }"
            " assertEquals(1, count());"
        )
        assert CATCH in judged(before, after, tmp_path)


class TestPristineChecksInAnExistingTry:
    def test_an_assertion_rewritten_in_place_is_not(self, tmp_path):
        before = (
            'try { String s = json(); assertEquals("{}", s); } catch (IOException e) {'
            " e.printStackTrace(); }"
        )
        after = before.replace('assertEquals("{}", s);', 'assertEquals("{}", s.trim());')
        assert CATCH not in judged(before, after, tmp_path)

    def test_a_pristine_assertion_moved_in_beside_one_is(self, tmp_path):
        before = (
            "try { process(); assertTrue(ready()); } catch (Exception e) { }"
            " assertEquals(1, count());"
        )
        after = (
            "try { process(); assertTrue(ready()); assertEquals(1, count()); }"
            " catch (Exception e) { }"
        )
        assert CATCH in judged(before, after, tmp_path)


class TestUnboundedFutureWaits:
    def test_a_get_on_a_future_local(self, tmp_path):
        before = (
            "ExecutorService ex = pool(); Future<?> f = ex.submit(() -> assertEquals(1,"
            " count())); f.get(); ex.shutdown();"
        )
        after = before.replace("f.get();", "try { f.get(); } catch (ExecutionException e) { }")
        assert CATCH in judged(before, after, tmp_path)

    def test_a_get_chained_from_submit(self, tmp_path):
        before = "pool().submit(() -> assertEquals(1, count())).get(); stop();"
        after = (
            "try { pool().submit(() -> assertEquals(1, count())).get(); }"
            " catch (Exception e) { } stop();"
        )
        assert CATCH in judged(before, after, tmp_path)

    def test_a_get_on_a_map_is_not(self, tmp_path):
        before = "Map<String, Integer> m = load(); assertEquals(1, count());"
        after = (
            'Map<String, Integer> m = load(); try { m.get("a"); } catch (Exception e) { }'
            " assertEquals(1, count());"
        )
        assert CATCH not in judged(before, after, tmp_path)


class TestExpectCallsOnAnExistingRule:
    RULE = "@Rule public ExpectedException thrown = ExpectedException.none();\n"

    def test_a_new_expect_on_an_existing_rule(self, tmp_path):
        before = cls(self.RULE + method("compute();"), JUNIT4)
        after = cls(self.RULE + method("thrown.expect(Exception.class); compute();"), JUNIT4)
        assert EXPECTED in codes(before, after, tmp_path)

    def test_an_unchanged_expect_is_not(self, tmp_path):
        body = "thrown.expect(IllegalStateException.class); compute();"
        before = cls(self.RULE + method(body), JUNIT4)
        after = cls(self.RULE + method(body.replace("compute();", "run();")), JUNIT4)
        assert EXPECTED not in codes(before, after, tmp_path)

    def test_an_expect_added_beside_a_kept_assert_throws(self, tmp_path):
        imports = JUNIT4 + "import static org.junit.jupiter.api.Assertions.assertThrows;\n"
        body = "assertThrows(IllegalStateException.class, () -> close()); assertTrue(done());"
        before = cls(self.RULE + method(body), imports)
        after = cls(
            self.RULE + method("thrown.expect(Throwable.class); " + body + " throw new Error();"),
            imports,
        )
        assert EXPECTED in codes(before, after, tmp_path)

    def test_an_assert_throws_converted_to_an_expect_is_not(self, tmp_path):
        imports = JUNIT4 + "import static org.junit.jupiter.api.Assertions.assertThrows;\n"
        before = cls(
            self.RULE + method("assertThrows(IllegalStateException.class, () -> close());"),
            imports,
        )
        after = cls(
            self.RULE + method("thrown.expect(IllegalStateException.class); close();"), imports
        )
        assert EXPECTED not in codes(before, after, tmp_path)


class TestQualifiedFailRethrows:
    BEFORE = "service.process();"

    def judge(self, fail: str, tmp_path, imports: str = JUNIT4) -> set[str]:
        after = f"try {{ service.process(); }} catch (IOException e) {{ {fail}(e.getMessage()); }}"
        return codes(cls(method(self.BEFORE), imports), cls(method(after), imports), tmp_path)

    def test_assert_fail(self, tmp_path):
        assert CATCH not in self.judge("Assert.fail", tmp_path)

    def test_a_fully_qualified_fail(self, tmp_path):
        assert CATCH not in self.judge("org.junit.Assert.fail", tmp_path)

    def test_jupiter_assertions_fail(self, tmp_path):
        imports = "import org.junit.jupiter.api.Test;\nimport org.junit.jupiter.api.Assertions;\n"
        assert CATCH not in self.judge("Assertions.fail", tmp_path, imports)

    def test_a_fail_on_a_project_class_swallows(self, tmp_path):
        imports = JUNIT4 + "import a.util.Checks;\n"
        assert CATCH in self.judge("Checks.fail", tmp_path, imports)

    def judge_with(self, members: str, fail: str, tmp_path, prefix: str = "") -> set[str]:
        after = (
            f"{prefix}try {{ service.process(); }} catch (IOException e) {{"
            f" {fail}(e.getMessage()); }}"
        )
        before = cls(members + method(prefix + self.BEFORE), JUNIT4)
        return codes(before, cls(members + method(after), JUNIT4), tmp_path)

    def test_a_fail_on_this_the_class_declares_swallows(self, tmp_path):
        members = "void fail(String m) { }\n"
        assert CATCH in self.judge_with(members, "this.fail", tmp_path)

    def test_a_fail_on_a_field_of_an_unread_type_swallows(self, tmp_path):
        assert CATCH in self.judge_with("Reporter reporter;\n", "reporter.fail", tmp_path)

    def test_a_fail_on_a_capitalized_field_swallows(self, tmp_path):
        assert CATCH in self.judge_with("Reporter Reporter;\n", "Reporter.fail", tmp_path)

    def test_a_fail_on_a_constant_swallows(self, tmp_path):
        assert CATCH in self.judge_with("static Reporter REPORTER;\n", "REPORTER.fail", tmp_path)

    def test_a_fail_on_a_local_swallows(self, tmp_path):
        prefix = "Reporter r = reporter(); "
        assert CATCH in self.judge_with("", "r.fail", tmp_path, prefix)

    def test_a_fail_on_a_new_object_swallows(self, tmp_path):
        assert CATCH in self.judge_with("", "new Reporter().fail", tmp_path)

    def test_a_fail_on_a_wildcard_type_when_a_read_file_declares_fail_swallows(self, tmp_path):
        members = "static class Reporter { static void fail(String m) { } }\n"
        assert CATCH in self.judge_with(members, "Reporter.fail", tmp_path)


class TestElementAndContainerWrites:
    def test_an_array_element_read_later(self, tmp_path):
        before = "int[] r = new int[1]; r[0] = compute(); assertEquals(0, r[0]);"
        after = (
            "int[] r = new int[1]; try { r[0] = compute(); } catch (Exception e) { }"
            " assertEquals(0, r[0]);"
        )
        assert CATCH in judged(before, after, tmp_path)

    def test_a_list_added_to_and_read_later(self, tmp_path):
        before = (
            "List<Integer> xs = new ArrayList<>(); xs.add(compute()); assertEquals(1, xs.size());"
        )
        after = (
            "List<Integer> xs = new ArrayList<>(); try { xs.add(compute()); } catch (Exception e)"
            " { } assertEquals(1, xs.size());"
        )
        assert CATCH in judged(before, after, tmp_path)


class TestReceiverFilter:
    WIDE = "weaken_assertion/tolerance_widened"

    @staticmethod
    def types(tol: str, other: str, extends: str = "") -> str:
        return (
            f"static class Tol {{ double delta() {{ return {tol}; }} }}\n"
            f"static class Other{extends} {{ double delta() {{ return {other}; }} }}\n"
        )

    BODY = method("Tol t = new Tol(); assertEquals(1.0, value(), t.delta());")

    def test_the_declared_class_widened_is_caught(self, tmp_path):
        before = cls(self.types("0.001", "0.001") + self.BODY)
        after = cls(self.types("0.5", "0.001") + self.BODY)
        assert self.WIDE in codes(before, after, tmp_path)

    def test_an_unrelated_class_widened_is_not(self, tmp_path):
        before = cls(self.types("0.001", "0.001") + self.BODY)
        after = cls(self.types("0.001", "0.5") + self.BODY)
        assert self.WIDE not in codes(before, after, tmp_path)

    def test_a_subclass_of_the_declared_class_counts(self, tmp_path):
        before = cls(self.types("0.001", "0.001", " extends Tol") + self.BODY)
        after = cls(self.types("0.001", "0.5", " extends Tol") + self.BODY)
        assert self.WIDE in codes(before, after, tmp_path)

    def test_a_type_variable_receiver_is_not_filtered(self, tmp_path):
        helper = "static <T extends Tol> double d(T t) { return t.delta(); }\n"
        body = method("assertEquals(1.0, value(), d(new Tol()));")
        before = cls(self.types("0.001", "0.001") + helper + body)
        after = cls(self.types("0.5", "0.001") + helper + body)
        assert self.WIDE in codes(before, after, tmp_path)


class TestEditedLoopWithATrailingBreak:
    def test_a_rewritten_header_and_a_trailing_break(self, tmp_path):
        before = "for (int i = 0; i < CASES.length; i++) { process(CASES[i]); }"
        after = "for (int i = 0; i < CASES.length; ++i) { process(CASES[i]); break; }"
        assert "disable/early_return" in judged(before, after, tmp_path)

    def test_an_enhanced_for_made_final_with_a_trailing_break(self, tmp_path):
        before = "for (String s : names()) { assertTrue(ok(s)); }"
        after = "for (final String s : names()) { assertTrue(ok(s)); break; }"
        assert rejected(judged(before, after, tmp_path))


class TestManyTries:
    def test_many_new_swallowing_tries_stay_quick(self, tmp_path):
        tries = " ".join(f"try {{ step{k}(); }} catch (Exception e) {{ }}" for k in range(300))
        before = " ".join(f"step{k}();" for k in range(300)) + " assertTrue(done());"
        start = time.perf_counter()
        judged(before, tries + " assertTrue(done());", tmp_path)
        assert time.perf_counter() - start < 10


class TestTypeVariablesBehindAnnotations:
    def test_an_annotated_generic_helper(self, tmp_path):
        types = "static class Tol { double delta() { return TOL; } }\n"
        helper = (
            '@SuppressWarnings("unchecked") static <T extends Tol> double d(T t)'
            " { return t.delta(); }\n"
        )
        body = method("assertEquals(1.0, value(), d(new Tol()));")
        before = cls("static final double TOL = 0.001;\n" + types + helper + body)
        after = cls("static final double TOL = 0.5;\n" + types + helper + body)
        assert "weaken_assertion/tolerance_widened" in codes(before, after, tmp_path)
