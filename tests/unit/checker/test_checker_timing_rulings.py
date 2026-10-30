import pytest
from test_checker_timing import IMPORTS, TIMING, cls, codes, judged, method

REMOVED = "weaken_assertion/assertion_removed"
EXPECTED = "swallow_failure/expected_exception_added"
A = "assertEquals(1, svc.count());"
MOCKITO = IMPORTS + "import static org.mockito.Mockito.*;\n"
ATOMIC = IMPORTS + "import java.util.concurrent.atomic.*;\n"
TESTNG = "import org.testng.annotations.Test;\nimport static org.testng.Assert.*;\n"


def mocked(before: str, after: str, tmp_path) -> set[str]:
    return codes(cls(method(before), MOCKITO), cls(method(after), MOCKITO), tmp_path)


class TestABoundTakenOffATimedWait:
    @pytest.mark.parametrize(
        ("before", "after"),
        [("f.get(500, TimeUnit.MILLISECONDS);", "f.get();"),
         ("sem.tryAcquire(5, TimeUnit.SECONDS);", "sem.acquire();"),
         ("lock.tryLock(5, TimeUnit.SECONDS);", "lock.lock();"),
         ("String s = q.poll(5, TimeUnit.SECONDS); assertEquals(\"a\", s);",
          "String s = q.take(); assertEquals(\"a\", s);"),
         ("t.join(500);", "t.join();")],
        ids=["get", "tryAcquire", "tryLock", "poll", "join"],
    )  # fmt: skip
    def test_is_widened(self, tmp_path, before, after):
        assert TIMING in judged(f"{before} {A}", f"{after} {A}", tmp_path)

    def test_an_unbounded_wait_on_another_receiver_is_not(self, tmp_path):
        before = f"f.get(500, TimeUnit.MILLISECONDS); {A}"
        after = f"f.get(500, TimeUnit.MILLISECONDS); other.get(); {A}"
        assert TIMING not in judged(before, after, tmp_path)

    def test_an_unbounded_wait_the_test_already_made_is_not(self, tmp_path):
        before = f"f.get(); {A}"
        after = f"f.get(); f.get(); {A}"
        assert TIMING not in judged(before, after, tmp_path)

    def test_a_timed_wait_dropped_with_no_unbounded_one_added_is_not(self, tmp_path):
        before = f"f.get(500, TimeUnit.MILLISECONDS); {A}"
        assert TIMING not in judged(before, A, tmp_path)


class TestAWaitMovedToAnotherMember:
    def test_a_raised_bound_moved_into_a_helper_is_widened(self, tmp_path):
        before = method(f"helper(); assertTrue(latch.await(1, TimeUnit.SECONDS)); {A}")
        before += "\nvoid helper() { svc.start(); }"
        after = method(f"helper(); {A}")
        after += "\nvoid helper() throws Exception { svc.start(); "
        after += "assertTrue(latch.await(60, TimeUnit.SECONDS)); }"
        assert TIMING in codes(cls(before), cls(after), tmp_path)

    def test_the_same_bound_moved_into_a_helper_is_not(self, tmp_path):
        before = method(f"helper(); assertTrue(latch.await(1, TimeUnit.SECONDS)); {A}")
        before += "\nvoid helper() { svc.start(); }"
        after = method(f"helper(); {A}")
        after += "\nvoid helper() throws Exception { svc.start(); "
        after += "assertTrue(latch.await(1, TimeUnit.SECONDS)); }"
        assert TIMING not in codes(cls(before), cls(after), tmp_path)

    def test_a_raised_awaitility_bound_moved_into_a_helper_is_widened(self, tmp_path):
        wait = "await().atMost({0}, TimeUnit.SECONDS).until(() -> svc.count() == 1);"
        before = method(f"{wait.format(1)} {A}") + "\nvoid helper() { }"
        after = method(f"helper(); {A}") + f"\nvoid helper() {{ {wait.format(30)} }}"
        assert TIMING in codes(cls(before), cls(after), tmp_path)


class TestMockitoVerificationModes:
    def test_a_raised_timeout_is_widened(self, tmp_path):
        before = f"verify(m, timeout(100)).run(); {A}"
        after = f"verify(m, timeout(5000)).run(); {A}"
        assert TIMING in mocked(before, after, tmp_path)

    def test_a_lowered_timeout_is_not(self, tmp_path):
        before = f"verify(m, timeout(5000)).run(); {A}"
        after = f"verify(m, timeout(100)).run(); {A}"
        assert TIMING not in mocked(before, after, tmp_path)

    def test_an_added_timeout_is_a_condition_wait(self, tmp_path):
        assert TIMING not in mocked(f"verify(m).run(); {A}", f"verify(m, timeout(100)).run(); {A}",
                                    tmp_path)  # fmt: skip

    @pytest.mark.parametrize(
        ("before", "after"),
        [("verify(m).run();", "verify(m, after(2000)).run();"),
         ("verify(m, after(100)).run();", "verify(m, after(2000)).run();"),
         ("verify(m).run();", "verify(m, Mockito.after(2000).never()).run();")],
        ids=["added", "raised", "qualified"],
    )  # fmt: skip
    def test_an_added_or_raised_after_is_a_fixed_sleep(self, tmp_path, before, after):
        assert TIMING in mocked(f"{before} {A}", f"{after} {A}", tmp_path)

    def test_an_after_that_replaces_a_sleep_as_long_is_not(self, tmp_path):
        before = f"Thread.sleep(2000); verify(m).run(); {A}"
        after = f"verify(m, after(2000)).run(); {A}"
        assert TIMING not in mocked(before, after, tmp_path)

    def test_an_after_longer_than_the_sleep_it_replaces_is_widened(self, tmp_path):
        before = f"Thread.sleep(500); verify(m).run(); {A}"
        after = f"verify(m, after(2000)).run(); {A}"
        assert TIMING in mocked(before, after, tmp_path)


class TestADroppedTimedWaitResult:
    @pytest.mark.parametrize(
        "wait",
        ["sem.tryAcquire(5, TimeUnit.SECONDS);", "lock.tryLock(5, TimeUnit.SECONDS);",
         "queue.poll(5, TimeUnit.SECONDS);", "sem.tryAcquire(2, 5, TimeUnit.SECONDS);"],
    )  # fmt: skip
    def test_is_a_fixed_sleep(self, tmp_path, wait):
        assert TIMING in judged(A, f"{wait} {A}", tmp_path)

    @pytest.mark.parametrize(
        "wait",
        ["assertTrue(sem.tryAcquire(5, TimeUnit.SECONDS));",
         'assertEquals("a", queue.poll(5, TimeUnit.SECONDS));',
         "Object x = queue.poll(5, TimeUnit.SECONDS); assertNotNull(x);",
         "if (!lock.tryLock(5, TimeUnit.SECONDS)) { fail(); }",
         "queue.poll();"],
    )  # fmt: skip
    def test_a_checked_or_untimed_one_is_not(self, tmp_path, wait):
        assert TIMING not in judged(A, f"{wait} {A}", tmp_path)


class TestObjectWait:
    @pytest.mark.parametrize(
        "wait",
        ["synchronized (lock) { lock.wait(2000); }", "synchronized (this) { wait(); }",
         "synchronized (lock) { lock.wait(2, 0); }"],
    )  # fmt: skip
    def test_an_added_wait_outside_a_recheck_loop_is_a_fixed_sleep(self, tmp_path, wait):
        assert TIMING in judged(A, f"{wait} {A}", tmp_path)

    def test_an_added_unbounded_recheck_loop_is_a_fixed_sleep(self, tmp_path):
        after = f"synchronized (lock) {{ while (!svc.ready()) {{ lock.wait(100); }} }} {A}"
        assert TIMING in judged(A, after, tmp_path)

    def test_an_added_bounded_and_checked_recheck_loop_is_not(self, tmp_path):
        after = (
            "long end = System.currentTimeMillis() + 5000; synchronized (lock) { "
            "while (!svc.ready()) { if (System.currentTimeMillis() > end) { fail(); } "
            f"lock.wait(100); }} }} {A}"
        )
        assert TIMING not in judged(A, after, tmp_path)

    def test_a_wait_the_test_already_made_is_not(self, tmp_path):
        wait = "synchronized (lock) { lock.wait(2000); }"
        assert TIMING not in judged(f"{wait} {A}", f"{wait} {A}", tmp_path)

    def test_a_declared_wait_method_is_followed_instead(self, tmp_path):
        before = cls(method(A) + "\nvoid wait(String s) { }")
        after = cls(method(f'wait("x"); {A}') + "\nvoid wait(String s) { }")
        assert TIMING not in codes(before, after, tmp_path)


class TestPollDelayAndDuring:
    WAIT = "await(){0}.atMost(5, TimeUnit.SECONDS).until(() -> svc.count() == 1);"

    @pytest.mark.parametrize(
        ("before", "after"),
        [("", ".pollDelay(3, TimeUnit.SECONDS)"),
         (".pollDelay(1, TimeUnit.SECONDS)", ".pollDelay(3, TimeUnit.SECONDS)"),
         ("", ".pollDelay(Duration.ofSeconds(3))")],
        ids=["added", "raised", "duration"],
    )  # fmt: skip
    def test_an_added_or_raised_poll_delay_is_a_fixed_sleep(self, tmp_path, before, after):
        old, new = self.WAIT.format(before), self.WAIT.format(after)
        assert judged(f"{old} {A}", f"{new} {A}", tmp_path) == {TIMING}

    @pytest.mark.parametrize(
        ("before", "after"),
        [(".pollDelay(3, TimeUnit.SECONDS)", ".pollDelay(1, TimeUnit.SECONDS)"),
         ("", ".pollDelay(0, TimeUnit.SECONDS)"),
         ("", ".pollInterval(100, TimeUnit.MILLISECONDS)")],
        ids=["lowered", "zero", "interval"],
    )  # fmt: skip
    def test_a_lowered_or_zero_delay_and_a_poll_interval_are_not(self, tmp_path, before, after):
        old, new = self.WAIT.format(before), self.WAIT.format(after)
        assert judged(f"{old} {A}", f"{new} {A}", tmp_path) == set()

    def test_a_poll_delay_replacing_a_sleep_as_long_is_not(self, tmp_path):
        new = self.WAIT.format(".pollDelay(2, TimeUnit.SECONDS)")
        assert judged(f"Thread.sleep(3000); {A}", f"{new} {A}", tmp_path) == set()

    def test_during_on_a_condition_that_always_holds_is_a_fixed_sleep(self, tmp_path):
        after = (
            "await().during(2, TimeUnit.SECONDS).atMost(5, TimeUnit.SECONDS)"
            f".until(() -> true); {A}"
        )
        assert TIMING in judged(A, after, tmp_path)

    def test_during_on_a_real_condition_is_not(self, tmp_path):
        after = (
            "await().during(2, TimeUnit.SECONDS).atMost(5, TimeUnit.SECONDS)"
            f".until(() -> svc.count() == 1); {A}"
        )
        assert TIMING not in judged(A, after, tmp_path)

    def test_an_added_at_most_keeps_the_wait_the_same_assertion(self, tmp_path):
        before = f"await().until(() -> svc.count() == 1); {A}"
        after = f"await().atMost(5, TimeUnit.SECONDS).until(() -> svc.count() == 1); {A}"
        assert judged(before, after, tmp_path) == set()

    def test_a_removed_during_is_still_an_assertion_removed(self, tmp_path):
        before = (
            "await().during(2, TimeUnit.SECONDS).atMost(5, TimeUnit.SECONDS)"
            f".until(() -> svc.count() == 1); {A}"
        )
        after = f"await().atMost(5, TimeUnit.SECONDS).until(() -> svc.count() == 1); {A}"
        assert REMOVED in judged(before, after, tmp_path)


class TestAThrowAtAPollLoopsBound:
    LOOP = (
        "long end = System.currentTimeMillis() + 1000; while (svc.count() != 1) { "
        'if (System.currentTimeMillis() > end) { throw new AssertionError("timeout"); } '
        "Thread.sleep(10); }"
    )

    def test_hides_no_assertion_after_the_loop(self, tmp_path):
        assert judged(f"Thread.sleep(1000); {A}", f"{self.LOOP} {A}", tmp_path) == set()

    def test_under_an_expected_exception_it_still_can(self, tmp_path):
        kw = {"annotations": '@Test @SuppressWarnings("x")'}
        old = cls(method(f"Thread.sleep(1000); {A}", **kw), TESTNG).replace(
            "@Test @", "@Test(expectedExceptions = AssertionError.class) @"
        )
        new = cls(method(f"{self.LOOP} {A}", **kw), TESTNG).replace(
            "@Test @", "@Test(expectedExceptions = AssertionError.class) @")  # fmt: skip
        assert REMOVED in codes(old, new, tmp_path)


class TestUntilTrue:
    @pytest.mark.parametrize(
        ("assertion", "wait"),
        [("assertTrue(done.get());", "untilTrue(done)"),
         ("assertFalse(done.get());", "untilFalse(done)"),
         ("assertTrue(this.done.get());", "untilTrue(this.done)")],
    )  # fmt: skip
    def test_credits_the_assertion_it_stands_for(self, tmp_path, assertion, wait):
        before = f"Thread.sleep(500); {assertion} {A}"
        after = f"await().atMost(5, TimeUnit.SECONDS).{wait}; {A}"
        assert judged(before, after, tmp_path) == set()

    @pytest.mark.parametrize(
        ("assertion", "wait", "code"),
        [("assertTrue(done.get());", "untilFalse(done)", REMOVED),
         ("assertTrue(done.get());", "untilTrue(other)", "flag/assertion_subject_replaced")],
    )  # fmt: skip
    def test_does_not_credit_another_one(self, tmp_path, assertion, wait, code):
        before = f"Thread.sleep(500); {assertion} {A}"
        after = f"await().atMost(5, TimeUnit.SECONDS).{wait}; {A}"
        assert code in judged(before, after, tmp_path)


class TestBusyWaits:
    @pytest.mark.parametrize(
        "loop",
        ["while (!svc.ready()) { }", "while (!svc.ready()) ;",
         "while (!svc.ready()) { Thread.onSpinWait(); }",
         "do { Thread.yield(); } while (!svc.ready());",
         "while (!latch.await(1, TimeUnit.SECONDS)) { }",
         "while (!latch.await(1, TimeUnit.SECONDS)) { log(\"waiting\"); }"],
    )  # fmt: skip
    def test_an_added_unbounded_one_is_a_fixed_sleep(self, tmp_path, loop):
        assert TIMING in judged(A, f"{loop} {A}", tmp_path)

    @pytest.mark.parametrize(
        "loop",
        ["for (int i = 0; i < 1000 && !svc.ready(); i++) { Thread.onSpinWait(); } "
         "assertTrue(svc.ready());",
         "long end = System.nanoTime() + 1_000_000_000L; while (!svc.ready()) { "
         "if (System.nanoTime() > end) { fail(); } Thread.onSpinWait(); }",
         "while (it.hasNext()) { it.next(); }"],
    )  # fmt: skip
    def test_a_bounded_and_checked_one_or_a_loop_that_does_work_is_not(self, tmp_path, loop):
        assert TIMING not in judged(A, f"{loop} {A}", tmp_path)

    def test_one_the_test_already_had_is_not(self, tmp_path):
        loop = "while (!svc.ready()) { Thread.onSpinWait(); }"
        assert TIMING not in judged(f"{loop} {A}", f"{loop} {A} g();", tmp_path)


class TestWallTimeLimit:
    def test_stays_unset_until_the_thresholds_record_sets_it(self):
        from rootflake.gate.checker import timing

        assert timing.WALL_TIME_LIMIT_MS is None


class TestTestNGExpectedExceptions:
    BODY = "{ svc.process(); assertEquals(svc.count(), 1); }"

    def judge(self, before: str, after: str, tmp_path) -> set[str]:
        old = f"@Test{before} public void testFoo() {self.BODY}"
        new = f"@Test{after} public void testFoo() {self.BODY}"
        return codes(cls(old, TESTNG), cls(new, TESTNG), tmp_path)

    @pytest.mark.parametrize(
        "types",
        [
            "Exception.class",
            "AssertionError.class",
            "{ IOException.class, IllegalStateException.class }",
        ],
    )
    def test_an_added_one_is_rejected(self, tmp_path, types):
        assert EXPECTED in self.judge("", f"(expectedExceptions = {types})", tmp_path)

    def test_one_the_test_had_is_not_added(self, tmp_path):
        attribute = "(expectedExceptions = IllegalStateException.class)"
        assert self.judge(attribute, attribute, tmp_path) == set()

    def test_a_widened_one_is_broadened_not_added(self, tmp_path):
        found = self.judge("(expectedExceptions = IllegalStateException.class)",
                           "(expectedExceptions = RuntimeException.class)", tmp_path)  # fmt: skip
        assert found == {"weaken_assertion/expected_exception_broadened"}

    def test_other_test_attributes_are_not(self, tmp_path):
        assert EXPECTED not in self.judge("", '(groups = "slow")', tmp_path)


class TestCodeMovedAfterAnUnmovedDroppedResult:
    # Open: the ruling covers added waits; a real fix moved assertions after an unmoved
    # completion.poll (accept-nod-assert-after-completion-wait), so that case stays accepted
    # until it is decided, while a latch keeps its earlier reading.
    def test_after_an_unmoved_poll_is_accepted_for_now(self, tmp_path):
        before = "go(); assertTrue(x()); q.poll(1, TimeUnit.SECONDS);"
        after = "go(); q.poll(1, TimeUnit.SECONDS); assertTrue(x());"
        assert TIMING not in judged(before, after, tmp_path)

    def test_after_an_unmoved_latch_is_still_a_fixed_sleep(self, tmp_path):
        before = "go(); assertTrue(x()); latch.await(1, TimeUnit.SECONDS);"
        after = "go(); latch.await(1, TimeUnit.SECONDS); assertTrue(x());"
        assert TIMING in judged(before, after, tmp_path)
