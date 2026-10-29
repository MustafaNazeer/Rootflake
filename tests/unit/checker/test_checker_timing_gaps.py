import pytest
from test_checker_timing import TIMING, cls, codes, judged, method

from rootflake.gate.checker.values import constant_lookup, fold

POLL = "while (!ready() && System.currentTimeMillis() < deadline) { Thread.sleep(50); }"


class TestExistingLoopBounds:
    def test_a_raised_deadline_local_with_any_name(self, tmp_path):
        before = f"long deadline = System.currentTimeMillis() + 5000; {POLL} assertTrue(ready());"
        assert TIMING in judged(before, before.replace("5000", "60000"), tmp_path)

    def test_a_raised_deadline_through_a_unit_conversion(self, tmp_path):
        before = (
            "long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);"
            " while (!ready() && System.nanoTime() < deadline) { Thread.sleep(50); }"
            " assertTrue(ready());"
        )
        assert TIMING in judged(before, before.replace("toNanos(5)", "toNanos(60)"), tmp_path)

    def test_a_lowered_deadline_is_not(self, tmp_path):
        before = f"long deadline = System.currentTimeMillis() + 5000; {POLL} assertTrue(ready());"
        assert TIMING not in judged(before, before.replace("5000", "1000"), tmp_path)

    def test_a_raised_literal_in_an_exit_condition(self, tmp_path):
        before = (
            "long start = System.nanoTime(); while (!ready()) {"
            ' if (System.nanoTime() > start + TimeUnit.SECONDS.toNanos(5)) fail("late");'
            " Thread.sleep(10); }"
        )
        assert TIMING in judged(before, before.replace("toNanos(5)", "toNanos(50)"), tmp_path)

    LOOP = "for (int i = 0; i < 10 && !ready(); i++) { Thread.sleep(50); } assertTrue(ready());"

    def test_a_bound_raised_by_a_product(self, tmp_path):
        assert TIMING in judged(self.LOOP, self.LOOP.replace("i < 10", "i < 10 * 100"), tmp_path)

    def test_a_bound_raised_with_another_comparison(self, tmp_path):
        assert TIMING in judged(self.LOOP, self.LOOP.replace("i < 10", "i <= 1000"), tmp_path)

    def test_a_bound_raised_beside_a_body_edit(self, tmp_path):
        after = self.LOOP.replace("i < 10", "i < 1000").replace("{ Thread", "{ log(); Thread")
        assert TIMING in judged(self.LOOP, after, tmp_path)

    def test_a_bound_moved_into_a_new_local(self, tmp_path):
        after = "int rounds = 1000; " + self.LOOP.replace("i < 10", "i < rounds")
        assert TIMING in judged(self.LOOP, after, tmp_path)

    def test_a_bound_moved_into_a_new_constant(self, tmp_path):
        before = cls(method(self.LOOP))
        after = cls("static final int ROUNDS = 1000;\n" + method(
            self.LOOP.replace("i < 10", "i < ROUNDS")))  # fmt: skip
        assert TIMING in codes(before, after, tmp_path)

    def test_a_lowered_bound_beside_a_body_edit_is_not(self, tmp_path):
        after = self.LOOP.replace("i < 10", "i < 5").replace("{ Thread", "{ log(); Thread")
        assert TIMING not in judged(self.LOOP, after, tmp_path)

    def test_a_condition_added_to_a_counted_loop_is_not(self, tmp_path):
        before = "for (int i = 0; i < 10; i++) { refresh(); Thread.sleep(50); }"
        before += " assertTrue(ready());"
        after = before.replace("i < 10;", "i < 10 && !ready();")
        assert TIMING not in judged(before, after, tmp_path)


class TestNestedTimeouts:
    def test_a_method_timeout_overrides_the_class_one(self, tmp_path):
        before = cls(method("f();"), header="@Timeout(5)")
        after = cls(method("f();", "@Test @Timeout(60)"), header="@Timeout(5)")
        assert codes(before, after, tmp_path) == {TIMING}

    def test_a_tighter_method_timeout_is_not_widened(self, tmp_path):
        before = cls(method("f();"), header="@Timeout(5)")
        after = cls(method("f();", "@Test @Timeout(2)"), header="@Timeout(5)")
        assert codes(before, after, tmp_path) == set()

    def test_removing_the_method_timeout_falls_back_to_the_class_one(self, tmp_path):
        before = cls(method("f();", "@Test @Timeout(2)"), header="@Timeout(5)")
        after = cls(method("f();"), header="@Timeout(5)")
        assert codes(before, after, tmp_path) == {TIMING}


class TestLatchResults:
    def test_an_assigned_result_never_checked(self, tmp_path):
        after = "f(); boolean done = latch.await(5, TimeUnit.SECONDS); g();"
        assert judged("f(); g();", after, tmp_path) == {TIMING}

    def test_an_assigned_result_only_logged(self, tmp_path):
        after = "f(); boolean done = latch.await(5, TimeUnit.SECONDS); System.out.println(done);"
        assert judged("f(); g();", after, tmp_path) == {TIMING}

    def test_an_empty_if(self, tmp_path):
        after = "f(); if (!latch.await(5, TimeUnit.SECONDS)) { } g();"
        assert judged("f(); g();", after, tmp_path) == {TIMING}

    def test_an_if_that_fails(self, tmp_path):
        after = 'f(); if (!latch.await(5, TimeUnit.SECONDS)) { fail("late"); } g();'
        assert judged("f(); g();", after, tmp_path) == set()

    def test_an_if_that_throws(self, tmp_path):
        after = "f(); if (!latch.await(5, TimeUnit.SECONDS)) throw new AssertionError(); g();"
        assert judged("f(); g();", after, tmp_path) == set()

    def test_an_assigned_result_checked_by_an_if(self, tmp_path):
        after = 'f(); boolean ok = latch.await(5, TimeUnit.SECONDS); if (!ok) fail("late");'
        assert judged("f(); g();", after, tmp_path) == set()

    def test_a_reassigned_result_never_checked(self, tmp_path):
        after = "f(); boolean ok; ok = latch.await(5, TimeUnit.SECONDS); g();"
        assert judged("f(); g();", after, tmp_path) == {TIMING}


class TestTestMethodsAreRun:
    def test_a_silent_loop_in_a_test_method_with_a_same_named_call(self, tmp_path):
        other = "void other(FooTest p) throws Exception { p.testFoo(); }"
        loop = "for (int i = 0; i < 100 && !ready(); i++) Thread.sleep(50); assertEquals(3, n());"
        before = cls(f"{other}\n{method('assertEquals(3, n());')}")
        after = cls(f"{other}\n{method(loop)}")
        assert TIMING in codes(before, after, tmp_path)


class TestClockForms:
    def test_a_statically_imported_clock(self, tmp_path):
        imports = "import static java.lang.System.currentTimeMillis;\n"
        after = (
            "f(); long until = currentTimeMillis() + 200;"
            " for (int i = 0; currentTimeMillis() < until; i++)"
            ' { if (i > 1000) fail("x"); Thread.sleep(10); }'
            " assertTrue(ready());"
        )
        from test_checker_timing import IMPORTS

        before = cls(method("f(); assertTrue(ready());"), IMPORTS + imports)
        assert TIMING in codes(before, cls(method(after), IMPORTS + imports), tmp_path)

    def test_a_fully_qualified_clock(self, tmp_path):
        after = (
            "f(); long until = java.lang.System.nanoTime() + 200;"
            " for (int i = 0; java.lang.System.nanoTime() < until; i++)"
            ' { if (i > 1000) fail("x"); Thread.sleep(10); }'
            " assertTrue(ready());"
        )
        assert TIMING in judged("f(); assertTrue(ready());", after, tmp_path)

    def test_a_clock_field(self, tmp_path):
        loop = (
            "f(); long until = systemClock.millis() + 200;"
            " for (int i = 0; systemClock.millis() < until; i++)"
            ' { if (i > 1000) fail("x"); Thread.sleep(10); }'
            " assertTrue(ready());"
        )
        before = cls(f"java.time.Clock systemClock;\n{method('f(); assertTrue(ready());')}")
        after = cls(f"java.time.Clock systemClock;\n{method(loop)}")
        assert TIMING in codes(before, after, tmp_path)


class TestCallbackFlags:
    def test_a_field_a_callback_sets_is_state(self, tmp_path):
        field = "volatile boolean done;"
        start = "client.start(() -> done = true);"
        before = cls(f"{field}\n{method(f'{start} Thread.sleep(500); assertTrue(done);')}")
        loop = (
            f"{start} long deadline = System.currentTimeMillis() + 5000;"
            " while (!done && System.currentTimeMillis() < deadline) { Thread.sleep(10); }"
            " assertTrue(done);"
        )
        assert TIMING not in codes(before, cls(f"{field}\n{method(loop)}"), tmp_path)

    def test_a_final_field_constant_is_not(self, tmp_path):
        field = "private final int tries = 10;"
        loop = "for (int i = 0; i < tries; i++) { Thread.sleep(10); } assertTrue(ready());"
        before = cls(f"{field}\n{method('assertTrue(ready());')}")
        assert TIMING in codes(before, cls(f"{field}\n{method(loop)}"), tmp_path)


class TestLoweredThroughConstants:
    def test_a_sleep_lowered_through_a_scale(self, tmp_path):
        field = "static final int SCALE = 1;"
        before = cls(f"{field}\n{method('f(); Thread.sleep(200); g();')}")
        after = cls(f"{field}\n{method('f(); Thread.sleep(100 * SCALE); g();')}")
        assert TIMING not in codes(before, after, tmp_path)

    def test_a_sleep_lowered_to_a_smaller_constant(self, tmp_path):
        field = "static final long SHORT = 50;"
        before = cls(f"{field}\n{method('f(); Thread.sleep(200); g();')}")
        after = cls(f"{field}\n{method('f(); Thread.sleep(SHORT); g();')}")
        assert TIMING not in codes(before, after, tmp_path)

    def test_a_sleep_raised_through_a_constant(self, tmp_path):
        field = "static final long LONG = 5000;"
        before = cls(f"{field}\n{method('f(); Thread.sleep(200); g();')}")
        after = cls(f"{field}\n{method('f(); Thread.sleep(LONG); g();')}")
        assert TIMING in codes(before, after, tmp_path)


class TestJavaFolding:
    @pytest.mark.parametrize(
        ("text", "value"),
        [("010", 8), ("0b101", 5), ("( int ) 2.7", 2), ("( long ) ( 0.25 * 1000 )", 250),
         ("( double ) 2.5", 2.5), ("10 / 2", 5), ("2.5 / 2", 1.25)],
    )  # fmt: skip
    def test_java_semantics(self, text, value):
        assert fold(text) == value

    def test_a_division_whose_types_decide_does_not_fold(self):
        assert fold("5 / 2") is None

    def test_a_doubling_chain_folds_quickly(self):
        constants = {f"C{k}": f"C{k - 1} + C{k - 1}" for k in range(1, 60)}
        constants["C0"] = "1"
        assert constant_lookup(constants)("C59") == 2.0**59


class TestRethrowsAreNotChecks:
    SLEEP = (
        "try { Thread.sleep(50); }"
        " catch (InterruptedException e) { throw new RuntimeException(e); }"
    )

    def test_a_rethrow_in_a_catch_does_not_check_the_wait(self, tmp_path):
        after = (
            "long deadline = System.currentTimeMillis() + 5000;"
            f" while (!ready() && System.currentTimeMillis() < deadline) {{ {self.SLEEP} }}"
            " assertEquals(3, n());"
        )
        assert TIMING in judged("assertEquals(3, n());", after, tmp_path)

    def test_a_throw_on_the_deadline_still_checks_it(self, tmp_path):
        after = (
            "long deadline = System.currentTimeMillis() + 5000; while (!ready()) {"
            ' if (System.currentTimeMillis() > deadline) throw new AssertionError("late");'
            f" {self.SLEEP} }} assertEquals(3, n());"
        )
        assert TIMING not in judged("assertEquals(3, n());", after, tmp_path)


class TestRulesTypedByInterface:
    IMPORTS = (
        "package a;\nimport org.junit.*;\nimport org.junit.rules.Timeout;\n"
        "import org.junit.rules.TestRule;\nimport static org.junit.Assert.*;\n"
    )

    def rule(self, kind: str, initializer: str) -> str:
        return cls(f"@Rule public {kind} timeout = {initializer};" + method("f();"), self.IMPORTS)

    def test_a_raised_rule_typed_test_rule(self, tmp_path):
        before, after = self.rule("TestRule", "Timeout.seconds(5)"), self.rule(
            "TestRule", "Timeout.seconds(60)")  # fmt: skip
        assert codes(before, after, tmp_path) == {TIMING}

    def test_retyping_the_same_rule_is_not(self, tmp_path):
        before = self.rule("Timeout", "Timeout.seconds(5)")
        after = self.rule("TestRule", "new Timeout(5000)")
        assert codes(before, after, tmp_path) == set()

    def test_another_test_rule_is_not_a_timeout(self, tmp_path):
        before = self.rule("TestRule", "new Other(5)")
        after = self.rule("TestRule", "new Other(60)")
        assert TIMING not in codes(before, after, tmp_path)


class TestValuesChangedAfterDeclaration:
    WAIT = "assertTrue(latch.await(t, TimeUnit.MILLISECONDS));"

    def test_a_compound_raise(self, tmp_path):
        before = f"long t = 5000; {self.WAIT}"
        after = f"long t = 5000; t *= 600; {self.WAIT}"
        assert TIMING in judged(before, after, tmp_path)

    def test_a_compound_lower_is_not(self, tmp_path):
        before = f"long t = 5000; {self.WAIT}"
        after = f"long t = 5000; t /= 2; {self.WAIT}"
        assert TIMING not in judged(before, after, tmp_path)

    def test_a_reassignment_raise(self, tmp_path):
        before = f"long t = 5000; f(); {self.WAIT}"
        after = f"long t = 5000; f(); t = 60000; {self.WAIT}"
        assert TIMING in judged(before, after, tmp_path)

    def test_a_reassignment_lower_is_not(self, tmp_path):
        before = f"long t = 5000; f(); {self.WAIT}"
        after = f"long t = 5000; f(); t = 1000; {self.WAIT}"
        assert TIMING not in judged(before, after, tmp_path)

    def test_an_unchanged_write_is_not(self, tmp_path):
        before = f"long t = 5000; t *= 2; f(); {self.WAIT}"
        assert TIMING not in judged(before, before.replace("f();", "g();"), tmp_path)

    def test_a_field_raised_in_setup(self, tmp_path):
        field = "long t = 5000;"
        wait = method(self.WAIT)
        setup = "@BeforeEach void setUp() { t = 60000; }"
        before = cls(f"{field}\n{wait}")
        after = cls(f"{field}\n{setup}\n{wait}")
        assert TIMING in codes(before, after, tmp_path)

    def test_a_local_that_shadows_a_field(self, tmp_path):
        field = "static final long TIMEOUT = 5000;"
        wait = "assertTrue(latch.await(TIMEOUT, TimeUnit.MILLISECONDS));"
        before = cls(f"{field}\n{method(wait)}")
        after = cls(f"{field}\n{method('long TIMEOUT = 60000; ' + wait)}")
        assert TIMING in codes(before, after, tmp_path)

    def test_a_new_local_written_twice_is_new_code(self, tmp_path):
        after = f"long t; t = 5000; {self.WAIT}"
        assert TIMING not in judged("assertTrue(ready());", after, tmp_path)


class TestWritesPairedWithThePristineWrite:
    WAIT = "assertTrue(latch.await(waitMs, TimeUnit.MILLISECONDS));"
    LATCH = "CountDownLatch latch = new CountDownLatch(1);\n"

    def pair(self, before_members: str, after_members: str, tmp_path, body: str = WAIT):
        before = cls(self.LATCH + before_members + "\n" + method(body))
        after = cls(self.LATCH + after_members + "\n" + method(body))
        return codes(before, after, tmp_path)

    def test_a_setup_write_raised_on_a_field_without_initializer(self, tmp_path):
        old = "long waitMs; @BeforeEach void up() { waitMs = 5000; }"
        assert TIMING in self.pair(old, old.replace("5000", "600000"), tmp_path)

    def test_a_setup_write_lowered_is_not(self, tmp_path):
        old = "long waitMs; @BeforeEach void up() { waitMs = 5000; }"
        assert TIMING not in self.pair(old, old.replace("5000", "100"), tmp_path)

    def test_a_write_through_this(self, tmp_path):
        old = "long waitMs = 5000; @BeforeEach void up() { }"
        new = "long waitMs = 5000; @BeforeEach void up() { this.waitMs = 600000; }"
        assert TIMING in self.pair(old, new, tmp_path)

    def test_a_constructor_write_raised(self, tmp_path):
        old = "long waitMs; FooTest() { waitMs = 5000; }"
        assert TIMING in self.pair(old, old.replace("5000", "600000"), tmp_path)

    def test_a_first_write_to_a_numeric_field_without_initializer(self, tmp_path):
        old = "long waitMs; @BeforeEach void up() { }"
        new = "long waitMs; @BeforeEach void up() { waitMs = 600000; }"
        assert TIMING in self.pair(old, new, tmp_path)

    def test_a_write_raised_under_a_larger_initializer(self, tmp_path):
        old = "long waitMs = 60000; @BeforeEach void up() { waitMs = 1000; }"
        assert TIMING in self.pair(old, old.replace("1000", "30000"), tmp_path)

    def test_a_split_local_raised(self, tmp_path):
        loop = (
            "long deadline; deadline = System.currentTimeMillis() + 5000; while (!ready()) {"
            ' if (System.currentTimeMillis() > deadline) fail("late"); Thread.sleep(10); }'
        )
        assert TIMING in judged(loop, loop.replace("5000", "600000"), tmp_path)

    def test_a_write_moved_to_another_member_unchanged_is_not(self, tmp_path):
        old = "volatile long waitMs = 0; void a() { waitMs = 5000; } void b() { }"
        new = "volatile long waitMs = 0; void a() { } void b() { waitMs = 5000; }"
        assert TIMING not in self.pair(old, new, tmp_path)

    def test_a_write_in_a_helper_whose_parameters_changed_is_not(self, tmp_path):
        old = "long waitMs = 1000; void configure() { waitMs = 2000; }"
        new = "long waitMs = 1000; void configure(int n) { waitMs = 2000; }"
        assert TIMING not in self.pair(old, new, tmp_path)


class TestStateWritesAreNotTimings:
    LOOP = "f(); while (!server.isStarted()) { Thread.sleep(10); } assertTrue(server.isStarted());"

    def test_a_server_set_up_again(self, tmp_path):
        before = cls("static Server server = null; @BeforeEach void up() { }\n" + method(self.LOOP))
        after = cls(
            "static Server server = null; @BeforeEach void up() { server = new Server(8080); }\n"
            + method(self.LOOP)
        )
        assert TIMING not in codes(before, after, tmp_path)

    def test_a_flag_set_in_a_callback(self, tmp_path):
        loop = "f(); while (!done) { Thread.sleep(10); } assertTrue(done);"
        before = cls("volatile boolean done = false; void finish() { }\n" + method(loop))
        after = cls(
            "volatile boolean done = false; void finish() { done = true; }\n" + method(loop)
        )
        assert TIMING not in codes(before, after, tmp_path)

    def test_a_counter_reset(self, tmp_path):
        loop = (
            "f(); while (counter.get() < 3) { Thread.sleep(10); } assertEquals(3, counter.get());"
        )
        members = "AtomicInteger counter = new AtomicInteger(); @BeforeEach void up() { %s }\n"
        before = cls(members % "" + method(loop))
        after = cls(members % "counter = new AtomicInteger(0);" + method(loop))
        assert TIMING not in codes(before, after, tmp_path)


class TestCompoundSteps:
    COUNTDOWN = (
        "long remaining = 5000; while (!ready() && remaining > 0) { Thread.sleep(100);"
        " remaining -= 100; } assertTrue(ready());"
    )

    def test_a_smaller_countdown_step_waits_longer(self, tmp_path):
        after = self.COUNTDOWN.replace("-= 100", "-= 1")
        assert TIMING in judged(self.COUNTDOWN, after, tmp_path)

    def test_a_larger_step_in_a_loop_bound_fails_closed(self, tmp_path):
        after = self.COUNTDOWN.replace("-= 100", "-= 200")
        assert TIMING in judged(self.COUNTDOWN, after, tmp_path)

    def test_a_lowered_floor(self, tmp_path):
        before = self.COUNTDOWN.replace("remaining > 0", "remaining > 4000")
        assert TIMING in judged(before, self.COUNTDOWN, tmp_path)

    def test_a_divisor_lowered(self, tmp_path):
        field = "static final long T = Config.TIMEOUT_MS / %s;\n"
        wait = method("assertTrue(latch.await(T, TimeUnit.MILLISECONDS));")
        assert TIMING in codes(cls(field % "10" + wait), cls(field % "1" + wait), tmp_path)


class TestBoundsInBranchesAndHelpers:
    def test_a_deadline_checked_in_an_else_branch(self, tmp_path):
        loop = (
            "long deadline = System.currentTimeMillis() + 5000; while (true) { if (ready()) break;"
            " if (System.currentTimeMillis() < deadline) { Thread.sleep(10); } else {"
            ' fail("late"); } }'
        )
        assert TIMING in judged(loop, loop.replace("5000", "600000"), tmp_path)

    def test_a_deadline_checked_in_a_helper(self, tmp_path):
        helper = (
            "void check(long d) { if (System.currentTimeMillis() > d)"
            ' throw new AssertionError("late"); }\n'
        )
        loop = (
            "long deadline = System.currentTimeMillis() + 5000;"
            " while (!ready()) { check(deadline); Thread.sleep(10); }"
        )
        before = cls(helper + method(loop))
        after = cls(helper + method(loop.replace("5000", "600000")))
        assert TIMING in codes(before, after, tmp_path)


class TestConditionsAdded:
    LOOP = "for (int i = 0; i < 10; i++) { Thread.sleep(50); } assertTrue(ready());"

    @pytest.mark.parametrize(
        "added", ["!server.isReady()", "!done.get()", "queue.size() < 3", "latch.getCount() > 0"]
    )
    def test_a_state_conjunct_is_not(self, tmp_path, added):
        after = self.LOOP.replace("i < 10;", f"i < 10 && {added};")
        assert TIMING not in judged(self.LOOP, after, tmp_path)

    def test_a_disjunct_is(self, tmp_path):
        after = self.LOOP.replace("i < 10;", "i < 10 || !server.isReady();")
        assert TIMING in judged(self.LOOP, after, tmp_path)

    def test_a_conjunct_beside_a_raised_bound_is(self, tmp_path):
        after = self.LOOP.replace("i < 10;", "i < 100 && !server.isReady();")
        assert TIMING in judged(self.LOOP, after, tmp_path)

    def test_a_conjunct_that_writes_is(self, tmp_path):
        after = self.LOOP.replace("i < 10;", "i < 10 && (n = poll()) == 0;")
        assert TIMING in judged(self.LOOP, after, tmp_path)


class TestHelperDropsAWaitResult:
    def test_a_new_call_of_a_helper_that_ignores_await(self, tmp_path):
        helper = (
            "CountDownLatch latch = new CountDownLatch(1);\n"
            "static void waitFor(CountDownLatch l) throws Exception"
            " { l.await(5, TimeUnit.SECONDS); }\n"
        )
        before = cls(helper + method("f(); Thread.sleep(2000); assertTrue(ready());"))
        after = cls(helper + method("f(); waitFor(latch); assertTrue(ready());"))
        assert TIMING in codes(before, after, tmp_path)

    def test_a_helper_that_returns_the_result_is_left_to_its_caller(self, tmp_path):
        helper = (
            "CountDownLatch latch = new CountDownLatch(1);\n"
            "static boolean waitFor(CountDownLatch l) throws Exception"
            " { return l.await(5, TimeUnit.SECONDS); }\n"
        )
        before = cls(helper + method("f(); Thread.sleep(2000); assertTrue(ready());"))
        after = cls(helper + method("f(); assertTrue(waitFor(latch)); assertTrue(ready());"))
        assert TIMING not in codes(before, after, tmp_path)


class TestRuleChainsAndRuleMethods:
    IMPORTS = TestRulesTypedByInterface.IMPORTS + "import org.junit.rules.RuleChain;\n"

    def test_a_rule_added_around_a_chained_timeout_is_not(self, tmp_path):
        before = cls(
            "@Rule public RuleChain chain = RuleChain.outerRule(new Timeout(5000));"
            + method("f();"), self.IMPORTS)  # fmt: skip
        after = cls(
            "@Rule public RuleChain chain = RuleChain.outerRule(new Timeout(5000))"
            ".around(cleanup);" + method("f();"), self.IMPORTS)  # fmt: skip
        assert TIMING not in codes(before, after, tmp_path)

    def test_a_raised_timeout_from_a_rule_method(self, tmp_path):
        rule = "@Rule public Timeout timeout() { return Timeout.seconds(%d); }\n"
        before = cls(rule % 5 + method("f();"), self.IMPORTS)
        after = cls(rule % 60 + method("f();"), self.IMPORTS)
        assert TIMING in codes(before, after, tmp_path)


def test_a_doubling_constant_chain_folds_quickly(tmp_path):
    chain = " ".join(f"static final long A{i} = A{i + 1} + A{i + 1};" for i in range(40))
    members = chain + " static final long A40 = 1;\n"
    before = cls(members + method("assertEquals(A0, f(), 1);"))
    after = cls(members + method("assertEquals(A0, g(), 1);"))
    codes(before, after, tmp_path)
