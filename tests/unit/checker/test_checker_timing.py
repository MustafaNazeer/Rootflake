from pathlib import Path

import pytest

from rootflake.gate.checker import timing
from rootflake.gate.checker.check import Target, check
from rootflake.gate.checker.timing import Bound, compare, duration_ms
from rootflake.gate.checker.values import fold

PATH = "src/test/java/a/FooTest.java"
TARGET = Target("a.FooTest", "testFoo")
TIMING = "weaken_assertion/timing_bound_widened"
IMPORTS = """import org.junit.jupiter.api.*;
import java.time.Duration;
import java.util.concurrent.*;
import java.util.concurrent.locks.LockSupport;
import static org.junit.jupiter.api.Assertions.*;
import static org.awaitility.Awaitility.*;
"""


def codes(before: str | None, after: str | None, tmp_path: Path, path: str = PATH) -> set[str]:
    pristine, workspace = tmp_path / "p", tmp_path / "w"
    for root, source in ((pristine, before), (workspace, after)):
        root.mkdir(parents=True)
        (root / "pom.xml").write_text("<project/>")  # a module, as the validator requires
        if source is not None:
            (root / path).parent.mkdir(parents=True)
            (root / path).write_text(source)
    return {f.code for f in check([path], pristine, workspace, TARGET).findings}


def cls(body: str, imports: str = IMPORTS, header: str = "") -> str:
    return f"package a;\n{imports}\n{header}\npublic class FooTest {{\n{body}\n}}\n"


def method(body: str, annotations: str = "@Test") -> str:
    return f"{annotations} void testFoo() throws Exception {{ {body} }}"


def judged(before_body: str, after_body: str, tmp_path: Path, **kw) -> set[str]:
    return codes(cls(method(before_body, **kw)), cls(method(after_body, **kw)), tmp_path)


class TestFolding:
    @pytest.mark.parametrize(
        ("text", "value"),
        [("60_000", 60000), ("10 * 1000", 10000), ("( long ) 5", 5), ("0x10", 16),
         ("TimeUnit . SECONDS . toMillis ( 10 )", 10000), ("SECONDS . toMillis ( 2 )", 2000),
         ("- 3 + 5", 2), ("100L * ( 2 + 1 )", 300), ("2.5d", 2.5)],
    )  # fmt: skip
    def test_constant_expressions_fold(self, text, value):
        assert fold(text) == value

    @pytest.mark.parametrize("text", ["x", "1 / 0", "LIMIT * 2", "f ( 1 )", "", "1 +"])
    def test_anything_else_does_not(self, text):
        assert fold(text) is None

    @pytest.mark.parametrize(
        ("text", "ms"),
        [("Duration . ofMillis ( 1100 )", 1100), ("java . time . Duration . ofSeconds ( 2 )", 2000),
         ("Duration . of ( 3 , ChronoUnit . SECONDS )", 3000),
         ("Duration . ofSeconds ( 1 , 500_000_000 )", 1500), ("Duration . ofMinutes ( 1 )", 60000),
         ("new Duration ( 5 , TimeUnit . SECONDS )", 5000)],
    )  # fmt: skip
    def test_durations_fold_to_milliseconds(self, text, ms):
        assert duration_ms(text) == (True, ms)

    def test_a_duration_of_an_unknown_value_is_still_a_duration(self):
        assert duration_ms("Duration . ofSeconds ( WAIT )") == (True, None)

    def test_other_text_is_not_a_duration(self):
        assert duration_ms("timeout ( 5 )") == (False, None)


def bounds(*values) -> list[Bound]:
    return [Bound(v, False, str(v), 1) if v is not None else Bound(None, False, "X", 1)
            for v in values]  # fmt: skip


class TestCompare:
    def test_a_raised_bound(self):
        raised, added = compare(bounds(5), bounds(50))
        assert [b.ms for b in raised] == [50] and added == []

    def test_a_raised_bound_cannot_hide_behind_an_added_smaller_one(self):
        raised, added = compare(bounds(5), bounds(1, 50))
        assert [b.ms for b in raised] == [50] and [b.ms for b in added] == [1]

    def test_one_of_two_raised_to_the_other(self):
        raised, _ = compare(bounds(1, 5), bounds(5, 5))
        assert [b.ms for b in raised] == [5]

    def test_a_lowered_bound(self):
        assert compare(bounds(5), bounds(3)) == ([], [])

    def test_an_added_bound_is_reported_as_added(self):
        raised, added = compare(bounds(5), bounds(5, 2))
        assert raised == [] and [b.ms for b in added] == [2]

    def test_a_changed_bound_that_does_not_fold_fails_closed(self):
        raised, _ = compare(bounds(5), bounds(None))
        assert len(raised) == 1


class TestTimeouts:
    def test_a_raised_jupiter_timeout(self, tmp_path):
        before = cls(method("f();", "@Test @Timeout(10)"))
        after = cls(method("f();", "@Test @Timeout(30)"))
        assert codes(before, after, tmp_path) == {TIMING}

    def test_a_lowered_jupiter_timeout(self, tmp_path):
        before = cls(method("f();", "@Test @Timeout(10)"))
        after = cls(method("f();", "@Test @Timeout(value = 5, unit = TimeUnit.SECONDS)"))
        assert codes(before, after, tmp_path) == set()

    def test_units_are_converted(self, tmp_path):
        before = cls(method("f();", "@Test @Timeout(10)"))
        same = cls(method("f();", "@Test @Timeout(value = 10000, unit = TimeUnit.MILLISECONDS)"))
        raised = cls(method("f();", "@Test @Timeout(value = 10, unit = TimeUnit.MINUTES)"))
        assert codes(before, same, tmp_path / "a") == set()
        assert codes(before, raised, tmp_path / "b") == {TIMING}

    def test_a_removed_timeout(self, tmp_path):
        before = cls(method("f();", "@Test @Timeout(10)"))
        after = cls(method("f();", "@Test"))
        assert codes(before, after, tmp_path) == {TIMING}

    def test_a_removed_class_timeout(self, tmp_path):
        before = cls(method("f();"), header="@Timeout(5)")
        after = cls(method("f();"))
        assert codes(before, after, tmp_path) == {TIMING}

    def test_an_added_timeout_tightens(self, tmp_path):
        before = cls(method("f();", "@Test"))
        after = cls(method("f();", "@Test @Timeout(10)"))
        assert codes(before, after, tmp_path) == set()

    def test_a_raised_junit4_test_timeout(self, tmp_path):
        imports = "import org.junit.Test;\nimport static org.junit.Assert.*;"
        before = cls(method("f();", "@Test(timeout = 60_000)"), imports)
        after = cls(method("f();", "@Test(timeout = 90_000)"), imports)
        assert codes(before, after, tmp_path) == {TIMING}

    @pytest.mark.parametrize("attribute", ["timeOut", "invocationTimeOut"])
    def test_a_raised_testng_timeout(self, tmp_path, attribute):
        imports = "import org.testng.annotations.Test;\nimport static org.testng.Assert.*;"
        before = cls(method("f();", f"@Test({attribute} = 100)"), imports)
        after = cls(method("f();", f"@Test({attribute} = 1000)"), imports)
        assert codes(before, after, tmp_path) == {TIMING}

    def test_a_raised_timeout_held_in_a_changed_constant_fails_closed(self, tmp_path):
        before = cls(method("f();", "@Test @Timeout(10)"))
        after = cls(method("f();", "@Test @Timeout(LIMIT)"))
        assert codes(before, after, tmp_path) == {TIMING}


RULE_IMPORTS = """import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.Timeout;
import java.util.concurrent.TimeUnit;
import static org.junit.Assert.*;
"""


class TestTimeoutRules:
    def rule(self, initializer: str | None) -> str:
        field = f"@Rule public Timeout timeout = {initializer};" if initializer else ""
        return cls(field + method("f();"), RULE_IMPORTS)

    def test_a_raised_rule(self, tmp_path):
        assert codes(
            self.rule("Timeout.seconds(5)"), self.rule("Timeout.seconds(10)"), tmp_path
        ) == {TIMING}

    @pytest.mark.parametrize(
        "after",
        ["new Timeout(5000)", "new Timeout(5, TimeUnit.SECONDS)", "Timeout.millis(5000)",
         "Timeout.builder().withTimeout(5, TimeUnit.SECONDS).build()"],
    )  # fmt: skip
    def test_the_same_rule_in_another_form(self, tmp_path, after):
        assert codes(self.rule("Timeout.seconds(5)"), self.rule(after), tmp_path) == set()

    def test_a_removed_rule(self, tmp_path):
        assert codes(self.rule("Timeout.seconds(5)"), self.rule(None), tmp_path) == {TIMING}


class TestAwaitility:
    def test_a_raised_at_most(self, tmp_path):
        assert judged(
            "await().atMost(5, TimeUnit.SECONDS).until(() -> ready());",
            "await().atMost(10, TimeUnit.SECONDS).until(() -> ready());",
            tmp_path,
        ) == {TIMING}

    @pytest.mark.parametrize("alias", ["timeout", "atMost"])
    def test_a_raised_duration_bound(self, tmp_path, alias):
        assert judged(
            f"await().{alias}(Duration.ofSeconds(5)).until(() -> ready());",
            f"await().{alias}(Duration.ofSeconds(6)).until(() -> ready());",
            tmp_path,
        ) == {TIMING}

    def test_a_raised_wait_at_most(self, tmp_path):
        assert judged(
            "waitAtMost(5, TimeUnit.SECONDS).until(() -> ready());",
            "waitAtMost(7, TimeUnit.SECONDS).until(() -> ready());",
            tmp_path,
        ) == {TIMING}

    # Changing the chain of a wait that holds a condition also changes the assertion it stands for,
    # which the assertion rules judge on their own, so these look only for the timing code.
    def test_a_removed_bound_below_the_default(self, tmp_path):
        assert TIMING in judged(
            "await().atMost(5, TimeUnit.SECONDS).until(() -> ready());",
            "await().until(() -> ready());",
            tmp_path,
        )

    def test_a_removed_bound_above_the_default(self, tmp_path):
        assert TIMING not in judged(
            "await().atMost(20, TimeUnit.SECONDS).until(() -> ready());",
            "await().until(() -> ready());",
            tmp_path,
        )

    def test_an_added_forever(self, tmp_path):
        assert TIMING in judged(
            "await().atMost(5, TimeUnit.SECONDS).until(() -> ready());",
            "await().forever().until(() -> ready());",
            tmp_path,
        )

    def test_a_new_wait_with_forever(self, tmp_path):
        assert judged("f();", "f(); await().forever().until(() -> ready());", tmp_path) == {TIMING}

    def test_an_added_default_timeout(self, tmp_path):
        assert judged(
            "f();", "Awaitility.setDefaultTimeout(Duration.ofMinutes(5)); f();", tmp_path
        ) == {TIMING}

    def test_a_raised_bound_behind_an_added_smaller_wait(self, tmp_path):
        assert judged(
            "await().atMost(5, TimeUnit.SECONDS).until(() -> ready());",
            "await().atMost(1, TimeUnit.SECONDS).until(() -> started());"
            "await().atMost(50, TimeUnit.SECONDS).until(() -> ready());",
            tmp_path,
        ) == {TIMING}

    def test_a_new_wait_is_held_to_the_wall_time_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(timing, "WALL_TIME_LIMIT_MS", 60_000.0)
        under = "f(); await().atMost(10, TimeUnit.SECONDS).until(() -> ready());"
        over = "f(); await().atMost(5, TimeUnit.MINUTES).until(() -> ready());"
        assert judged("f();", under, tmp_path / "a") == set()
        assert judged("f();", over, tmp_path / "b") == {TIMING}

    def test_a_new_wait_without_a_limit_set_is_checked_for_a_bound_only(self, tmp_path):
        assert judged("f();", "f(); await().atMost(5, TimeUnit.MINUTES).until(() -> ready());",
                      tmp_path) == set()  # fmt: skip

    def test_a_poll_delay_before_a_condition_that_is_always_true_is_a_sleep(self, tmp_path):
        assert judged(
            "f(); assertTrue(ready());",
            "f(); await().pollDelay(2, TimeUnit.SECONDS).until(() -> true); assertTrue(ready());",
            tmp_path,
        ) == {TIMING}

    def test_a_poll_delay_before_a_real_condition_is_not(self, tmp_path):
        assert (
            judged(
                "f(); assertTrue(ready());",
                "f(); await().pollDelay(100, TimeUnit.MILLISECONDS).until(() -> ready());"
                " assertTrue(ready());",
                tmp_path,
            )
            == set()
        )

    def test_an_await_on_a_latch_is_not_an_awaitility_root(self, tmp_path):
        assert (
            judged(
                "assertTrue(latch.await(1, TimeUnit.SECONDS));",
                "assertTrue(latch.await(1, TimeUnit.SECONDS));",
                tmp_path,
            )
            == set()
        )


class TestTimedCalls:
    def test_a_raised_latch_await(self, tmp_path):
        assert judged(
            "assertTrue(latch.await(1, TimeUnit.SECONDS));",
            "assertTrue(latch.await(5, TimeUnit.SECONDS));",
            tmp_path,
        ) == {TIMING}

    def test_a_raised_future_get(self, tmp_path):
        assert judged(
            "assertEquals(1, future.get(500, TimeUnit.MILLISECONDS));",
            "assertEquals(1, future.get(1, TimeUnit.SECONDS));",
            tmp_path,
        ) == {TIMING}

    def test_an_equal_bound_in_another_unit(self, tmp_path):
        assert (
            judged(
                "assertEquals(1, future.get(1000, TimeUnit.MILLISECONDS));",
                "assertEquals(1, future.get(1, TimeUnit.SECONDS));",
                tmp_path,
            )
            == set()
        )

    def test_a_raised_succeeds_within(self, tmp_path):
        assert judged(
            "assertThat(future).succeedsWithin(Duration.ofMillis(1100));",
            "assertThat(future).succeedsWithin(Duration.ofMillis(2000));",
            tmp_path,
        ) == {TIMING}

    def test_a_raised_literal_in_a_wait_helper(self, tmp_path):
        assert judged(
            "assertTrue(Wait.waitFor(() -> done(), 10_000, 200));",
            "assertTrue(Wait.waitFor(() -> done(), 20_000, 200));",
            tmp_path,
        ) == {TIMING}

    def test_a_lowered_literal_in_a_wait_helper(self, tmp_path):
        assert (
            judged(
                "assertTrue(Wait.waitFor(() -> done(), 10_000, 200));",
                "assertTrue(Wait.waitFor(() -> done(), 5_000, 200));",
                tmp_path,
            )
            == set()
        )

    def test_a_mockito_timeout_mode_is_left_to_the_assertion_rules(self, tmp_path):
        found = judged("verify(mock, timeout(100)).run();", "verify(mock, timeout(200)).run();",
                       tmp_path)  # fmt: skip
        assert TIMING not in found

    def test_a_tolerance_factory_is_left_to_the_assertion_rules(self, tmp_path):
        found = judged("assertThat(x()).isCloseTo(1.0, within(0.1));",
                       "assertThat(x()).isCloseTo(1.0, within(0.2));", tmp_path)  # fmt: skip
        assert TIMING not in found


class TestSleeps:
    @pytest.mark.parametrize(
        "sleep",
        ["Thread.sleep(50);", "TimeUnit.MILLISECONDS.sleep(10);",
         "Thread.currentThread().join(50);",
         "LockSupport.parkNanos(TimeUnit.MILLISECONDS.toNanos(50));",
         "Uninterruptibles.sleepUninterruptibly(2, TimeUnit.SECONDS);", "Jvm.pause(10);",
         "TestUtils.sleepQuietly(10);", "sleepAtLeast(10L);"],
    )  # fmt: skip
    def test_an_added_sleep(self, tmp_path, sleep):
        assert judged("f(); assertTrue(ready());", f"f(); {sleep} assertTrue(ready());",
                      tmp_path) == {TIMING}  # fmt: skip

    def test_an_added_sleep_in_a_lambda(self, tmp_path):
        assert judged(
            "run(() -> f()); assertTrue(ready());",
            "run(() -> { try { TimeUnit.MILLISECONDS.sleep(10); } catch (Exception e) {} f(); });"
            " assertTrue(ready());",
            tmp_path,
        ) == {TIMING}

    def test_an_unchanged_sleep(self, tmp_path):
        body = "f(); Thread.sleep(50); assertTrue(ready());"
        assert judged(body, body, tmp_path) == set()

    def test_a_statement_added_before_an_unchanged_sleep(self, tmp_path):
        before = "f(); Thread.sleep(50); assertTrue(ready());"
        after = "f(); g(); Thread.sleep(50); assertTrue(ready());"
        assert judged(before, after, tmp_path) == set()

    def test_an_unchanged_sleep_moved_before_an_assertion(self, tmp_path):
        assert judged(
            "Thread.sleep(50); f(); assertTrue(ready());",
            "f(); Thread.sleep(50); assertTrue(ready());",
            tmp_path,
        ) == {TIMING}

    def test_a_raised_sleep(self, tmp_path):
        before = "f(); Thread.sleep(50); assertTrue(ready());"
        after = "f(); Thread.sleep(500); assertTrue(ready());"
        assert judged(before, after, tmp_path) == {TIMING}

    def test_a_lowered_sleep(self, tmp_path):
        assert judged("f(); Thread.sleep(500); assertTrue(ready());",
                      "f(); Thread.sleep(50); assertTrue(ready());", tmp_path) == set()  # fmt: skip

    def test_a_sleep_in_an_added_after_each(self, tmp_path):
        before = cls(method("f();"))
        after = cls("@AfterEach void tearDown() throws Exception { Thread.sleep(2000); }"
                    + method("f();"))  # fmt: skip
        assert codes(before, after, tmp_path) == {TIMING}

    def test_an_existing_sleep_helper_newly_called(self, tmp_path):
        helper = "private static void pause() { try { Thread.sleep(100); } catch (Exception e) {} }"
        before = cls(helper + method("f(); assertTrue(ready());"))
        after = cls(helper + method("f(); pause(); assertTrue(ready());"))
        assert codes(before, after, tmp_path) == {TIMING}

    def test_a_helper_reaching_a_sleep_through_another_helper(self, tmp_path):
        helpers = ("private static void nap() throws Exception { Thread.sleep(100); }"
                   "private static void settle() throws Exception { g(); nap(); }")  # fmt: skip
        before = cls(helpers + method("f(); assertTrue(ready());"))
        after = cls(helpers + method("f(); settle(); assertTrue(ready());"))
        assert codes(before, after, tmp_path) == {TIMING}

    def test_a_new_call_to_a_helper_without_a_sleep(self, tmp_path):
        helper = "private static void prepare() { g(); }"
        before = cls(helper + method("f(); assertTrue(ready());"))
        after = cls(helper + method("f(); prepare(); assertTrue(ready());"))
        assert codes(before, after, tmp_path) == set()

    def test_an_unbounded_loop_around_a_sleep_is_a_fixed_sleep(self, tmp_path):
        before = "f(); assertTrue(ready());"
        after = "f(); while (!ready()) { Thread.sleep(10); } assertTrue(ready());"
        assert TIMING in judged(before, after, tmp_path)

    def test_string_join_is_not_a_sleep(self, tmp_path):
        assert judged('f(); assertEquals("a", g());',
                      'f(); String s = String.join(",", list); assertEquals("a", g());',
                      tmp_path) == set()  # fmt: skip

    def test_a_declared_sleep_named_method_is_followed_not_assumed(self, tmp_path):
        helper = "private static void sleepless() { g(); }"
        before = cls(helper + method("f(); assertTrue(ready());"))
        after = cls(helper + method("f(); sleepless(); assertTrue(ready());"))
        assert codes(before, after, tmp_path) == set()


class TestIgnoredWaits:
    def test_an_added_unchecked_latch_await(self, tmp_path):
        assert judged("f(); g();", "f(); latch.await(5, TimeUnit.SECONDS); g();", tmp_path) == {
            TIMING
        }

    def test_a_checked_latch_await(self, tmp_path):
        assert judged("f(); g();", "f(); assertTrue(latch.await(5, TimeUnit.SECONDS)); g();",
                      tmp_path) == set()  # fmt: skip

    def test_an_assigned_latch_await(self, tmp_path):
        assert (
            judged(
                "f(); g();",
                "f(); boolean done = latch.await(5, TimeUnit.SECONDS); assertTrue(done);",
                tmp_path,
            )
            == set()
        )

    def test_an_unchecked_await_termination_in_a_test(self, tmp_path):
        assert judged("f(); g();", "f(); pool.awaitTermination(5, TimeUnit.SECONDS); g();",
                      tmp_path) == {TIMING}  # fmt: skip

    def cleanup(self, body: str, annotation: str = "@AfterEach") -> str:
        return cls(f"{annotation} void cleanup() throws Exception {{ {body} }}" + method("f();"))

    def test_cleanup_after_each_is_exempt(self, tmp_path):
        before = cls(method("f();"))
        after = self.cleanup("pool.shutdownNow(); pool.awaitTermination(10, TimeUnit.SECONDS);")
        assert codes(before, after, tmp_path) == set()

    def test_cleanup_followed_by_an_assertion_is_not_exempt(self, tmp_path):
        before = cls(method("f();"))
        after = self.cleanup(
            "pool.shutdownNow(); pool.awaitTermination(10, TimeUnit.SECONDS); assertTrue(done());"
        )
        assert codes(before, after, tmp_path) == {TIMING}

    def test_a_wait_in_a_finally_block_is_exempt(self, tmp_path):
        assert (
            judged(
                "f();",
                "try { f(); } finally { pool.shutdown();"
                " pool.awaitTermination(1, TimeUnit.SECONDS); }",
                tmp_path,
            )
            == set()
        )

    def test_a_sleep_in_cleanup_is_never_exempt(self, tmp_path):
        before = cls(method("f();"))
        after = self.cleanup("pool.shutdownNow(); Thread.sleep(100);")
        assert codes(before, after, tmp_path) == {TIMING}


def test_production_code_is_not_judged_here(tmp_path):
    path = "src/main/java/a/Service.java"
    before = "package a;\npublic class Service { void run() { f(); } }\n"
    body = "void run() throws Exception { Thread.sleep(5); f(); }"
    after = f"package a;\npublic class Service {{ {body} }}\n"
    assert TIMING not in codes(before, after, tmp_path, path)
