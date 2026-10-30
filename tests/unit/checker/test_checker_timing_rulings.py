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
        before = "assertTrue(x()); go(); q.poll(1, TimeUnit.SECONDS);"
        after = "go(); q.poll(1, TimeUnit.SECONDS); assertTrue(x());"
        assert TIMING not in judged(before, after, tmp_path)

    @pytest.mark.parametrize(
        "wait", ["q.poll(5, TimeUnit.SECONDS);", "sem.tryAcquire(5, TimeUnit.SECONDS);"]
    )
    def test_a_moved_one_is_still_a_fixed_sleep(self, tmp_path, wait):
        before = f"svc.start(); assertTrue(x()); {wait}"
        after = f"svc.start(); {wait} assertTrue(x());"
        assert TIMING in judged(before, after, tmp_path)

    def test_after_an_unmoved_latch_is_still_a_fixed_sleep(self, tmp_path):
        before = "go(); assertTrue(x()); latch.await(1, TimeUnit.SECONDS);"
        after = "go(); latch.await(1, TimeUnit.SECONDS); assertTrue(x());"
        assert TIMING in judged(before, after, tmp_path)


class TestReviewOfTheRulings:
    def test_a_thrown_type_a_read_file_declares_stays_an_exit(self, tmp_path):
        pristine, workspace = tmp_path / "p", tmp_path / "w"
        shadow = (
            "package a;\npublic class TimeoutException extends "
            "org.opentest4j.TestAbortedException { TimeoutException(String m) { } }\n"
        )
        test = "src/test/java/a/FooTest.java"
        old = cls(method(A))
        new = cls(method(f'if (svc.flaky()) {{ throw new TimeoutException("x"); }} {A}'))
        for root, source, extra in ((pristine, old, False), (workspace, new, True)):
            root.mkdir(parents=True)
            (root / "pom.xml").write_text("<project/>")
            (root / test).parent.mkdir(parents=True)
            (root / test).write_text(source)
            if extra:
                (root / "src/test/java/a/TimeoutException.java").write_text(shadow)
        from rootflake.gate.checker.check import Target, check

        found = check([test, "src/test/java/a/TimeoutException.java"], pristine, workspace,
                      Target("a.FooTest", "testFoo")).findings  # fmt: skip
        assert REMOVED in {f.code for f in found}

    def test_a_class_level_testng_expected_exception_keeps_a_throw_an_exit(self, tmp_path):
        header = "@Test(expectedExceptions = IllegalStateException.class)"
        body = "@Test public void testFoo() {{ {0} assertEquals(svc.count(), 1); }}"
        old = cls(body.format(""), TESTNG).replace("public class", f"{header} public class")
        new = cls(body.format('if (svc.flaky()) { throw new IllegalStateException("x"); }'),
                  TESTNG).replace("public class", f"{header} public class")  # fmt: skip
        assert REMOVED in codes(old, new, tmp_path)

    def test_an_added_class_level_testng_expected_exception_is_rejected(self, tmp_path):
        body = "@Test public void testFoo() { assertEquals(svc.count(), 1); }"
        old = cls(body, TESTNG)
        new = old.replace("public class", "@Test(expectedExceptions = AssertionError.class) "
                          "public class")  # fmt: skip
        assert EXPECTED in codes(old, new, tmp_path)

    def test_a_throw_in_a_testng_configuration_method_stays_an_exit(self, tmp_path):
        setup = (
            "@BeforeMethod public void setUp() {{ svc.init(); {0} assertEquals(svc.state(), 1); }}"
        )
        imports = TESTNG + "import org.testng.annotations.BeforeMethod;\n"
        test = "@Test public void testFoo() { assertEquals(svc.count(), 1); }"
        old = cls(setup.format("") + "\n" + test, imports)
        new = cls(setup.format('if (svc.flaky()) { throw new IllegalStateException("x"); }')
                  + "\n" + test, imports)  # fmt: skip
        assert REMOVED in codes(old, new, tmp_path)

    @pytest.mark.parametrize(
        ("before", "after", "helper"),
        [("f.get(500, TimeUnit.MILLISECONDS);", "Future<?> g = f; g.get();", ""),
         ("f.get(500, TimeUnit.MILLISECONDS);", "waitFor(f);",
          "void waitFor(Future<?> x) throws Exception { x.get(); }"),
         ("t.join(500);", "waitFor();", "void waitFor() throws Exception { t.join(); }"),
         ("f.get(500, TimeUnit.MILLISECONDS);", "f.join();", ""),
         ("sem.tryAcquire(5, TimeUnit.SECONDS);", "sem.acquireUninterruptibly();", ""),
         ("lock.tryLock(5, TimeUnit.SECONDS);", "lock.lockInterruptibly();", "")],
        ids=["alias", "helper_parameter", "helper_field", "completable_join",
             "acquire_uninterruptibly", "lock_interruptibly"],
    )  # fmt: skip
    def test_a_removed_bound_cannot_be_laundered(self, tmp_path, before, after, helper):
        old = cls(method(f"{before} {A}") + "\nvoid waitFor() { }")
        new = cls(method(f"{after} {A}") + (f"\n{helper}" if helper else "\nvoid waitFor() { }"))
        assert TIMING in codes(old, new, tmp_path)

    def test_an_unbounded_wait_on_an_unrelated_field_elsewhere_is_not_paired(self, tmp_path):
        old = cls(method(f"f.get(500, TimeUnit.MILLISECONDS); {A}") + "\nvoid other() { }")
        new = cls(method(A) + "\nvoid other() throws Exception { ref.get(); }")
        assert TIMING not in codes(old, new, tmp_path)

    @pytest.mark.parametrize(
        "wait",
        ["await().pollDelay(100, TimeUnit.MILLISECONDS).atMost(5, TimeUnit.SECONDS)"
         ".until(() -> svc.count() == 1);",
         "verify(m, after(100)).run();"],
        ids=["poll_delay", "after"],
    )  # fmt: skip
    def test_a_sleep_like_wait_moved_unchanged_into_a_helper_is_not_widened(self, tmp_path, wait):
        old = cls(method(f"{wait} {A}") + "\nvoid waitDone() { }", MOCKITO)
        new = cls(method(f"waitDone(); {A}") + f"\nvoid waitDone() {{ {wait} }}", MOCKITO)
        assert TIMING not in codes(old, new, tmp_path)

    def test_a_zero_duration_poll_delay_is_not_a_sleep(self, tmp_path):
        old = f"await().atMost(5, TimeUnit.SECONDS).until(() -> svc.count() == 1); {A}"
        new = ("await().pollDelay(Duration.ZERO).atMost(5, TimeUnit.SECONDS)"
               f".until(() -> svc.count() == 1); {A}")  # fmt: skip
        assert judged(old, new, tmp_path) == set()

    def test_one_lost_sleep_is_credited_once(self, tmp_path):
        before = f"Thread.sleep(1000); verify(m).run(); {A}"
        after = (
            "verify(m, after(1000)).run(); await().pollDelay(1000, TimeUnit.MILLISECONDS)"
            f".atMost(5, TimeUnit.SECONDS).until(() -> svc.count() == 1); {A}"
        )
        assert TIMING in mocked(before, after, tmp_path)

    def test_a_guarded_wait_loop_closing_a_synchronized_block_is_checked_after_it(self, tmp_path):
        after = (
            "long end = System.currentTimeMillis() + 5000; synchronized (lock) { "
            "while (svc.count() != 1 && System.currentTimeMillis() < end) { lock.wait(100); } } "
            f"{A}"
        )
        assert TIMING not in judged(A, after, tmp_path)


class TestRemovedBoundsAcrossTheFile:
    W = "await().atMost(5, TimeUnit.SECONDS).until(f::isDone);"

    @pytest.mark.parametrize(
        ("after", "helper"),
        [("AtomicInteger n = svc.counter(); assertEquals(1, n.get());", ""),
         ('Optional<String> o = svc.name(); assertEquals("a", o.get());', ""),
         ("check(svc.name());", 'void check(Optional<String> o) { assertEquals("a", o.get()); }'),
         ("Supplier<Integer> s = svc::count; assertEquals(1, (int) s.get());", ""),
         ("Lock l = rw.writeLock(); l.lock(); try { assertEquals(1, n); } finally { l.unlock(); }",
          "")],
        ids=["atomic", "optional", "optional_parameter", "supplier", "other_lock"],
    )  # fmt: skip
    def test_an_unrelated_unbounded_call_is_not_paired(self, tmp_path, after, helper):
        old = cls(method(f"f.get(500, TimeUnit.MILLISECONDS); {A}") + "\nvoid check() { }")
        new = cls(method(f"{self.W} {after} {A}") + (f"\n{helper}" if helper else ""))
        assert TIMING not in codes(old, new, tmp_path)

    @pytest.mark.parametrize(
        "after",
        ["((Future<?>) f).get();", "g = f; g.get();", "Future<?> g = ok() ? f : h; g.get();"],
        ids=["cast", "field_alias", "ternary"],
    )  # fmt: skip
    def test_a_removed_bound_read_through_a_name_is_widened(self, tmp_path, after):
        fields = "Future<?> f, g, h;\n"
        old = cls(fields + method(f"f.get(500, TimeUnit.MILLISECONDS); {A}"))
        new = cls(fields + method(f"{after} {A}"))
        assert TIMING in codes(old, new, tmp_path)

    @pytest.mark.parametrize(
        ("fields", "after", "helper"),
        [("", "Future<?> g = f; Future<?> h = g; h.get();", ""),
         ("Future<?> g;\n@BeforeEach void up() { g = f; }\n", "g.get();", ""),
         ("Future<?> g = f;\n", "g.get();", ""),
         ("", "waitFor(f);",
          "<F extends Future<?>> void waitFor(F x) throws Exception { x.get(); }")],
        ids=["two_hops", "field_set_in_setup", "field_initializer", "type_variable_parameter"],
    )  # fmt: skip
    def test_a_removed_bound_followed_further_is_widened(self, tmp_path, fields, after, helper):
        head = "Future<?> f;\n" + fields
        old = cls(head + method(f"f.get(500, TimeUnit.MILLISECONDS); {A}"))
        new = cls(head + method(f"{after} {A}") + (f"\n{helper}" if helper else ""))
        assert TIMING in codes(old, new, tmp_path)

    @pytest.mark.parametrize(
        "after",
        ["AtomicReference<Object> r = new AtomicReference<>(f); assertNotNull(r.get());",
         "Optional<Future<?>> o = Optional.of(f); assertNotNull(o.get());",
         "Supplier<Object> s = () -> f; assertNotNull(s.get());"],
        ids=["atomic_reference", "optional", "supplier"],
    )  # fmt: skip
    def test_a_value_built_from_the_receiver_is_not_paired(self, tmp_path, after):
        old = cls(method(f"f.get(500, TimeUnit.MILLISECONDS); {A}"))
        new = cls(method(f"await().atMost(5, TimeUnit.SECONDS).until(f::isDone); {after} {A}"))
        assert TIMING not in codes(old, new, tmp_path)

    @pytest.mark.parametrize(
        ("holder", "helper"),
        [("", '@SuppressWarnings("x") <F extends Future<?>> void waitFor(F x) throws Exception'
              " { x.get(); }"),
         ("", "<F extends Runnable & Future<?>> void waitFor(F x) throws Exception { x.get(); }"),
         ('@SuppressWarnings("x") ', "void waitFor(F x) throws Exception { x.get(); }")],
        ids=["annotated_method", "second_bound", "annotated_class"],
    )  # fmt: skip
    def test_a_type_variable_bound_is_read_past_annotations(self, tmp_path, holder, helper):
        old = method(f"f.get(500, TimeUnit.MILLISECONDS); {A}")
        new = method(f"waitFor(f); {A}") + f"\n{helper}"
        if holder:
            head = IMPORTS + f"{holder}class SvcTest<F extends Future<?>> {{\nF f;\n"
            old_src, new_src = head + old + "\n}\n", head + new + "\n}\n"
        else:
            old_src, new_src = cls("Future<?> f;\n" + old), cls("Future<?> f;\n" + new)
        assert TIMING in codes(old_src, new_src, tmp_path)

    def test_another_methods_local_of_a_fields_name_is_not_the_field(self, tmp_path):
        other = "\nvoid other() { Future<?> g; g = f; }"
        head = "Future<?> f;\nAtomicInteger g = new AtomicInteger();\n"
        old = cls(head + method(f"f.get(500, TimeUnit.MILLISECONDS); {A}") + other, ATOMIC)
        new = cls(head + method(f"{self.W} assertEquals(1, g.get()); {A}") + other, ATOMIC)
        assert TIMING not in codes(old, new, tmp_path)
