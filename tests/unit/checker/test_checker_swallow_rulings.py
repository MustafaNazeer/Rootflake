import pytest
from test_checker_timing import cls, codes, judged, method

from rootflake.gate.checker.swallow import CATCH, RETRY_LOOP

REMOVED = "weaken_assertion/assertion_removed"
EARLY = "disable/early_return"
TEST = method("svc.process(); assertEquals(1, svc.count());")


def with_after(test: str, after: str) -> str:
    return cls(f"{test}\n@AfterEach void tearDown() throws Exception {{ {after} }}")


class TestAPristineStatementMovedIntoASwallowingTry:
    def test_wrapping_a_statement_the_test_ran_is_rejected(self, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = "try { svc.process(); } catch (Exception e) { } assertEquals(1, svc.count());"
        assert CATCH in judged(before, after, tmp_path)

    def test_wrapping_it_inside_a_submitted_callable_is_rejected(self, tmp_path):
        before = "pool.submit(() -> { svc.process(); return 1; }).get(); assertEquals(1, n);"
        after = (
            "pool.submit(() -> { try { svc.process(); } catch (Exception e) { } return 1; })"
            ".get(); assertEquals(1, n);"
        )
        assert CATCH in judged(before, after, tmp_path)

    def test_a_copy_run_ahead_of_the_statement_that_still_runs_is_not_caught(self, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = (
            "try { svc.process(); } catch (Throwable t) { System.err.println(t); }"
            " svc.process(); assertEquals(1, svc.count());"
        )
        assert CATCH not in judged(before, after, tmp_path)

    def test_a_catch_that_keeps_the_exception_for_a_later_assertion_is_not_caught(self, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = (
            "Exception seen = null; try { svc.process(); } catch (IllegalStateException e)"
            " { seen = e; } assertNotNull(seen);"
        )
        assert CATCH not in judged(before, after, tmp_path)

    def test_the_interrupt_idiom_around_a_pristine_call_is_judged_as_before(self, tmp_path):
        before = "latch.countDown(); assertEquals(1, svc.count());"
        after = (
            "try { latch.countDown(); } catch (InterruptedException e)"
            " { Thread.currentThread().interrupt(); } assertEquals(1, svc.count());"
        )
        assert CATCH not in judged(before, after, tmp_path)


class TestNewCleanupInASwallowingTry:
    def test_a_new_after_method_that_swallows_its_own_cleanup_is_accepted(self, tmp_path):
        after = with_after(TEST, "try { svc.reset(); } catch (Exception e) { }")
        assert CATCH not in codes(cls(TEST), after, tmp_path)

    def test_new_cleanup_that_ends_the_test_is_accepted(self, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = before + ' try { System.clearProperty("a"); } catch (SecurityException e) { }'
        assert CATCH not in judged(before, after, tmp_path)

    def test_cleanup_that_moves_a_pristine_statement_into_the_try_is_rejected(self, tmp_path):
        before = with_after(TEST, "svc.reset();")
        after = with_after(TEST, "try { svc.reset(); } catch (Exception e) { }")
        assert CATCH in codes(before, after, tmp_path)

    def test_a_try_ending_the_test_around_a_check_is_still_rejected(self, tmp_path):
        before = "svc.process();"
        after = "svc.process(); try { assertEquals(1, svc.count()); } catch (Throwable t) { }"
        assert CATCH in judged(before, after, tmp_path)


class TestACleanupWaitAfterTheTest:
    WAIT = (
        "pool.shutdownNow(); try { pool.awaitTermination(5, TimeUnit.SECONDS); }"
        " catch (InterruptedException e) { Thread.currentThread().interrupt(); }"
    )

    def test_a_shutdown_wait_in_an_after_method_that_restores_the_interrupt_is_accepted(
        self, tmp_path
    ):
        assert CATCH not in codes(cls(TEST), with_after(TEST, self.WAIT), tmp_path)

    def test_the_same_wait_in_the_test_with_an_assertion_after_it_is_rejected(self, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = "svc.process(); " + self.WAIT + " assertEquals(1, svc.count());"
        assert CATCH in judged(before, after, tmp_path)


class TestCheckPrefixesAtWordStarts:
    @pytest.mark.parametrize("name", ["checkpointAndReset", "expectedValues", "checkout"])
    def test_a_name_that_only_begins_with_the_letters_is_not_a_check(self, name, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = f"try {{ svc.{name}(); }} catch (Exception e) {{ }} " + before
        assert CATCH not in judged(before, after, tmp_path)

    @pytest.mark.parametrize("name", ["checkState", "verify", "expectNoErrors", "assert2"])
    def test_a_name_whose_first_word_is_a_check_is_one(self, name, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = f"try {{ svc.{name}(); }} catch (Exception e) {{ }} " + before
        assert CATCH in judged(before, after, tmp_path)


class TestKnownCosts:
    def test_a_hand_written_poll_until_assert_is_rejected(self, tmp_path):
        before = "svc.process(); assertEquals(1, svc.count());"
        after = (
            "svc.process(); for (int i = 0; i < 50; i++) { try { assertEquals(1, svc.count());"
            " break; } catch (AssertionError e) { } } assertEquals(1, svc.count());"
        )
        found = judged(before, after, tmp_path)
        assert CATCH in found and RETRY_LOOP in found


class TestExitsCountOnlyAgainstWhatTheyCanSkip:
    def test_a_break_in_a_search_loop_before_the_assertion_skips_nothing(self, tmp_path):
        before = "svc.use(k); assertEquals(3, svc.count());"
        after = (
            'for (String s : m.keySet()) { if (s.startsWith("a")) { k = s; break; } }'
            " svc.use(k); assertEquals(3, svc.count());"
        )
        found = judged(before, after, tmp_path)
        assert REMOVED not in found and EARLY not in found

    def test_a_switch_break_before_the_assertion_skips_nothing(self, tmp_path):
        before = "svc.use(k); assertEquals(3, svc.count());"
        after = (
            "switch (mode) { case 1: k = 2; break; default: k = 3; }"
            " svc.use(k); assertEquals(3, svc.count());"
        )
        assert REMOVED not in judged(before, after, tmp_path)

    def test_a_break_in_the_loop_that_holds_the_assertion_still_counts(self, tmp_path):
        before = "for (Item i : items) { svc.use(i); assertEquals(3, svc.count()); }"
        after = (
            "for (Item i : items) { svc.use(i); if (svc.done()) { break; }"
            " assertEquals(3, svc.count()); }"
        )
        assert REMOVED in judged(before, after, tmp_path)

    def test_a_continue_in_the_loop_that_holds_the_assertion_still_counts(self, tmp_path):
        before = "for (Item i : items) { svc.use(i); assertEquals(3, svc.count()); }"
        after = (
            "for (Item i : items) { svc.use(i); if (svc.done()) { continue; }"
            " assertEquals(3, svc.count()); }"
        )
        assert REMOVED in judged(before, after, tmp_path)

    def test_a_labelled_break_counts_against_everything_after_it(self, tmp_path):
        before = "svc.use(k); assertEquals(3, svc.count());"
        after = (
            "outer: for (String s : m.keySet()) { for (String t : n) { if (t.equals(s))"
            " { break outer; } } } svc.use(k); assertEquals(3, svc.count());"
        )
        assert REMOVED in judged(before, after, tmp_path)

    def test_a_guarded_break_in_a_new_poll_loop_is_not_an_early_return(self, tmp_path):
        before = "svc.start(); assertEquals(1, svc.count());"
        after = (
            "svc.start(); for (int i = 0; i < 100; i++) { if (svc.count() >= 1) { break; }"
            " Thread.yield(); } assertEquals(1, svc.count());"
        )
        assert EARLY not in judged(before, after, tmp_path)

    def test_a_break_in_a_new_loop_around_a_pristine_statement_is_an_early_return(self, tmp_path):
        before = "for (Item i : items) { svc.use(i); } assertEquals(3, svc.count());"
        after = (
            "for (Item i : items) { svc.use(i); } for (Item i : more) { if (i == null)"
            " { break; } svc.use(i); } assertEquals(3, svc.count());"
        )
        assert EARLY in judged(before, after, tmp_path)
