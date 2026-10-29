import pytest
from test_checker_timing import TIMING, cls, codes, judged, method

from rootflake.gate.checker.flow import components, timing_name


def fields(before: str, after: str, body: str, tmp_path, extra: str = "") -> set[str]:
    return codes(
        cls(f"{before}\n{extra}\n{method(body)}"),
        cls(f"{after}\n{extra}\n{method(body)}"),
        tmp_path,
    )


class TestNames:
    @pytest.mark.parametrize(
        ("name", "parts"),
        [("maxTries", ["max", "tries"]), ("MAX_TRIES", ["max", "tries"]), ("TRIES", ["tries"]),
         ("HTTPTimeout", ["http", "timeout"]), ("entries", ["entries"])],
    )  # fmt: skip
    def test_names_split_at_underscores_and_case_changes(self, name, parts):
        assert components(name) == parts

    @pytest.mark.parametrize(
        "name",
        ["WAIT_TIME_SECONDS", "timeoutMs", "TRIES", "maxTries", "NUM_ATTEMPTS", "pollInterval"],
    )
    def test_timing_names(self, name):
        assert timing_name(name)

    @pytest.mark.parametrize("name", ["entries", "SCALE", "LIMIT", "count"])
    def test_other_names(self, name):
        assert not timing_name(name)


class TestRaisedConstants:
    def test_a_raised_constant_in_a_pair_is_widened(self, tmp_path):
        body = "latch.await(WAIT, TimeUnit.SECONDS);"
        extra = "CountDownLatch latch;"
        found = fields(
            "static final int WAIT = 5;", "static final int WAIT = 30;", body, tmp_path, extra
        )
        assert TIMING in found

    def test_a_lowered_constant_is_not(self, tmp_path):
        body = "latch.await(WAIT, TimeUnit.SECONDS);"
        extra = "CountDownLatch latch;"
        found = fields(
            "static final int WAIT = 30;", "static final int WAIT = 5;", body, tmp_path, extra
        )
        assert TIMING not in found

    def test_any_name_flowing_into_a_sleep_is_widened(self, tmp_path):
        body = "Thread.sleep(100 * SCALE);"
        found = fields("static final int SCALE = 1;", "static final int SCALE = 4;", body, tmp_path)
        assert TIMING in found

    def test_a_constant_read_only_by_an_assertion_is_left_to_decision_5(self, tmp_path):
        body = "assertEquals(SCALE, compute());"
        found = fields("static final int SCALE = 1;", "static final int SCALE = 4;", body, tmp_path)
        assert TIMING not in found

    def test_a_raised_local_flowing_into_a_sleep_is_widened(self, tmp_path):
        assert TIMING in judged("long pause = 10; Thread.sleep(pause);",
                                "long pause = 50; Thread.sleep(pause);", tmp_path)  # fmt: skip

    def test_a_value_followed_through_a_helper_parameter(self, tmp_path):
        helper = "static void nap(long ms) throws Exception { Thread.sleep(ms); }"
        found = fields("static final long SCALE = 2;", "static final long SCALE = 9;",
                       "nap(SCALE);", tmp_path, helper)  # fmt: skip
        assert TIMING in found

    def test_a_value_followed_out_of_a_method_through_its_return(self, tmp_path):
        helper = "static long session() { return (long) (MULTIPLE * 1000); }"
        body = "Thread.sleep(session());"
        found = fields("static final double MULTIPLE = .25;", "static final double MULTIPLE = 1.5;",
                       body, tmp_path, helper)  # fmt: skip
        assert TIMING in found

    def test_a_field_flowing_into_a_unit_conversion_is_widened(self, tmp_path):
        body = "long ms = timing.multiple(MULTIPLE).milliseconds(); use(ms);"
        extra = "Timing timing;"
        found = fields("static final double MULTIPLE = .25;", "static final double MULTIPLE = 1.5;",
                       body, tmp_path, extra)  # fmt: skip
        assert TIMING in found

    def test_a_local_with_another_name_in_a_conversion_is_not(self, tmp_path):
        before = "int n = 2; use(TimeUnit.SECONDS.toMillis(n));"
        assert TIMING not in judged(before, before.replace("2", "3"), tmp_path)

    def test_a_timing_name_bounding_a_loop_that_sleeps_is_widened(self, tmp_path):
        body = "for (int i = 0; i < TRIES; i++) { if (ready()) return; Thread.sleep(10); }"
        found = fields(
            "static final int TRIES = 5;", "static final int TRIES = 10;", body, tmp_path
        )
        assert TIMING in found

    def test_another_name_bounding_that_loop_is_widened_too(self, tmp_path):
        body = "for (int i = 0; i < COUNT; i++) { if (ready()) return; Thread.sleep(10); }"
        found = fields(
            "static final int COUNT = 5;", "static final int COUNT = 10;", body, tmp_path
        )
        assert TIMING in found

    def test_another_name_bounding_a_loop_that_does_not_wait_is_not(self, tmp_path):
        body = "for (int i = 0; i < COUNT; i++) { add(i); }"
        found = fields(
            "static final int COUNT = 5;", "static final int COUNT = 10;", body, tmp_path
        )
        assert TIMING not in found

    def test_a_timing_name_passed_to_a_wait_named_call_is_widened(self, tmp_path):
        body = "waitForServer(TIMEOUT_MS);"
        found = fields("static final int TIMEOUT_MS = 500;", "static final int TIMEOUT_MS = 5000;",
                       body, tmp_path)  # fmt: skip
        assert TIMING in found

    def test_a_changed_value_that_cannot_be_read_fails_closed(self, tmp_path):
        body = "latch.await(WAIT, TimeUnit.SECONDS);"
        after = 'static final int WAIT = Integer.getInteger("w", 5);'
        found = fields("static final int WAIT = 5;", after, body, tmp_path, "CountDownLatch latch;")
        assert TIMING in found

    def test_a_raised_duration_constant_is_widened(self, tmp_path):
        body = "await().atMost(LIMIT).until(() -> ready());"
        before = "static final Duration LIMIT = Duration.ofSeconds(5);"
        found = fields(before, before.replace("5", "30"), body, tmp_path)
        assert TIMING in found

    def test_a_lowered_duration_constant_is_not(self, tmp_path):
        body = "await().atMost(LIMIT).until(() -> ready());"
        before = "static final Duration LIMIT = Duration.ofSeconds(30);"
        found = fields(before, before.replace("30", "5"), body, tmp_path)
        assert TIMING not in found


class TestLoopBounds:
    def test_a_raised_literal_bounding_a_loop_that_sleeps_is_widened(self, tmp_path):
        before = "for (int i = 0; i < 1000; i++) { if (ready()) break; Thread.sleep(5); }"
        after = before.replace("1000", "5000")
        assert TIMING in judged(before, after, tmp_path)

    def test_a_lowered_one_is_not(self, tmp_path):
        before = "for (int i = 0; i < 5000; i++) { if (ready()) break; Thread.sleep(5); }"
        assert TIMING not in judged(before, before.replace("5000", "1000"), tmp_path)

    def test_a_loop_that_does_not_wait_is_not_judged(self, tmp_path):
        before = "for (int i = 0; i < 10; i++) { add(i); }"
        assert TIMING not in judged(before, before.replace("10", "20"), tmp_path)


class TestElapsedTime:
    def test_a_raised_bound_on_an_elapsed_time_is_widened(self, tmp_path):
        before = (
            "long start = System.currentTimeMillis(); run();"
            " assertTrue(System.currentTimeMillis() - start < 1000);"
        )
        assert TIMING in judged(before, before.replace("1000", "10000"), tmp_path)

    def test_an_elapsed_time_held_in_a_local_is_read(self, tmp_path):
        before = (
            "long start = System.nanoTime(); run();"
            " long took = System.nanoTime() - start; assertTrue(took < 50);"
        )
        assert TIMING in judged(before, before.replace("50", "500"), tmp_path)

    def test_a_lowered_bound_is_not(self, tmp_path):
        before = (
            "long start = System.currentTimeMillis(); run();"
            " assertTrue(System.currentTimeMillis() - start < 1000);"
        )
        assert TIMING not in judged(before, before.replace("1000", "500"), tmp_path)
