from pathlib import Path

from rootflake.gate.checker.check import check
from rootflake.gate.checker.production import (
    CLASS,
    DETECTION,
    OVERRIDE,
    RESOURCE,
    RETRY,
    SLEEP,
    SWALLOW,
    TIMING,
    ZONE,
)

MAIN = "src/main/java/a/Service.java"
TEST = "src/test/java/a/ServiceTest.java"


def checked(files: dict, tmp_path: Path, extra=(), overridden=()) -> list:
    pristine, workspace = tmp_path / "p", tmp_path / "w"
    for root in (pristine, workspace):
        root.mkdir(parents=True)
        (root / "pom.xml").write_text("<project/>")
    for path, (before, after) in files.items():
        for root, source in ((pristine, before), (workspace, after)):
            if source is not None:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text(source)
    paths = [*files, *extra]
    return list(check(paths, pristine, workspace, None, overridden).findings)


def codes(files: dict, tmp_path: Path, **kw) -> set[str]:
    return {f.code for f in checked(files, tmp_path, **kw)}


def service(body: str, imports: str = "") -> str:
    return f"package a;\n{imports}\npublic class Service {{\n{body}\n}}\n"


def production(before: str, after: str, tmp_path: Path, imports: str = "") -> set[str]:
    return codes({MAIN: (service(before, imports), service(after, imports))}, tmp_path)


TEST_IMPORTS = (
    "import org.junit.jupiter.api.*;\nimport static org.junit.jupiter.api.Assertions.*;\n"
)


def junit_file(body: str) -> str:
    return f"package a;\n{TEST_IMPORTS}\npublic class ServiceTest {{\n{body}\n}}\n"


class TestProductionRetry:
    CALL = "void run() { connect(); }"

    def test_an_added_loop_around_a_catch_that_logs(self, tmp_path):
        after = (
            "void run() { for (int i = 0; i < 3; i++) { try { connect(); return; }"
            ' catch (IOException e) { LOG.warn("retry", e); } } }'
        )
        assert production(self.CALL, after, tmp_path) == {RETRY}

    def test_an_added_loop_around_a_catch_that_rethrows_is_not(self, tmp_path):
        after = (
            "void run() { while (more()) { try { connect(); } catch (IOException e) {"
            " throw new UncheckedIOException(e); } } }"
        )
        assert RETRY not in production(self.CALL, after, tmp_path)

    def test_a_loop_the_pristine_method_had_is_not(self, tmp_path):
        before = (
            "void run() { for (int i = 0; i < 3; i++) { try { connect(); return; }"
            ' catch (IOException e) { LOG.warn("retry", e); } } }'
        )
        after = before.replace("connect();", "connect(1);")
        assert RETRY not in production(before, after, tmp_path)

    def test_a_self_rescheduling_method(self, tmp_path):
        after = (
            'void run() { try { connect(); } catch (Exception e) { LOG.warn("x", e); }'
            " finally { schedule(); } }\n"
            "void schedule() { executor.schedule(this::run, 1, TimeUnit.SECONDS); }"
        )
        assert production(self.CALL, after, tmp_path) == {RETRY}

    def test_a_method_that_calls_itself_from_its_catch(self, tmp_path):
        after = "void run() { try { connect(); } catch (IOException e) { run(); } }"
        assert RETRY in production(self.CALL, after, tmp_path)

    def test_a_task_that_hands_itself_on(self, tmp_path):
        after = (
            'public void run() { try { connect(); } catch (Exception e) { LOG.warn("x", e); }'
            " finally { executor.schedule(this, 1, TimeUnit.SECONDS); } }"
        )
        assert production("public void run() { connect(); }", after, tmp_path) == {RETRY}

    def test_a_task_that_hands_on_a_field_holding_itself(self, tmp_path):
        after = (
            "private final Runnable self = this;\n"
            'public void run() { try { connect(); } catch (Exception e) { LOG.warn("x", e); }'
            " finally { executor.schedule(self, 1, TimeUnit.SECONDS); } }"
        )
        before = "private final Runnable self = this;\npublic void run() { connect(); }"
        assert production(before, after, tmp_path) == {RETRY}

    def test_a_catch_that_passes_this_to_a_listener_is_a_call_not_a_task(self, tmp_path):
        after = (
            'void close() { try { connect(); } catch (Exception e) { LOG.warn("x", e); }'
            " listeners.remove(this); }"
        )
        assert RETRY not in production("void close() { connect(); }", after, tmp_path)

    def test_many_helpers_do_not_flag_an_unrelated_method(self, tmp_path):
        helpers = (
            "".join(f"void h{i}() {{ h{i + 1}(); }}\n" for i in range(300)) + "void h300() { }\n"
        )
        bodies = "".join(
            f'void m{i}() {{ try {{ h0(); }} catch (Exception e) {{ LOG.warn("x", e); }} }}\n'
            for i in range(12)
        )
        assert RETRY not in production(helpers, helpers + bodies, tmp_path)

    def test_a_rescheduling_method_that_already_was_one_is_not(self, tmp_path):
        before = (
            'void run() { try { connect(); } catch (Exception e) { LOG.warn("x", e); }'
            " finally { schedule(); } }\n"
            "void schedule() { executor.schedule(this::run, 1, TimeUnit.SECONDS); }"
        )
        after = before.replace("connect();", "connect(2);")
        assert RETRY not in production(before, after, tmp_path)

    def test_an_added_retry_library_use(self, tmp_path):
        imports = "import org.springframework.retry.support.RetryTemplate;\n"
        after = "void run() { RetryTemplate.builder().build().execute(c -> connect()); }"
        assert RETRY in production(self.CALL, after, tmp_path, imports)


class TestProductionTimingRaised:
    def test_a_raised_timeout_constant(self, tmp_path):
        before = "static final long CONNECT_TIMEOUT_MS = 1000;"
        assert production(before, before.replace("1000", "5000"), tmp_path) == {TIMING}

    def test_a_raised_duration_constant(self, tmp_path):
        before = "static final Duration RETRY_INTERVAL = Duration.ofSeconds(1);"
        after = before.replace("ofSeconds(1)", "ofSeconds(10)")
        assert production(before, after, tmp_path, "import java.time.Duration;\n") == {TIMING}

    def test_a_raised_retry_count(self, tmp_path):
        before = "private int maxAttempts = 3;"
        assert production(before, before.replace("3", "10"), tmp_path) == {TIMING}

    def test_a_lowered_timeout_is_not(self, tmp_path):
        before = "static final long CONNECT_TIMEOUT_MS = 5000;"
        assert TIMING not in production(before, before.replace("5000", "1000"), tmp_path)

    def test_a_new_constant_is_not(self, tmp_path):
        after = "static final long CONNECT_TIMEOUT_MS = 5000;"
        assert TIMING not in production("", after, tmp_path)

    def test_a_raised_constant_of_another_name_is_not(self, tmp_path):
        before = "static final int BUFFER_SIZE = 1024;"
        assert TIMING not in production(before, before.replace("1024", "4096"), tmp_path)


class TestProductionSleepAdded:
    def test_an_added_sleep(self, tmp_path):
        after = "void run() throws Exception { Thread.sleep(100); connect(); }"
        assert production("void run() { connect(); }", after, tmp_path) == {SLEEP}

    def test_a_sleep_moved_unchanged_into_a_new_method(self, tmp_path):
        before = "void run() throws Exception { while (!ready()) { Thread.sleep(100); } }"
        after = (
            "void run() throws Exception { waitReady(); }\n"
            "void waitReady() throws Exception { while (!ready()) { Thread.sleep(100); } }"
        )
        assert SLEEP in production(before, after, tmp_path)

    def test_a_sleep_left_where_it_was_is_not(self, tmp_path):
        before = "void run() throws Exception { Thread.sleep(100); connect(); }"
        after = before.replace("connect();", "connect(); close();")
        assert SLEEP not in production(before, after, tmp_path)

    def test_a_sleep_whose_duration_changed_in_place_pairs(self, tmp_path):
        before = "void run() throws Exception { Thread.sleep(100); connect(); }"
        assert SLEEP not in production(before, before.replace("100", "50"), tmp_path)

    BEFORE = service("void run() throws Exception { connect(); }")
    AFTER = service("void run() throws Exception { Thread.sleep(100); connect(); }")

    def test_a_sleep_an_unchanged_test_reaches_is_flagged(self, tmp_path):
        test = junit_file("@Test void testRun() throws Exception { new Service().run(); }")
        found = codes({MAIN: (self.BEFORE, self.AFTER), TEST: (test, test)}, tmp_path)
        assert found == {SLEEP}

    def test_a_sleep_a_rejected_test_call_reaches_is_left_to_the_test_rules(self, tmp_path):
        before = junit_file("@Test void testRun() throws Exception { check(); }")
        after = junit_file(
            "@Test void testRun() throws Exception { new Service().run(); check(); }"
        )
        found = codes({MAIN: (self.BEFORE, self.AFTER), TEST: (before, after)}, tmp_path)
        assert "weaken_assertion/timing_bound_widened" in found
        assert SLEEP not in found


class TestProductionSwallow:
    CALL = "void run() { connect(); }"

    def test_a_new_catch_that_drops_the_failure(self, tmp_path):
        after = "void run() { try { connect(); } catch (IOException e) { } }"
        assert production(self.CALL, after, tmp_path) == {SWALLOW}

    def test_a_new_catch_that_logs_is_not(self, tmp_path):
        after = 'void run() { try { connect(); } catch (IOException e) { log.error("x", e); } }'
        assert SWALLOW not in production(self.CALL, after, tmp_path)

    def test_a_new_catch_that_rethrows_is_not(self, tmp_path):
        after = (
            "void run() { try { connect(); } catch (IOException e) { cleanup();"
            " throw new IllegalStateException(e); } }"
        )
        assert SWALLOW not in production(self.CALL, after, tmp_path)

    def test_a_catch_the_pristine_method_had_is_not(self, tmp_path):
        before = "void run() { try { connect(); } catch (IOException e) { } }"
        after = before.replace("connect();", "connect(); close();")
        assert SWALLOW not in production(before, after, tmp_path)


class TestProductionTestDetection:
    def test_a_surefire_property_read(self, tmp_path):
        after = (
            'boolean testing() { return System.getProperty("surefire.test.class.path") != null; }'
        )
        assert production("", after, tmp_path) == {DETECTION}

    def test_a_stack_scan_for_junit(self, tmp_path):
        after = (
            "boolean testing() { for (StackTraceElement e : Thread.currentThread().getStackTrace())"
            ' { if (e.getClassName().startsWith("org.junit.")) return true; } return false; }'
        )
        assert DETECTION in production("", after, tmp_path)

    def test_a_signal_the_pristine_file_already_read_is_not(self, tmp_path):
        before = (
            'boolean testing() { return System.getProperty("surefire.test.class.path") != null; }'
        )
        after = before + "\nvoid other() { }"
        assert DETECTION not in production(before, after, tmp_path)

    def test_test_code_reading_the_signal_is_not(self, tmp_path):
        test = junit_file('@Test void testRun() { assertNull(System.getProperty("testng.x")); }')
        found = codes({TEST: (junit_file("@Test void testRun() { }"), test)}, tmp_path)
        assert DETECTION not in found


class TestNewProductionClass:
    def test_a_new_file_under_src_main_java(self, tmp_path):
        source = service("void run() { }")
        assert codes({MAIN: (None, source)}, tmp_path) == {CLASS}

    def test_a_new_nested_class(self, tmp_path):
        after = "void run() { }\nstatic class Helper { }"
        assert production("void run() { }", after, tmp_path) == {CLASS}

    def test_a_new_test_file_is_not(self, tmp_path):
        source = junit_file("@Test void testRun() { assertTrue(true == true); }")
        assert CLASS not in codes({TEST: (None, source)}, tmp_path)

    def test_a_new_file_in_a_test_support_module_is_not(self, tmp_path):
        path = "service-test/src/main/java/a/Support.java"
        source = "package a;\npublic class Support { void run() { } }\n"
        assert CLASS not in codes({path: (None, source)}, tmp_path)


class TestDefaultZoneOrLocaleSet:
    BEFORE = "@Test void testRun() { assertEquals(1, f()); }"

    def judged(self, after_body: str, tmp_path) -> set[str]:
        files = {TEST: (junit_file(self.BEFORE), junit_file(after_body))}
        return codes(files, tmp_path)

    def test_a_new_time_zone_default(self, tmp_path):
        after = (
            "@Test void testRun() { TimeZone old = TimeZone.getDefault();"
            ' TimeZone.setDefault(TimeZone.getTimeZone("UTC")); try { assertEquals(1, f()); }'
            " finally { TimeZone.setDefault(old); } }"
        )
        assert self.judged(after, tmp_path) == {ZONE}

    def test_a_new_locale_default(self, tmp_path):
        after = "@Test void testRun() { Locale.setDefault(Locale.ROOT); assertEquals(1, f()); }"
        assert self.judged(after, tmp_path) == {ZONE}

    def test_a_qualified_call(self, tmp_path):
        after = (
            "@Test void testRun() { java.util.Locale.setDefault(java.util.Locale.US);"
            " assertEquals(1, f()); }"
        )
        assert ZONE in self.judged(after, tmp_path)

    def test_a_time_zone_system_property(self, tmp_path):
        after = (
            '@Test void testRun() { System.setProperty("user.timezone", "UTC");'
            " assertEquals(1, f()); }"
        )
        assert self.judged(after, tmp_path) == {ZONE}

    def test_another_system_property_is_not(self, tmp_path):
        after = (
            '@Test void testRun() { System.setProperty("app.mode", "x"); assertEquals(1, f()); }'
        )
        assert ZONE not in self.judged(after, tmp_path)

    def test_a_default_the_test_already_set_is_not(self, tmp_path):
        before = "@Test void testRun() { Locale.setDefault(Locale.ROOT); assertEquals(1, f()); }"
        after = before.replace("assertEquals(1, f());", "assertEquals(1, f()); g();")
        assert ZONE not in codes({TEST: (junit_file(before), junit_file(after))}, tmp_path)

    def test_production_code_setting_it_is_not(self, tmp_path):
        after = "void run() { Locale.setDefault(Locale.ROOT); }"
        assert ZONE not in production("void run() { }", after, tmp_path)


class TestResourcesAndOverrides:
    def test_a_changed_test_resource_is_flagged_with_its_path(self, tmp_path):
        resource = "src/test/resources/a/config.properties"
        test = junit_file("@Test void testRun() { assertEquals(1, f()); }")
        found = checked({TEST: (test, test)}, tmp_path, extra=[resource])
        assert [(f.code, f.path) for f in found] == [(RESOURCE, resource)]

    def test_each_changed_resource_is_listed(self, tmp_path):
        extra = ["m/src/test/resources/a.yaml", "m/src/test/resources/b/c.yaml"]
        test = junit_file("@Test void testRun() { assertEquals(1, f()); }")
        found = checked({TEST: (test, test)}, tmp_path, extra=extra)
        assert sorted(f.path for f in found if f.code == RESOURCE) == extra

    def test_an_override_lists_the_paths_the_checker_could_not_read(self, tmp_path):
        resource = "src/test/resources/a/run.sh"
        test = junit_file("@Test void testRun() { assertEquals(1, f()); }")
        found = checked({TEST: (test, test)}, tmp_path, extra=[resource], overridden=[resource])
        assert (OVERRIDE, resource) in {(f.code, f.path) for f in found}

    def test_an_override_of_a_file_the_checker_read_is_still_flagged(self, tmp_path):
        test = junit_file("@Test void testRun() { assertEquals(1, f()); }")
        found = checked({TEST: (test, test)}, tmp_path, overridden=[TEST])
        assert [(f.code, f.path) for f in found] == [(OVERRIDE, None)]

    def test_a_reject_drops_the_flags(self, tmp_path):
        before = junit_file("@Test void testRun() { assertEquals(1, f()); }")
        after = junit_file("@Test void testRun() { f(); }")
        extra = ["src/test/resources/a.yaml"]
        found = codes({TEST: (before, after)}, tmp_path, extra=extra)
        assert RESOURCE not in found and found
