from test_checker_timing import TIMING, cls, codes, judged, method

DEADLINE = "long deadline = System.currentTimeMillis() + 5000;"


def with_helper(helper: str, before_body: str, after_body: str, tmp_path) -> set[str]:
    return codes(
        cls(f"{helper}\n{method(before_body)}"), cls(f"{helper}\n{method(after_body)}"), tmp_path
    )


class TestInlineLoops:
    def test_a_bounded_loop_asserted_after_on_its_condition_is_a_poll(self, tmp_path):
        after = (
            f"f(); {DEADLINE} while (!ready() && System.currentTimeMillis() < deadline)"
            " { Thread.sleep(10); } assertTrue(ready());"
        )
        assert TIMING not in judged("f(); Thread.sleep(500); assertTrue(ready());", after, tmp_path)

    def test_a_silent_loop_followed_by_another_assertion_is_a_fixed_sleep(self, tmp_path):
        after = (
            f"f(); {DEADLINE} while (!ready() && System.currentTimeMillis() < deadline)"
            " { Thread.sleep(10); } assertEquals(3, count());"
        )
        assert TIMING in judged("f(); assertEquals(3, count());", after, tmp_path)

    def test_a_loop_that_throws_at_its_deadline_is_a_poll(self, tmp_path):
        after = (
            f"f(); {DEADLINE} while (!ready()) {{ if (System.currentTimeMillis() > deadline)"
            ' throw new AssertionError("late"); Thread.sleep(10); } use();'
        )
        assert TIMING not in judged("f(); use();", after, tmp_path)

    def test_a_loop_reading_only_the_clock_is_a_fixed_sleep(self, tmp_path):
        after = (
            "f(); long until = System.currentTimeMillis() + 200; while"
            ' (System.currentTimeMillis() < until) { Thread.sleep(10); } fail("x");'
        )
        assert TIMING in judged("f();", after, tmp_path)

    def test_a_loop_that_sleeps_before_its_first_check_is_a_fixed_sleep(self, tmp_path):
        after = (
            f"f(); {DEADLINE} do {{ Thread.sleep(10); }} while (!ready()"
            " && System.currentTimeMillis() < deadline); assertTrue(ready());"
        )
        assert TIMING in judged("f(); assertTrue(ready());", after, tmp_path)

    def test_a_counted_loop_asserted_after_is_a_poll(self, tmp_path):
        after = (
            "f(); for (int i = 0; i < 50 && !ready(); i++) { Thread.sleep(10); }"
            " assertTrue(ready());"
        )
        assert TIMING not in judged("f(); assertTrue(ready());", after, tmp_path)

    def test_a_raised_interval_in_an_existing_poll_loop_is_widened(self, tmp_path):
        loop = (
            "for (int i = 0; i < 50 && !ready(); i++) {{ Thread.sleep({}); }} assertTrue(ready());"
        )
        assert TIMING in judged(loop.format(10), loop.format(100), tmp_path)

    def test_a_lowered_one_is_not(self, tmp_path):
        loop = (
            "for (int i = 0; i < 50 && !ready(); i++) {{ Thread.sleep({}); }} assertTrue(ready());"
        )
        assert TIMING not in judged(loop.format(100), loop.format(10), tmp_path)


ASSERTING = """static void awaitTrue(java.util.function.Supplier<Boolean> condition, long ms)
        throws Exception {
    long deadline = System.currentTimeMillis() + ms;
    while (System.currentTimeMillis() < deadline) {
        if (Boolean.TRUE.equals(condition.get())) { return; }
        Thread.sleep(10);
    }
    assertTrue(condition.get());
}"""

SILENT = """static void awaitStatus(Task task, Status expected, long ms) throws Exception {
    long deadline = System.currentTimeMillis() + ms;
    while (task.status() != expected && System.currentTimeMillis() < deadline) {
        Thread.sleep(20);
    }
}"""

RETURNING = """static boolean waitUntilReady(long ms) throws Exception {
    long deadline = System.currentTimeMillis() + ms;
    while (System.currentTimeMillis() < deadline) {
        if (ready()) { return true; }
        Thread.sleep(20);
    }
    return false;
}"""


class TestPollHelpers:
    def test_an_asserting_helper_reading_state_is_a_poll(self, tmp_path):
        found = with_helper(
            ASSERTING, "Thread.sleep(200); use();", "awaitTrue(() -> ready(), 1000); use();",
            tmp_path,
        )  # fmt: skip
        assert TIMING not in found

    def test_the_same_helper_reading_only_the_clock_is_a_fixed_sleep(self, tmp_path):
        after = (
            "long until = System.currentTimeMillis() + 200;"
            " awaitTrue(() -> System.currentTimeMillis() >= until, 1000); use();"
        )
        assert TIMING in with_helper(ASSERTING, "Thread.sleep(200); use();", after, tmp_path)

    def test_a_silent_helper_followed_by_an_assertion_of_its_exit_is_a_poll(self, tmp_path):
        after = "awaitStatus(task, Status.DONE, 5000); assertEquals(Status.DONE, task.status());"
        before = "Thread.sleep(100); assertEquals(Status.DONE, task.status());"
        assert TIMING not in with_helper(SILENT, before, after, tmp_path)

    def test_a_silent_helper_followed_by_another_assertion_is_a_fixed_sleep(self, tmp_path):
        after = "awaitStatus(task, Status.DONE, 5000); assertEquals(0, task.failures());"
        before = "Thread.sleep(100); assertEquals(0, task.failures());"
        assert TIMING in with_helper(SILENT, before, after, tmp_path)

    def test_a_silent_helper_as_the_last_statement_is_a_fixed_sleep(self, tmp_path):
        after = "use(); awaitStatus(task, Status.DONE, 5000);"
        assert TIMING in with_helper(SILENT, "use();", after, tmp_path)

    def test_a_silent_helper_whose_result_is_asserted_is_a_poll(self, tmp_path):
        after = "use(); assertTrue(waitUntilReady(5000));"
        assert TIMING not in with_helper(RETURNING, "use(); Thread.sleep(100);", after, tmp_path)
