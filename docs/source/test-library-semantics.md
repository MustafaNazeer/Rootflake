# Test library semantics for the shortcut checker

Checked on 2026-10-14. This note records what the build plugins and test libraries that Rootflake's shortcut checker reasons about actually do, taken from each project's official documentation or from its source at a release tag. Every fact names the version it applies to. Anything I could not confirm from a primary source is marked **unverified**.

## Versions checked

| Library | Coordinates | Version checked | Latest on Maven Central on 2026-10-14 |
|---|---|---|---|
| Maven Surefire | `org.apache.maven.plugins:maven-surefire-plugin` | 3.6.0 | 3.6.0 (`lastUpdated` 20260903221843) |
| Maven Failsafe | `org.apache.maven.plugins:maven-failsafe-plugin` | 3.6.0 | 3.6.0 (`lastUpdated` 20260903221842) |
| JUnit Jupiter (JUnit 5 line) | `org.junit.jupiter:junit-jupiter-api` | 5.14.4 | 6.1.3 overall; 5.14.4 is the newest 5.x |
| JUnit 4 | `junit:junit` | 4.13.2 | 4.13.2 |
| TestNG | `org.testng:testng` | 7.12.0 | 7.12.0 |
| AssertJ | `org.assertj:assertj-core` | 3.27.7 | 4.0.0-M1 (a milestone); 3.27.7 is the newest final release |
| JSONAssert | `org.skyscreamer:jsonassert` | 1.5.3 | 2.0-rc1 (a release candidate); 1.5.3 is the newest final release |
| Awaitility | `org.awaitility:awaitility` | 4.3.0 | 4.3.0 |
| Mockito | `org.mockito:mockito-core` | 5.24.0 | 5.24.0 |

Versions came from each artifact's `maven-metadata.xml` on https://repo1.maven.org/maven2/. The sandbox image does not pin a Surefire or Failsafe version: `sandbox/Dockerfile` pins Maven 3.9.16 only, so each target project runs whatever plugin version its own POM resolves. The Surefire and Failsafe facts below are for 3.6.0 and **have not been checked against older plugin versions**, which is what most target projects will actually use.

## 1. Maven Surefire and Failsafe 3.6.0

### Sources

* Surefire `test` goal reference (shows "Version: 3.6.0", "Last Published: 2026-08-31"): https://maven.apache.org/surefire/maven-surefire-plugin/test-mojo.html
* Failsafe `integration-test` goal reference (3.6.0): https://maven.apache.org/surefire/maven-failsafe-plugin/integration-test-mojo.html
* Failsafe `verify` goal reference (3.6.0): https://maven.apache.org/surefire/maven-failsafe-plugin/verify-mojo.html
* Rerun failing tests guide (3.6.0): https://maven.apache.org/surefire/maven-surefire-plugin/examples/rerun-failing-tests.html
* What's new in 3.6.0: https://maven.apache.org/surefire/maven-surefire-plugin/whats-new-3-6-0.html
* XML reporter source at tag `surefire-3.6.0` (commit `0ff622b160253f362511a72d29a1f8067631f9d3`): https://github.com/apache/maven-surefire/blob/surefire-3.6.0/maven-surefire-common/src/main/java/org/apache/maven/plugin/surefire/report/ReportEntryType.java and https://github.com/apache/maven-surefire/blob/surefire-3.6.0/maven-surefire-common/src/main/java/org/apache/maven/plugin/surefire/report/StatelessXmlReporter.java

The maven.apache.org pages always serve the newest release; a versioned archive URL for 3.6.0 (`surefire-archives/surefire-3.6.0/...`) returned 404 when checked. The tagged source above is the stable reference.

### Parameters and user properties

POM parameter names are the `<configuration>` element names. "None" means the reference lists no user property.

| Parameter | Surefire user property | Failsafe user property | Default | What the reference says |
|---|---|---|---|---|
| `rerunFailingTestsCount` | `surefire.rerunFailingTestsCount` | `failsafe.rerunFailingTestsCount` | 0 | "The number of times each failing test will be rerun. If set larger than 0, rerun failing tests immediately after they fail. If a failing test passes in any of those reruns, it will be marked as pass and reported as a "flake". However, all the failing attempts will be recorded." |
| `failOnFlakeCount` | `surefire.failOnFlakeCount` | `failsafe.failOnFlakeCount` (on the `verify` goal) | 0 | Fails the test set when the cumulative number of flakes reaches the threshold; 0 allows unlimited flakes. Since 3.0.0-M6. |
| `failIfNoTests` | `failIfNoTests` | `failIfNoTests` | false | "Set this to "true" to cause a failure if there are no tests to run." |
| `failIfNoSpecifiedTests` | `surefire.failIfNoSpecifiedTests` | `failsafe.failIfNoSpecifiedTests` | true | Surefire: fail "if none of the tests specified in -Dtest=... are run". Failsafe uses `-Dit.test=...`. Failsafe also keeps a deprecated `failIfNoSpecifiedTestsDeprecated` bound to `it.failIfNoSpecifiedTests`. |
| `skipTests` | `skipTests` | None (deprecated in Failsafe; use `skipITs`) | false | Surefire: skip running tests "but still compile them". The 3.6.0 Failsafe reference says Failsafe "no longer binds its own skipTests parameter to the skipTests user property, so -DskipTests=true now skips only Surefire"; the `<skipTests>` element "is still honored when set explicitly in the plugin <configuration>". |
| `skip` | `maven.test.skip` | `maven.test.skip` | false | Bypasses tests entirely; "maven.test.skip disables both running the tests and compiling the tests". |
| `skipExec` (deprecated) | `maven.test.skip.exec` | `maven.test.skip.exec` | | "just like skipTests, but bound to the old property". |
| `skipITs` | not a Surefire parameter | `skipITs` | | Skip running integration tests; on `verify` it also skips verification. |
| `testFailureIgnore` | `maven.test.failure.ignore` | `maven.test.failure.ignore` (on `verify`) | false | "Set this to "true" to ignore a failure during testing." |
| `skipAfterFailureCount` | `surefire.skipAfterFailureCount` | `failsafe.skipAfterFailureCount` | 0 | Skips the remaining tests once the failure count is reached. |
| `excludes` | `surefire.excludes` | `failsafe.excludes` | `**/*$*` when neither `excludes` nor `test` is set | List of exclude patterns; supports `%regex[...]` and comma separated sub lists. |
| `excludesFile` | `surefire.excludesFile` | `failsafe.excludesFile` | | File of exclude patterns; since 3.0.0-M6 it accepts method filters such as `pkg.SomeTest#testMethod`. Patterns are appended to `excludes`. |
| `includesFile` | `surefire.includesFile` | `failsafe.includesFile` | | File of include patterns, appended to `includes`. |
| `test` | `test` | `it.test` | | Runs individual tests and overrides `includes` and `excludes`; patterns prefixed with `!` are excluded. |
| `groups` | `groups` | `groups` | | Only tests with one of these groups, categories, or tags run (TestNG, JUnit 4.8+ through the 4.7 provider, JUnit 5 since 2.22.0). |
| `excludedGroups` | `excludedGroups` | `excludedGroups` | | Tests with one of these groups, categories, or tags do not run. |
| `excludeJUnit5Engines` | `surefire.excludeJUnit5Engines` | `failsafe.excludeJUnit5Engines` | | Excludes JUnit Platform engines by ID. |

### Rerun results in the XML report

From the rerun guide and the 3.6.0 source:

* A test that fails and then passes on a rerun "will be counted as a flaky test. The build will be successful". The console summary gains a `Flakes:` count, for example `Tests run: 2, Failures: 0, Errors: 0, Skipped: 0, Flakes: 1`. The build fails on flakes only when `failOnFlakeCount` is set.
* In `TEST-*.xml`, a test that passed on a rerun carries one `flakyFailure` or `flakyError` element per failing attempt, each with its own `stackTrace`, `system-out`, and `system-err`, and **no** `failure` or `error` element. The top level `system-out` and `system-err` are those of the last, successful run, and the reported time is that of the last successful run.
* A test that fails every rerun keeps the usual `failure` or `error` element for the first run, plus one `rerunFailure` or `rerunError` element per later failing attempt. Its type (failure or error) "depends on its first failure", and its time is that of the first run.
* In the 3.6.0 source, `ReportEntryType` maps `ERROR` to `error`, `flakyError`, `rerunError` and `FAILURE` to `failure`, `flakyFailure`, `rerunFailure`. `StatelessXmlReporter` writes a `flakes` attribute on `testsuite` next to `tests`, `errors`, `skipped`, and `failures`.
* If a test passes on its first run, the report is identical to one written without `rerunFailingTestsCount`.
* The guide says reruns are supported for JUnit 4.12+, JUnit 5, and TestNG, and that since 3.6.0 "all test frameworks run via the unified JUnit Platform provider" and failed tests are rerun "after the initial test execution completes".

Consequence for Rootflake: a report reader that treats a `testcase` without `failure` or `error` as passed will count a test that failed and then passed on rerun as passed. Rerun settings must be rejected in a patch, and `flakyFailure`, `flakyError`, `rerunFailure`, and `rerunError` should be treated as evidence of a failing attempt.

### Provider change in 3.6.0

The What's New page says 3.6.0 removed the `surefire-junit3`, `surefire-junit4`, `surefire-junit47`, and `surefire-testng` providers; one provider, `surefire-junit-platform`, now runs JUnit 5 and 6 natively, JUnit 4 through the Vintage engine (minimum 4.12), and TestNG through the TestNG JUnit Platform engine (minimum 6.14.3).

## 2. JUnit Jupiter 5.14.4

### Sources

* User guide, single page export: https://docs.junit.org/5.14.4/_exports/junit-user-guide-5.14.4.html
* Repeated tests: https://docs.junit.org/5.14.4/writing-tests/repeated-tests.html
* Timeouts: https://docs.junit.org/5.14.4/writing-tests/timeouts.html
* Test execution order: https://docs.junit.org/5.14.4/writing-tests/test-execution-order.html
* Registering extensions: https://docs.junit.org/5.14.4/extensions/registering-extensions.html
* Exception handling extensions: https://docs.junit.org/5.14.4/extensions/exception-handling.html
* Configuration parameters: https://docs.junit.org/5.14.4/running-tests/configuration-parameters.html
* Javadoc: https://docs.junit.org/5.14.4/api/org.junit.jupiter.api/org/junit/jupiter/api/RepeatedTest.html, https://docs.junit.org/5.14.4/api/org.junit.jupiter.api/org/junit/jupiter/api/extension/TestExecutionExceptionHandler.html, https://docs.junit.org/5.14.4/api/org.junit.jupiter.api/org/junit/jupiter/api/Order.html

### `@RepeatedTest` and `failureThreshold`

* "Each invocation of a repeated test behaves like the execution of a regular @Test method with full support for the same lifecycle callbacks and extensions." Each repetition is reported on its own. In the guide's console output for `@RepeatedTest(value = 8, failureThreshold = 2)`, repetitions 2 and 4 are shown failed, repetitions 5 to 8 are shown skipped with "Failure threshold [2] exceeded", and the parent method node is shown with a check mark. So a failing repetition is reported as a failed test, while the repetitions after the threshold are skipped rather than failed.
* `failureThreshold` (since 5.10 per the guide): "the number of failures after which remaining repetitions will be automatically skipped". It defaults to `Integer.MAX_VALUE`, meaning every repetition runs regardless of failures. With parallel execution "no guarantees can be made regarding the failure threshold".
* How Surefire writes a repeated test into `TEST-*.xml` (one `testcase` per repetition, and how the skipped repetitions are counted) is **unverified**.

### `TestExecutionExceptionHandler` can swallow a failure

The Javadoc says implementations "must perform one of the following": swallow the throwable, "thereby preventing propagation"; rethrow it as is; or throw a new exception. Swallowing is a documented use: "Common use cases include swallowing an exception if it's anticipated". If a handler swallows the throwable, later handlers are not invoked. It applies to "the physical invocation of a @Test method"; failures in `@BeforeAll`, `@BeforeEach`, `@AfterEach`, and `@AfterAll` go to `LifecycleMethodExecutionExceptionHandler` instead. The guide's example `IgnoreIOExceptionExtension` returns normally for any `IOException`, which makes a test that throws one pass.

### Extension autodetection

* An extension is registered globally by putting its fully qualified class name in a file named `org.junit.jupiter.api.extension.Extension` under `/META-INF/services` in its enclosing JAR, discovered through Java's `ServiceLoader`.
* Autodetection "is not enabled by default". It is enabled by setting the configuration parameter `junit.jupiter.extensions.autodetection.enabled` to `true`, as a JVM system property, a launcher configuration parameter, or in the JUnit Platform configuration file. The 5.14.4 guide also documents `junit.jupiter.extensions.autodetection.include` and `junit.jupiter.extensions.autodetection.exclude` patterns.
* The configuration file is "a file named junit-platform.properties in the root of the class path" (for example `src/test/resources`). Lookup order: launcher parameters (Surefire's `configurationParameters` property), then custom configuration files, then JVM system properties, then `junit-platform.properties`.

So a patch can register a failure swallowing extension for every test without touching the test class, by adding a `META-INF/services/org.junit.jupiter.api.extension.Extension` file under test resources together with the autodetection setting.

### `@Timeout` and default timeout properties

* `@Timeout` makes a test, test factory, test template, or lifecycle method fail if it exceeds the duration; the unit defaults to seconds. A class level `@Timeout` applies to test, factory, and template methods in the class and its `@Nested` classes, not to lifecycle methods. On a `@RepeatedTest` or `@ParameterizedTest`, each invocation gets the timeout.
* Default timeouts, from general to specific: `junit.jupiter.execution.timeout.default`, `junit.jupiter.execution.timeout.testable.method.default`, `junit.jupiter.execution.timeout.test.method.default`, `junit.jupiter.execution.timeout.testtemplate.method.default`, `junit.jupiter.execution.timeout.testfactory.method.default`, `junit.jupiter.execution.timeout.lifecycle.method.default`, and the four per lifecycle method keys `junit.jupiter.execution.timeout.beforeall.method.default`, `...beforeeach...`, `...aftereach...`, `...afterall...`. More specific keys override less specific ones. Values use `<number> [ns|μs|ms|s|m|h|d]`; no unit means seconds.
* `junit.jupiter.execution.timeout.mode` takes `enabled` (the default), `disabled`, or `disabled_on_debug`. Setting it to `disabled` turns every `@Timeout` off.
* `junit.jupiter.execution.timeout.thread.mode.default` resolves the `INFERRED` thread mode; `SAME_THREAD` is the fallback.

### `@TestMethodOrder` and `@Order`

* By default methods run in an order that is "deterministic but intentionally nonobvious".
* `@TestMethodOrder` on a class or interface selects a `MethodOrderer`: `DisplayName`, `MethodName`, `OrderAnnotation`, `Random` (with a configurable seed), and the deprecated `Alphanumeric`. `junit.jupiter.testmethod.order.default` sets a default orderer for classes without `@TestMethodOrder`.
* With `MethodOrderer.OrderAnnotation`, methods are sorted numerically by `@Order`. A method without `@Order` gets `Order.DEFAULT`, "equal to the value of Integer.MAX_VALUE / 2".

JUnit 6.x (6.1.3 is the newest release) was not checked; these facts are for 5.14.4 only.

## 3. JUnit 4.13.2

### Sources

* https://junit.org/junit4/javadoc/4.13.2/org/junit/Assume.html
* https://junit.org/junit4/javadoc/4.13.2/org/junit/Test.html
* https://junit.org/junit4/javadoc/4.13.2/org/junit/rules/Timeout.html
* https://junit.org/junit4/javadoc/4.13.2/org/junit/FixMethodOrder.html
* https://junit.org/junit4/javadoc/4.13.2/org/junit/runners/MethodSorters.html

### Facts

* `Assume.assumeNoException(Throwable e)`: "Use to assume that an operation completes normally. If e is non-null, the test will halt and be ignored." The class Javadoc says "The default JUnit runner skips tests with failing assumptions. Custom runners may behave differently." Wrapping a failure in `assumeNoException` therefore turns it into a skip. That Surefire 3.6.0 (through the Vintage engine) reports such a test as `skipped` in `TEST-*.xml` is **unverified** here.
* `@Test(timeout=...)`: fails the test "if it takes longer than a specified amount of clock time (measured in milliseconds)". The Javadoc warns it "should not be considered deterministic", and that a method with a timeout runs "in a thread other than the thread which runs the fixture's @Before and @After methods".
* `org.junit.rules.Timeout` (since 4.7): applies one timeout to every test method in the class. "Each test is run in a new thread"; on expiry the test is interrupted with `Thread.interrupt()`. "A specified timeout of 0 will be interpreted as not set". The `Timeout(int millis)` constructor is deprecated; `Timeout.millis(...)`, `Timeout.seconds(...)`, `Timeout(long, TimeUnit)`, and `Timeout.builder()` remain.
* `@FixMethodOrder` chooses the method order with a `MethodSorters` value. `DEFAULT` is "deterministic, but not predictable"; `JVM` "Leaves the test methods in the order returned by the JVM" and that order "may vary from run to run"; `NAME_ASCENDING` sorts by method name in lexicographic order with `Method.toString()` as a tiebreaker.

## 4. TestNG 7.12.0

### Sources

* Documentation (footer "Last updated 2026-08-31 06:55:52 UTC"): https://testng.org/
* `@Test` source at tag `7.12.0` (commit `a21a584326ab0e3f3973cc1b5c1a4d1268f80d20`): https://github.com/testng-team/testng/blob/7.12.0/testng-core-api/src/main/java/org/testng/annotations/Test.java
* `ITestResult`: https://github.com/testng-team/testng/blob/7.12.0/testng-core-api/src/main/java/org/testng/ITestResult.java
* `IRetryAnalyzer`: https://github.com/testng-team/testng/blob/7.12.0/testng-core-api/src/main/java/org/testng/IRetryAnalyzer.java
* Success percentage logic: https://github.com/testng-team/testng/blob/7.12.0/testng-core/src/main/java/org/testng/internal/invokers/BaseInvoker.java#L104-L122
* Retry handling: https://github.com/testng-team/testng/blob/7.12.0/testng-core/src/main/java/org/testng/internal/invokers/TestInvoker.java#L552-L577

### Facts

* `invocationCount` (default 1) is "The number of times this method should be invoked." `successPercentage` (default 100) is "The percentage of success expected from this method."
* In `BaseInvoker.handleException`, a failing invocation gets status `SUCCESS_PERCENTAGE_FAILURE` instead of `FAILURE` when `failureCount < ((100 - successPercentage) * invocationCount) / 100f`. For a method without a data provider, `failureCount` is the number of earlier failures (counted from 0), so with `invocationCount = 10` and `successPercentage = 80` the first two failures are `SUCCESS_PERCENTAGE_FAILURE` and the third is `FAILURE`. TestNG reports these through `onTestFailedButWithinSuccessPercentage`, separate from `onTestFailure`. How Surefire 3.6.0 counts a `SUCCESS_PERCENTAGE_FAILURE` result is **unverified**.
* `skipFailedInvocations` (default false): when true and `invocationCount` is above 1, "all invocations after a failure will be marked as a SKIP instead of a FAIL".
* `retryAnalyzer` takes a class implementing `IRetryAnalyzer`, whose `retry(ITestResult)` "Returns true if the test method has to be retried". In `TestInvoker`, an attempt that will be retried has its status set to `SKIP` and `wasRetried` set to true; only the final attempt keeps its real status. So a test that fails and then passes on a retry ends with one or more skipped attempts and one pass. How Surefire records the retried attempts is **unverified**.
* `priority` (default 0): "The scheduling priority. Lower priorities will be scheduled first."
* `dependsOnMethods`: "you are guaranteed that all these methods will be run before the test method", there is "no guarantee on the order" among them, and "if any of these methods was not a SUCCESS, this test method will not be run and will be flagged as a SKIP". `alwaysRun = true` runs it even if a dependency failed; `ignoreMissingDependencies = true` runs it even if a dependency is missing or excluded.
* `enabled` (default true): "Whether methods on this class/method are enabled." `@Test(enabled = false)` disables the method. The documentation also describes `@Ignore`, which "has a higher priority than individual @Test method annotations" and can be placed on a class or a package.
* `expectedExceptions`: "If no exception or a different than one on this list is thrown, this test will be marked a failure." `expectedExceptionsMessageRegExp` (default `.*`) must match the message when `expectedExceptions` is set.

## 5. AssertJ 3.27.7

### Sources

Source at tag `assertj-build-3.27.7` (commit `e84071667f5f8f13084af9dfa54cee5fd9db18db`):

* https://github.com/assertj/assertj/blob/assertj-build-3.27.7/assertj-core/src/main/java/org/assertj/core/api/ObjectEnumerableAssert.java
* https://github.com/assertj/assertj/blob/assertj-build-3.27.7/assertj-core/src/main/java/org/assertj/core/api/AbstractDoubleAssert.java
* https://github.com/assertj/assertj/blob/assertj-build-3.27.7/assertj-core/src/main/java/org/assertj/core/api/Assertions.java
* https://github.com/assertj/assertj/blob/assertj-build-3.27.7/assertj-core/src/main/java/org/assertj/core/data/Offset.java

### Collection assertions

| Method | Order | Duplicates | Javadoc wording |
|---|---|---|---|
| `containsExactly` | must match | counted | "contains exactly the given values and nothing else, in order" |
| `containsExactlyInAnyOrder` | ignored | counted | "contains exactly the given values and nothing else, in any order"; the example fails when actual holds `vilya` twice and the expected values hold it once |
| `containsOnly` | ignored | **ignored** | "contains only the given values and nothing else, in any order and ignoring duplicates (i.e. once a value is found, its duplicates are also considered found)"; duplicates are ignored "on both actual and expected values" |

So `containsExactly` to `containsExactlyInAnyOrder` relaxes only order, while `containsExactly` or `containsExactlyInAnyOrder` to `containsOnly` also stops checking how many times each value appears. `containsOnly` throws `IllegalArgumentException` for an empty array of values.

### `isCloseTo`, `within`, `offset`, `byLessThan`

* `Assertions.within(Double)` is documented as an "Alias for offset(Double) to use with isCloseTo assertions"; both return `Offset.offset(value)`.
* `Offset.offset(value)` lets `isCloseTo` pass when `|actual - expected| <= offset value`. A negative value throws `IllegalArgumentException`; null throws `NullPointerException`.
* `byLessThan(Double)` builds a strict offset: `isCloseTo` "will fail when abs(actual - expected) == offset". The `isCloseTo` Javadoc calls this a "Breaking change since 2.9.0/3.9.0".
* Widening a tolerance is a change to the offset value passed to `isCloseTo`, `within`, `offset`, or `byLessThan`; the method names alone do not show it.

## 6. JSONAssert 1.5.3

### Sources

Source at tag `jsonassert-1.5.3` (commit `b1750da5b45924f65039a1b7c62d8123437fc64a`):

* https://github.com/skyscreamer/JSONassert/blob/jsonassert-1.5.3/src/main/java/org/skyscreamer/jsonassert/JSONCompareMode.java
* https://github.com/skyscreamer/JSONassert/blob/jsonassert-1.5.3/src/main/java/org/skyscreamer/jsonassert/JSONAssert.java

### Compare modes

Each mode combines two settings: extensible (extra fields in the actual JSON are allowed) and strict ordering (arrays must be in the same sequence).

| Mode | Extensible | Strict array order |
|---|---|---|
| `STRICT` | no | yes |
| `LENIENT` | yes | no |
| `NON_EXTENSIBLE` | no | no |
| `STRICT_ORDER` | yes | yes |

The Javadoc: when extensibility is not allowed "any additional fields will cause the test to fail"; when it is allowed "all values must still match". With strict ordering "JSON arrays must be in strict sequence". The boolean overload `assertEquals(expected, actual, boolean strict)` maps `true` to `STRICT` and `false` to `LENIENT`. So `STRICT` to `NON_EXTENSIBLE` relaxes only array order, while any move to `LENIENT` or `STRICT_ORDER` also accepts extra fields.

## 7. Awaitility 4.3.0

### Sources

Source at tag `awaitility-4.3.0` (commit `e3ff879b33c10261ae8d58e50ecf601b2037bd11`):

* https://github.com/awaitility/awaitility/blob/awaitility-4.3.0/awaitility/src/main/java/org/awaitility/Awaitility.java
* https://github.com/awaitility/awaitility/blob/awaitility-4.3.0/awaitility/src/main/java/org/awaitility/core/ConditionFactory.java
* https://github.com/awaitility/awaitility/blob/awaitility-4.3.0/awaitility/src/main/java/org/awaitility/Durations.java

### Facts

* `until(Callable<Boolean>)`, `until(Callable<T>, Matcher<? super T>)`, and `untilAsserted(ThrowingRunnable)` are each documented with `@throws org.awaitility.core.ConditionTimeoutException If condition was not fulfilled within the given time period.` `atMost(Duration)` means "Await at most timeout before throwing a timeout exception."
* The default wait constraint is `AtMostWaitConstraint.TEN_SECONDS` (`Awaitility.java`, `defaultWaitConstraint`), and `Durations.TEN_SECONDS` is `Duration.ofSeconds(10)`. The default poll interval is a fixed 100 milliseconds (`ONE_HUNDRED_MILLISECONDS`); the default poll delay is `null`.
* `Awaitility.setDefaultTimeout(Duration)` "Sets the default timeout that all await statements will use", so a single call can lengthen every wait in the JVM.
* `ignoreExceptions()` treats all exceptions during evaluation "as evaluating to false". The await still ends with `ConditionTimeoutException` if the condition never holds.
* The `untilAsserted` Javadoc warns that in Kotlin and Groovy a lambda that returns a value can be taken as a `Callable` instead of a `ThrowingRunnable`, and `await().untilAsserted { true == false }` "will pass". Rootflake's targets are Java projects, but a Kotlin or Groovy test source would need care.

## 8. Mockito 5.24.0

### Source

* https://github.com/mockito/mockito/blob/v5.24.0/mockito-core/src/main/java/org/mockito/Mockito.java (tag `v5.24.0`, commit `5a2f0f81cdafc9b34ddb4520db7f8866c787dac0`)

### Facts

* `verify(mock)` "Verifies certain behavior happened once" and is documented as equivalent to `verify(mock, times(1))`; the implementation is `return MOCKITO_CORE.verify(mock, times(1));` (lines 3163 to 3165).
* `times(n)`: "Allows verifying exact number of invocations."
* `never()`: "Verifies that interaction did not happen."
* `atLeastOnce()`: "Allows at-least-once verification." `atLeast(n)`: "Allows at-least-x verification."
* `atMostOnce()`: "Allows at-most-once verification." `atMost(n)`: "Allows at-most-x verification."
* `only()`: checks that the given method "was the only one invoked", equivalent to `verify(mock).someMethod()` followed by `verifyNoMoreInteractions(mock)`.

Replacing `verify(mock)` or `times(n)` with `atLeast`, `atLeastOnce`, `atMost`, or `atMostOnce` turns an exact count into a bound.

## Not verified

* Surefire and Failsafe parameter names, user properties, and XML element names in versions older than 3.6.0.
* How Surefire 3.6.0 records: each `@RepeatedTest` repetition and the repetitions skipped by `failureThreshold`; a JUnit 4 test skipped by a failed assumption; a TestNG `SUCCESS_PERCENTAGE_FAILURE` result; TestNG attempts marked `SKIP` because they were retried.
* JUnit 6.x, AssertJ 4.0.0-M1, and JSONAssert 2.0-rc1.
