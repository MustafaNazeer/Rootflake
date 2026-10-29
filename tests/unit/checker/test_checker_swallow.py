from test_checker_timing import cls, codes, judged, method

from rootflake.gate.checker.swallow import CATCH, EXIT, EXPECTED, HANDLER, RETRY_LOOP

BODY = "int n = compute(); assertEquals(3, n);"
JUNIT4 = """import org.junit.*;
import org.junit.rules.ExpectedException;
import static org.junit.Assert.*;
"""


def wrapped(catch: str, types: str = "AssertionError e", body: str = BODY) -> str:
    return f"try {{ {body} }} catch ({types}) {{ {catch} }}"


class TestRuleOne:
    def test_an_empty_catch_around_an_assertion(self, tmp_path):
        assert CATCH in judged(BODY, wrapped(""), tmp_path)

    def test_a_catch_that_only_prints(self, tmp_path):
        assert CATCH in judged(BODY, wrapped("e.printStackTrace();", "Throwable e"), tmp_path)

    def test_a_catch_that_returns(self, tmp_path):
        assert CATCH in judged(BODY, wrapped("return;", "Exception e"), tmp_path)

    def test_a_catch_that_rethrows_is_not(self, tmp_path):
        assert CATCH not in judged(BODY, wrapped("log(e); throw e;"), tmp_path)

    def test_a_catch_that_wraps_and_throws_is_not(self, tmp_path):
        after = wrapped("throw new IllegalStateException(e);", "Exception e")
        assert CATCH not in judged(BODY, after, tmp_path)

    def test_a_catch_that_fails_is_not(self, tmp_path):
        assert CATCH not in judged(BODY, wrapped('fail("broke");', "Exception e"), tmp_path)

    def test_a_catch_that_throws_an_assumption_aborts_the_test(self, tmp_path):
        after = wrapped("throw new org.opentest4j.TestAbortedException();", "Exception e")
        assert CATCH in judged(BODY, after, tmp_path)

    def test_a_try_with_only_finally_is_not(self, tmp_path):
        assert CATCH not in judged(BODY, f"try {{ {BODY} }} finally {{ close(); }}", tmp_path)

    def test_a_wait_counts_as_a_check(self, tmp_path):
        before = "latch.await(5, TimeUnit.SECONDS);"
        after = wrapped("", "Exception e", before)
        assert CATCH in judged(before, after, tmp_path)

    def test_a_helper_named_like_a_check_counts_in_test_code(self, tmp_path):
        before = "verifyState(store);"
        assert CATCH in judged(before, wrapped("", "Exception e", before), tmp_path)


class TestRulesTwoAndThree:
    def test_a_value_set_in_the_try_and_asserted_later(self, tmp_path):
        before = "int n = compute(); assertEquals(3, n);"
        after = "int n = 0; try { n = compute(); } catch (Exception e) { } assertEquals(3, n);"
        assert CATCH in judged(before, after, tmp_path)

    def test_a_value_followed_through_a_later_assignment(self, tmp_path):
        before = "int n = compute(); int m = n + 1; assertEquals(4, m);"
        after = (
            "int n = 0; try { n = compute(); } catch (Exception e) { }"
            " int m = n + 1; assertEquals(4, m);"
        )
        assert CATCH in judged(before, after, tmp_path)

    def test_a_try_that_ends_the_test(self, tmp_path):
        before = "start(); compute();"
        after = "start(); try { compute(); } catch (Exception e) { }"
        assert CATCH in judged(before, after, tmp_path)

    def test_a_cleanup_try_with_an_assertion_after_it_is_not(self, tmp_path):
        before = "int n = compute(); assertEquals(3, n);"
        after = "int n = compute(); try { close(); } catch (Exception e) { } assertEquals(3, n);"
        assert CATCH not in judged(before, after, tmp_path)


class TestTryAndFail:
    BEFORE = "parse(good);"
    IDIOM = 'try { parse(bad); fail("no error"); } catch (IllegalArgumentException e) { }'

    def test_the_expected_exception_idiom_is_not(self, tmp_path):
        assert CATCH not in judged(self.BEFORE, self.IDIOM, tmp_path)

    def test_a_return_before_the_fail_voids_it(self, tmp_path):
        after = self.IDIOM.replace("parse(bad);", "if (skip()) return; parse(bad);")
        assert CATCH in judged(self.BEFORE, after, tmp_path)

    def test_catching_an_assertion_error_voids_it(self, tmp_path):
        after = self.IDIOM.replace("IllegalArgumentException", "AssertionError")
        assert CATCH in judged(self.BEFORE, after, tmp_path)

    def test_catching_a_listed_assertion_subclass_voids_it(self, tmp_path):
        after = self.IDIOM.replace("IllegalArgumentException", "AssertionFailedError")
        assert CATCH in judged(self.BEFORE, after, tmp_path)

    def test_a_fail_declared_in_a_read_file_voids_it(self, tmp_path):
        before = cls(method(self.BEFORE))
        after = cls("static void fail(String m) { }\n" + method(self.IDIOM))
        assert CATCH in codes(before, after, tmp_path)

    def test_the_pristine_call_moved_in_voids_it(self, tmp_path):
        after = self.IDIOM.replace("parse(bad);", "parse(good);")
        assert CATCH in judged(self.BEFORE, after, tmp_path)

    def test_pristine_statements_moved_in_void_it(self, tmp_path):
        before = "int n = compute(); assertEquals(3, n);"
        after = (
            'try { int n = compute(); assertEquals(3, n); fail("x"); }'
            " catch (IllegalStateException e) { }"
        )
        assert CATCH in judged(before, after, tmp_path)


class TestInterruptIdiom:
    def test_restoring_the_interrupt_around_a_sleep_is_not(self, tmp_path):
        after = (
            "try { Thread.sleep(10); } catch (InterruptedException e) {"
            " Thread.currentThread().interrupt(); } compute(); assertTrue(done());"
        )
        assert CATCH not in judged("compute(); assertTrue(done());", after, tmp_path)

    def test_restoring_the_interrupt_around_an_assertion_is(self, tmp_path):
        before = "assertTrue(done());"
        after = (
            "try { assertTrue(done()); } catch (InterruptedException e) {"
            " Thread.currentThread().interrupt(); }"
        )
        assert CATCH in judged(before, after, tmp_path)


class TestWidenedTries:
    def test_an_assertion_moved_into_an_existing_swallowing_try(self, tmp_path):
        before = "try { connect(); } catch (Exception e) { } assertTrue(done());"
        after = "try { connect(); assertTrue(done()); } catch (Exception e) { }"
        assert CATCH in judged(before, after, tmp_path)

    def test_a_call_added_to_an_existing_try_is_not(self, tmp_path):
        before = "try { connect(); } catch (Exception e) { } assertTrue(done());"
        after = "try { connect(); log(); } catch (Exception e) { } assertTrue(done());"
        assert CATCH not in judged(before, after, tmp_path)


class TestRetryLoops:
    def test_a_swallowing_try_around_a_check_in_a_loop(self, tmp_path):
        after = (
            f"for (int i = 0; i < 3; i++) {{ try {{ {BODY} break; }} catch (Throwable t) {{ }} }}"
        )
        found = judged(BODY, after, tmp_path)
        assert {CATCH, RETRY_LOOP} <= found
        assert "disable/early_return" not in found

    def test_a_guarded_break_before_cases_in_a_new_loop_is_an_early_return(self, tmp_path):
        before = "compute(); assertTrue(done());"
        after = "for (int i = 0; i < 1; i++) { if (slow()) break; compute(); assertTrue(done()); }"
        assert "disable/early_return" in judged(before, after, tmp_path)


class TestExits:
    def test_a_return_added_to_an_existing_catch(self, tmp_path):
        before = "try { connect(); } catch (Exception e) { log(e); } assertTrue(done());"
        after = before.replace("log(e);", "log(e); return;")
        assert EXIT in judged(before, after, tmp_path)

    def test_a_break_of_a_loop_inside_the_catch_is_not(self, tmp_path):
        before = "try { connect(); } catch (Exception e) { log(e); } assertTrue(done());"
        after = before.replace("log(e);", "for (Object o : list) { if (o == null) break; }")
        assert EXIT not in judged(before, after, tmp_path)


class TestExpectedExceptions:
    def test_an_added_junit4_expected_attribute(self, tmp_path):
        before = cls(method("compute();", "@Test"), JUNIT4)
        after = cls(method("compute();", "@Test(expected = Exception.class)"), JUNIT4)
        assert EXPECTED in codes(before, after, tmp_path)

    def test_an_added_expected_exception_rule(self, tmp_path):
        rule = "@Rule public ExpectedException thrown = ExpectedException.none();\n"
        before = cls(method("compute();"), JUNIT4)
        after = cls(rule + method("thrown.expect(Exception.class); compute();"), JUNIT4)
        assert EXPECTED in codes(before, after, tmp_path)

    def test_an_unchanged_expected_attribute_is_not(self, tmp_path):
        source = cls(method("compute();", "@Test(expected = Exception.class)"), JUNIT4)
        assert EXPECTED not in codes(source, source.replace("compute();", "run();"), tmp_path)


class TestHandlers:
    HANDLER_CLASS = (
        "static class Quiet implements TestExecutionExceptionHandler {"
        " public void handleTestExecutionException(ExtensionContext c, Throwable t) { } }\n"
    )
    IMPORTS = (
        "import org.junit.jupiter.api.*;\nimport org.junit.jupiter.api.extension.*;\n"
        "import static org.junit.jupiter.api.Assertions.*;\n"
    )

    def test_an_added_exception_handler(self, tmp_path):
        before = cls(method("compute();"), self.IMPORTS)
        after = cls(self.HANDLER_CLASS + method("compute();"), self.IMPORTS)
        assert HANDLER in codes(before, after, tmp_path)


class TestHelpers:
    HELPER = "static void quietly(Runnable r) { try { r.run(); } catch (Throwable t) { } }\n"

    def test_a_check_passed_to_a_new_swallowing_helper(self, tmp_path):
        before = cls(self.HELPER + method("assertTrue(done());"))
        after = cls(self.HELPER + method("quietly(() -> assertTrue(done()));"))
        assert CATCH in codes(before, after, tmp_path)

    def test_a_helper_that_swallows_around_its_own_check(self, tmp_path):
        helper = "static void check() { try { assertTrue(done()); } catch (Throwable t) { } }\n"
        before = cls(helper + method("assertTrue(done());"))
        after = cls(helper + method("check();"))
        assert CATCH in codes(before, after, tmp_path)

    def test_a_plain_call_passed_to_a_swallowing_helper_is_not(self, tmp_path):
        before = cls(self.HELPER + method("cleanup(); assertTrue(done());"))
        after = cls(self.HELPER + method("quietly(() -> cleanup()); assertTrue(done());"))
        assert CATCH not in codes(before, after, tmp_path)
