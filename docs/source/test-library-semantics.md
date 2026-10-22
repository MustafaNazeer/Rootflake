# Test library semantics for the shortcut checker

Checked on 2026-10-14; extended on 2026-10-22 with JUnit Pioneer, rerunner-jupiter, the Lucene test framework, Hamcrest, the retry APIs of several client libraries, and further Surefire, JUnit, TestNG, AssertJ and Awaitility facts. This note records what the build plugins and test libraries that Rootflake's shortcut checker reasons about actually do, taken from each project's official documentation or from its source at a release tag. Every fact names the version it applies to. Where a source file is cited, line numbers refer to that file at the named tag. Anything I could not confirm from a primary source is marked **unverified**.

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

Added on 2026-10-22:

| Library | Coordinates | Version checked | Latest on Maven Central on 2026-10-22 |
|---|---|---|---|
| JUnit Pioneer | `org.junit-pioneer:junit-pioneer` | 2.3.0 | 2.3.0 |
| rerunner-jupiter | `io.github.artsok:rerunner-jupiter` | 2.1.5 (newest release tag); names compared with the 2.1.6 sources jar | 2.1.6, which has no tag in the repository |
| Lucene test framework | `org.apache.lucene:lucene-test-framework` | 10.5.2, with 9.12.0, 9.12.3, 9.1.0, 9.0.0 and 8.11.4 for `BadApple` | 10.5.2 |
| randomizedtesting runner (used by Lucene 10.5.2) | `com.carrotsearch.randomizedtesting:randomizedtesting-runner` | 2.8.1 | not checked |
| Hamcrest | `org.hamcrest:hamcrest` | 3.0, matching logic compared with 2.2 | 3.0 |
| Armeria | `com.linecorp.armeria:armeria` | 1.42.0 | 1.42.0 |
| Spring Retry | `org.springframework.retry:spring-retry` | 2.0.13 | 2.0.13 |
| Spring Framework | `org.springframework:spring-core` | 7.0.9 | 7.1.0-M2 (a milestone); 7.0.9 is the newest final release |
| resilience4j | `io.github.resilience4j:resilience4j-retry` | 2.4.0 | 2.4.0 |
| Failsafe | `dev.failsafe:failsafe` (3.x); `net.jodah:failsafe` (2.x) | 3.3.2; 2.4.4 for the old package | 3.3.2; 2.4.4 |
| OkHttp | `com.squareup.okhttp3:okhttp` | 5.5.1 | 5.5.1 |
| Apache HttpClient 5 | `org.apache.httpcomponents.client5:httpclient5` | 5.6.5 | 5.7-alpha1 (an alpha); 5.6.5 is the newest final release |
| Apache HttpClient 4 | `org.apache.httpcomponents:httpclient` | 4.5.14 | 4.5.14 |

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

### Default includes, `-Dtest` and groups, `enableAssertions` (added 2026-10-22)

Sources, all at tag `surefire-3.6.0` (commit `0ff622b160253f362511a72d29a1f8067631f9d3`):

* https://github.com/apache/maven-surefire/blob/surefire-3.6.0/maven-surefire-plugin/src/main/java/org/apache/maven/plugin/surefire/SurefireMojo.java
* https://github.com/apache/maven-surefire/blob/surefire-3.6.0/maven-failsafe-plugin/src/main/java/org/apache/maven/plugin/failsafe/IntegrationTestMojo.java
* https://github.com/apache/maven-surefire/blob/surefire-3.6.0/maven-surefire-common/src/main/java/org/apache/maven/plugin/surefire/AbstractSurefireMojo.java
* https://github.com/apache/maven-surefire/blob/surefire-3.6.0/surefire-providers/surefire-junit-platform/src/main/java/org/apache/maven/surefire/junitplatform/JUnitPlatformProvider.java
* The `test` goal reference above, which shows "Version: 3.6.0".

Facts:

* **Surefire default includes.** `SurefireMojo.getDefaultIncludes()` returns `**/Test*.java`, `**/*Test.java`, `**/*Tests.java` and `**/*TestCase.java` (lines 560 to 563). The `includes` Javadoc (lines 182 to 192) says these apply "When not specified and when the `test` parameter is not specified". A class named `FooIT` matches none of them.
* **Failsafe default includes.** `IntegrationTestMojo.getDefaultIncludes()` returns `**/IT*.java`, `**/*IT.java` and `**/*ITCase.java` (lines 613 to 616), documented at lines 228 to 236.
* **Default excludes.** `AbstractSurefireMojo.getDefaultExcludes()` returns `**/*$*` (lines 1032 to 1034), used only when no exclude is configured and `test` is not set (lines 2280 to 2297).
* **`-Dtest` overrides includes and excludes.** The `test` Javadoc says it runs individual tests "overriding the parameter `includes` and `excludes`" (`SurefireMojo.java` lines 69 to 75). In the code, a non blank `test` replaces the include list with its own patterns and empties the exclude list (`AbstractSurefireMojo.java` lines 2280 to 2283 and 2323 to 2324).
* **`-Dtest` does not override `groups` or `excludedGroups`.** Nothing in the `test` Javadoc mentions groups. `convertGroupParameters()` copies `groups` and `excludedGroups` into the provider properties whenever they are set, with no check of `test` (`AbstractSurefireMojo.java` lines 1588 to 1595); the JUnit Platform provider calls it unconditionally in `addProviderProperties()` (lines 3161 to 3166). In `JUnitPlatformProvider.newFilters()`, when neither Vintage nor TestNG is detected, `groups` becomes `TagFilter.includeTags` and `excludedGroups` becomes `TagFilter.excludeTags` (lines 520 to 524); for JUnit 4 through Vintage they become category filters (lines 525 to 541). These are added to the same filter list as the method filter built from `-Dtest` (lines 542 to 546). So, reading the 3.6.0 source, a test that the project's build excludes by tag or category stays excluded when it is named with `-Dtest`. I read this from the code and did not observe it in a run.
* **`enableAssertions` defaults to `true`.** `@Parameter(property = "enableAssertions", defaultValue = "true")`, documented as "By default, Surefire enables JVM assertions for the execution of your test cases. To disable the assertions, set this flag to "false"" (`AbstractSurefireMojo.java` lines 804 to 811; since 2.3.1). The `test` goal reference lists "Default : true" and the user property `enableAssertions`. So a Java `assert` statement in a test runs by default, and a POM or command line that sets the property to `false` turns every such statement off.

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

### Added 2026-10-22: sources

Source at tag `r5.14.4` (commit `096fd696c1b22780e025a02417d42903e0022862`):

* https://github.com/junit-team/junit-framework/blob/r5.14.4/junit-jupiter-api/src/main/java/org/junit/jupiter/api/Assumptions.java
* https://github.com/junit-team/junit-framework/blob/r5.14.4/junit-jupiter-api/src/main/java/org/junit/jupiter/api/Disabled.java
* https://github.com/junit-team/junit-framework/blob/r5.14.4/junit-jupiter-engine/src/main/java/org/junit/jupiter/engine/extension/DisabledCondition.java
* https://github.com/junit-team/junit-framework/blob/r5.14.4/junit-platform-commons/src/main/java/org/junit/platform/commons/support/AnnotationSupport.java
* https://github.com/junit-team/junit-framework/blob/r5.14.4/junit-jupiter-api/src/main/java/org/junit/jupiter/api/parallel/Execution.java, `ExecutionMode.java`, `Isolated.java`, `ResourceLock.java` and `ResourceLocks.java` in the same folder
* User guide pages: https://docs.junit.org/5.14.4/writing-tests/annotations.html, https://docs.junit.org/5.14.4/writing-tests/conditional-test-execution.html, https://docs.junit.org/5.14.4/writing-tests/parallel-execution.html (their sources are under `documentation/modules/ROOT/pages/writing-tests/` at the same tag)

### `Assumptions.abort` and `Assumptions.assumingThat`

* `abort()`, `abort(String)` and `abort(Supplier<String>)` are `@API(status = STABLE, since = "5.9")`, each documented `@throws TestAbortedException always`, and each body is a single `throw new TestAbortedException(...)` (`Assumptions.java` lines 275 to 277, 298 to 300, 314 to 316). They are generic (`<V> V`) so they can stand where a value is expected.
* The class Javadoc: "failed assumptions do not result in a test failure; rather, a failed assumption results in a test being aborted", but "failed assertions and other exceptions thrown by tests take precedence over failed assumptions" when both are thrown, and then the test is reported as failed (lines 29 to 35).
* `assumingThat(boolean, Executable)` and `assumingThat(BooleanSupplier, Executable)`: "Execute the supplied Executable, but only if the supplied assumption is valid. Unlike the other assumption methods, this method will not abort the test. If the assumption is invalid, this method does nothing. If the assumption is valid and the executable throws an exception, it will be treated like a regular test failure" (lines 215 to 258). So `assumingThat` never skips the test; when its condition is false, the assertions inside the executable silently do not run and the test can pass.

### `@Disabled` and the condition annotations as meta annotations

* `@Disabled` is `@Target({ ElementType.TYPE, ElementType.METHOD })` (`Disabled.java` line 61). In the Java SE 21 `ElementType` Javadoc, `TYPE` means "Class, interface (including annotation interface), enum, or record declaration" (https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/lang/annotation/ElementType.html), so `@Disabled` may be placed on an annotation type.
* `DisabledCondition.evaluateExecutionCondition` looks it up with `AnnotationSupport.findAnnotation(element, Disabled.class)` (`DisabledCondition.java` lines 40 to 45). That method finds the annotation when it is "directly present, meta-present, or indirectly present", and, for a class, it "will additionally search on interfaces implemented by the class" (`AnnotationSupport.java` lines 121 to 139). So in 5.14.4 `@Disabled` is honoured as a meta annotation, and also when it sits on an interface the test class implements.
* The user guide: "JUnit Jupiter annotations can be used as meta-annotations. That means that you can define your own composed annotation that will automatically inherit the semantics of its meta-annotations" (annotations page, source lines 133 to 137), and "any of the conditional annotations listed in the following sections may also be used as a meta-annotation in order to create a custom composed annotation" (conditional test execution page, source lines 28 to 34).
* `@Disabled` "is not @Inherited" (`Disabled.java` line 33): a subclass of a disabled class is not disabled by it.

### Parallel execution: `@Execution`, `@Isolated`, `@ResourceLock`

* Parallel execution is opt in: "By default, JUnit Jupiter tests are run sequentially in a single thread", and it is turned on with the configuration parameter `junit.jupiter.execution.parallel.enabled` set to `true` (parallel execution page, source lines 3 to 6). The three annotations below therefore change nothing unless that parameter is on.
* `@Execution` (package `org.junit.jupiter.api.parallel`, `@Target({ TYPE, METHOD })`, `@Inherited`) sets the execution mode of a class or method; `value()` is an `ExecutionMode` and `reason()` is informational only (`Execution.java` lines 26 to 117). Without it, `SAME_THREAD` is used unless `junit.jupiter.execution.parallel.mode.default` or `junit.jupiter.execution.parallel.mode.classes.default` says otherwise. `ExecutionMode.SAME_THREAD` means "Force execution in same thread as the parent node" and `CONCURRENT` "Allow concurrent execution with any other node" (`ExecutionMode.java` lines 27 to 39).
* `@Isolated` (`@Target(TYPE)`, `@Inherited`) declares "that the annotated test class should be executed in isolation from other test classes": "no other test class is executed concurrently". It is itself annotated `@ResourceLock(Resources.GLOBAL)` (`Isolated.java` lines 23 to 41).
* `@ResourceLock` (`@Target({ TYPE, METHOD })`, `@Inherited`, `@Repeatable(ResourceLocks.class)`) declares that a class or method "requires access to a shared resource identified by a key"; with the default mode `READ_WRITE`, the element runs "while no other test class or test method that uses the shared resource is being executed", and the lock covers `@BeforeEach` and `@AfterEach` (`ResourceLock.java` lines 26 to 111). Because it is repeatable, several locks on one element compile to the container annotation `@ResourceLocks`. Since 5.12 it also takes `providers` (classes implementing `ResourceLocksProvider`) and `target` (lines 113 to 136).

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
* `singleThreaded` (added 2026-10-22; `Test.java` lines 157 to 166, `@Test` is `@Target({METHOD, TYPE})` at line 13): "If set to true, all the methods on this test class are guaranteed to run in the same thread, even if the tests are currently being run with parallel="true". This attribute can only be used at the class level and will be ignored if used at the method level." Default `false`.

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

### `succeedsWithin` (added 2026-10-22)

Sources at the same tag: https://github.com/assertj/assertj/blob/assertj-build-3.27.7/assertj-core/src/main/java/org/assertj/core/api/AbstractFutureAssert.java and https://github.com/assertj/assertj/blob/assertj-build-3.27.7/assertj-core/src/main/java/org/assertj/core/api/AbstractCompletableFutureAssert.java

* `succeedsWithin` exists on both `AbstractFutureAssert` (for `Future`) and `AbstractCompletableFutureAssert` (for `CompletableFuture`), in four overloads on each: `succeedsWithin(Duration)`, `succeedsWithin(long, TimeUnit)`, and the same two with a trailing `InstanceOfAssertFactory` (`AbstractFutureAssert.java` lines 218, 264, 313, 363; `AbstractCompletableFutureAssert.java` lines 427, 470, 505, 536). On `Future` it is `@since 3.17.0` (line 216).
* Javadoc: it "Waits if necessary for at most the given time for this future to complete and then returns its result for further assertions. If the future's result is not available for any reason an assertion error is thrown", with `@throws AssertionError if the actual CompletableFuture does not succeed within the given timeout` (`AbstractFutureAssert.java` lines 175 to 215). So its timing argument is always a `Duration` or a number with a `TimeUnit`.
* It "does not fully integrate with soft assertions": a timeout fails the test at once instead of being collected (lines 180 to 186).
* The opposite assertion, `failsWithin(Duration)` and `failsWithin(long, TimeUnit)`, also exists on both classes (`AbstractFutureAssert.java` lines 404 and 444; `AbstractCompletableFutureAssert.java` lines 618 and 653).

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

### `waitAtMost`, `atMost` and the other ways to set the bound (added 2026-10-22)

* `Awaitility.waitAtMost(Duration)` and `Awaitility.waitAtMost(long, TimeUnit)` are static methods documented as "An alternative to using `await()` if you want to specify a timeout directly". Both build a `ConditionFactory` with `defaultWaitConstraint.withMaxWaitTime(...)` (`Awaitility.java` lines 365 to 390). So `waitAtMost(x)` sets the same bound as `await().atMost(x)`.
* On `ConditionFactory`, `atMost(Duration)` and `atMost(long, TimeUnit)` "Await at most timeout before throwing a timeout exception" (`ConditionFactory.java` lines 168 to 176 and line 338). `timeout(Duration)` and `timeout(long, TimeUnit)` carry the same Javadoc and simply call `atMost` (lines 158 to 166 and 290 to 299).
* `forever()` replaces the bound with `AtMostWaitConstraint.FOREVER`; its Javadoc: "Await forever until the condition is satisfied. Caution: You can block subsequent tests and the entire build can hang indefinitely, it's recommended to always use a timeout" (lines 259 to 269).
* `setDefaultTimeout` has two overloads, `setDefaultTimeout(long, TimeUnit)` (`Awaitility.java` lines 412 to 420) and `setDefaultTimeout(Duration)` (line 463).

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

## 9. JUnit Pioneer 2.3.0, `@RetryingTest`

### Sources

Source at tag `v2.3.0` (commit `3b2ae595df32269700bdb62a994bf03410ca9b9e`):

* https://github.com/junit-pioneer/junit-pioneer/blob/v2.3.0/src/main/java/org/junitpioneer/jupiter/RetryingTest.java
* https://github.com/junit-pioneer/junit-pioneer/blob/v2.3.0/src/main/java/org/junitpioneer/jupiter/RetryingTestExtension.java
* Documentation: https://junit-pioneer.org/docs/retrying-test/

### Facts

* The annotation is `org.junitpioneer.jupiter.RetryingTest`. It is `@Target({ ElementType.METHOD, ElementType.ANNOTATION_TYPE })`, so it can be used as a meta annotation, and it is itself annotated `@Execution(SAME_THREAD)`, `@ExtendWith(RetryingTestExtension.class)` and `@TestTemplate` (`RetryingTest.java` lines 70 to 77).
* Javadoc: it "retries a failing test a certain number of times before the test actually shows up as failing". With `@RetryingTest(n)` the method "is executed as long as it keeps failing, but no more than n times", and all executions except possibly the last "show up as being ignored/aborted"; "Only if all n executions fail, is the last one marked as such" (lines 25 to 38). A method so annotated "MUST NOT be annotated with @Test" (lines 49 to 50). Before 0.7.0 it was called `@RepeatFailedTest` (line 66).
* Attributes: `value` and its alias `maxAttempts` (the maximum number of executions), `minSuccess` (default 1), `suspendForMs` (default 0, a pause between executions), `onExceptions` (default empty, meaning retry on everything except `TestAbortedException`), and `name` (lines 118 to 163).
* Mechanism: `RetryingTestExtension` implements `TestTemplateInvocationContextProvider` and `TestExecutionExceptionHandler` (`RetryingTestExtension.java` line 39). A failed execution that will be retried is rethrown as a `TestAbortedException` (lines 156 to 165); only when no attempts remain is a `MultipleFailuresError` thrown (lines 166 to 175). So the earlier failing attempts are reported as aborted, not failed, and a later passing attempt is reported as passed.

## 10. rerunner-jupiter 2.1.5

### Sources

Source at tag `2.1.5` (commit `2cbf66808719749a664dfceab8f64f6a4ba2131a`; its `pom.xml` declares version 2.1.5). Maven Central also has 2.1.6, which has no tag in the repository; its sources jar contains the same two annotation files and the same two extension classes by name.

* https://github.com/artsok/rerunner-jupiter/blob/2.1.5/src/main/java/io/github/artsok/RepeatedIfExceptionsTest.java
* https://github.com/artsok/rerunner-jupiter/blob/2.1.5/src/main/java/io/github/artsok/ParameterizedRepeatedIfExceptionsTest.java
* https://github.com/artsok/rerunner-jupiter/blob/2.1.5/src/main/java/io/github/artsok/extension/RepeatIfExceptionsExtension.java
* https://github.com/artsok/rerunner-jupiter/blob/2.1.5/src/main/java/io/github/artsok/extension/ParameterizedRepeatedExtension.java

### Facts

* Both annotations are in package `io.github.artsok`: `@RepeatedIfExceptionsTest` is `@Target(ElementType.METHOD)`, `@TestTemplate`, `@ExtendWith(RepeatIfExceptionsExtension.class)` (`RepeatedIfExceptionsTest.java` lines 30 to 34); `@ParameterizedRepeatedIfExceptionsTest` is `@Target(ElementType.METHOD)`, `@TestTemplate`, `@ExtendWith(ParameterizedRepeatedExtension.class)` (`ParameterizedRepeatedIfExceptionsTest.java` lines 23 to 27). Neither targets `ANNOTATION_TYPE`.
* Attributes on both: `exceptions` (default `Throwable.class`, the "Pool of exceptions"), `repeats` (default 1, "N-times repeat test if it failed"), `minSuccess` (default 1), `suspend` (default 0, milliseconds of sleep between runs), plus display name attributes (`RepeatedIfExceptionsTest.java` lines 71 to 106; `ParameterizedRepeatedIfExceptionsTest.java` lines 97 to 141).
* Mechanism: both extensions implement `TestExecutionExceptionHandler`. When a listed exception is thrown and the success target can still be reached, the handler throws `new TestAbortedException("Do not fail completely, but repeat the test", throwable)` (`RepeatIfExceptionsExtension.java` lines 151 to 165; `ParameterizedRepeatedExtension.java` lines 131 to 136). Each extension also adds `TestAbortedException` to the repeatable exceptions (lines 114 and 112). So, as with JUnit Pioneer, a failing attempt is reported as aborted rather than failed.

## 11. Lucene test framework: `AwaitsFix`, `BadApple`, `Nightly`

### Sources

* Lucene 10.5.2, tag `releases/lucene/10.5.2` (commit `145d89cd30c38d25e39a0a2232cf00eee208b9cf`): https://github.com/apache/lucene/blob/releases/lucene/10.5.2/lucene/test-framework/src/java/org/apache/lucene/tests/util/LuceneTestCase.java
* Lucene 9.12.0, tag `releases/lucene/9.12.0` (commit `e913796758de3d9b9440669384b29bec07e6a5cd`): https://github.com/apache/lucene/blob/releases/lucene/9.12.0/lucene/test-framework/src/java/org/apache/lucene/tests/util/LuceneTestCase.java; the same file was also checked at 9.12.3 (commit `f965e930673c1f5cb478dc6a8907f5fc4ef7b539`), 9.1.0 (commit `5b522487ba8e0f1002b50a136817ca037aec9686`) and 9.0.0 (commit `0b18b3b965cedaf5eb129aa41243a44c83ca826d`, path `org/apache/lucene/util/`)
* Lucene 8.11.4, tag `releases/lucene-solr/8.11.4` (commit `e27f44e3d78dfcec230c97e0a1240e3751daeff9`): https://github.com/apache/lucene-solr/blob/releases/lucene-solr/8.11.4/lucene/test-framework/src/java/org/apache/lucene/util/LuceneTestCase.java
* randomizedtesting 2.8.1 (the version in Lucene 10.5.2's `versions.toml`), tag `release/2.8.1` (commit `70a38c719c9a65cde1ba6d48d1b8f4f18916b2f9`): https://github.com/randomizedtesting/randomizedtesting/blob/release/2.8.1/randomized-runner/src/main/java/com/carrotsearch/randomizedtesting/annotations/TestGroup.java and `GroupEvaluator.java` in the parent folder

### Facts

* **They are nested annotation types of `LuceneTestCase`**, not top level classes: `LuceneTestCase.Nightly`, `LuceneTestCase.AwaitsFix` and, where it exists, `LuceneTestCase.BadApple`. The package is `org.apache.lucene.tests.util` from 9.1.0 on (line 18 in 9.1.0, 9.12.0 and 10.5.2) and `org.apache.lucene.util` in 9.0.0 and in 8.11.4 (line 18). Code imports them either as `org.apache.lucene.tests.util.LuceneTestCase.AwaitsFix` or through the outer class, `@LuceneTestCase.AwaitsFix(bugUrl = ...)`, or by simple name inside a subclass of `LuceneTestCase`.
* **Each is a randomizedtesting test group.** Each is meta annotated `@TestGroup(enabled = ..., sysProperty = ...)`, plus `@Documented`, `@Inherited` and `@Retention(RUNTIME)`, with no `@Target`. `LuceneTestCase` runs with `@RunWith(RandomizedRunner.class)` (10.5.2 line 262). The `TestGroup` Javadoc: "A group can be enabled or disabled using boolean system properties ... A test case is executed if it has no groups or if all of its groups are enabled", and `enabled()` says whether "the group enabled or disabled by default" (randomizedtesting `TestGroup.java` lines 15 to 49). `GroupEvaluator.getIgnoreReason` returns a reason when a group is disabled (lines 104 to 147), so a test in a disabled group is ignored, not run.
* **`@AwaitsFix` is off by default.** 10.5.2: `@TestGroup(enabled = false, sysProperty = SYSPROP_AWAITSFIX)` with `SYSPROP_AWAITSFIX = "tests.awaitsfix"`, Javadoc "Annotation for tests which exhibit a known issue and are temporarily disabled", required attribute `bugUrl` (lines 288 and 323 to 331). The test runs only with `-Dtests.awaitsfix=true`.
* **`@Nightly` is off by default.** 10.5.2: `@TestGroup(enabled = false, sysProperty = SYSPROP_NIGHTLY)` with `SYSPROP_NIGHTLY = "tests.nightly"`, Javadoc "Annotation for tests that should only be run during nightly builds" (lines 285 and 300 to 305). `TEST_NIGHTLY` reads the property with that default (lines 475 to 477). The test runs only with `-Dtests.nightly=true`. randomizedtesting also ships its own `com.carrotsearch.randomizedtesting.annotations.Nightly`, also `@TestGroup(enabled = false)`.
* **`@BadApple` is on by default where it exists, and it no longer exists in Lucene 10.** In 8.11.4 (lines 308 to 309), 9.0.0 (lines 285 to 286), 9.1.0 (lines 305 to 306), 9.12.0 and 9.12.3 (lines 346 to 347) it is `@TestGroup(enabled = true, sysProperty = SYSPROP_BADAPPLES)` with `SYSPROP_BADAPPLES = "tests.badapples"`. The 9.12.0 Javadoc: "Annotation for tests that fail frequently and are not executed in Jenkins builds to not spam mailing lists with false reports. Tests are turned on for developers by default. If you want to disable them, set: -Dtests.badapples=false" (lines 331 to 350). So adding `@BadApple` does not skip the test in a default run; it marks it so that a build passing `-Dtests.badapples=false` skips it. `LuceneTestCase.java` at 10.0.0 (commit `eadc07cc6a17f9cfea81f3e82ec06164014232fb`) and at 10.5.2 declares no `BadApple` and no `SYSPROP_BADAPPLES`.

## 12. Hamcrest 3.0, `contains` and `containsInAnyOrder`

### Sources

Source at tag `v3.0` (commit `68984b85e869df6a888fffcad87e4b676a8fc0ac`):

* https://github.com/hamcrest/JavaHamcrest/blob/v3.0/hamcrest/src/main/java/org/hamcrest/collection/IsIterableContainingInOrder.java
* https://github.com/hamcrest/JavaHamcrest/blob/v3.0/hamcrest/src/main/java/org/hamcrest/collection/IsIterableContainingInAnyOrder.java

The matching code of `IsIterableContainingInAnyOrder` was compared with tag `v2.2` (commit `60454d3c3e2eac8f3049dfbef2900a989a8b8624`) and is the same.

### Facts

* `contains(E... items)`: "Creates a matcher for Iterables that matches when a single pass over the examined Iterable yields a series of items, each logically equal to the corresponding item in the specified items. For a positive match, the examined iterable must be of the same length as the number of specified items" (`IsIterableContainingInOrder.java` lines 85 to 100). Order and length both count.
* `containsInAnyOrder(T... items)`: "Creates an order agnostic matcher for Iterables that matches when a single pass over the examined Iterable yields a series of items, each logically equal to one item anywhere in the specified items. For a positive match, the examined iterable must be of the same length as the number of specified items. N.B. each of the specified items will only be used once during a given examination" (`IsIterableContainingInAnyOrder.java` lines 108 to 132). The matcher overloads, `containsInAnyOrder(Matcher...)` and `containsInAnyOrder(Collection<Matcher>)`, carry the same wording (lines 80 to 104 and 162).
* **Duplicates are counted.** The implementation copies the expected matchers into a list, and for each actual item removes the first matcher that matches it; an item with no matcher left fails, and the match succeeds only if the list is empty at the end (lines 22 to 78). With plain values (each wrapped in `equalTo`), `containsInAnyOrder` therefore passes exactly when the actual and expected values are equal as multisets: `[a, a, b]` does not match `containsInAnyOrder(a, b, b)`. So `contains` to `containsInAnyOrder` on the same values relaxes only order.
* The matching is greedy: with matchers that overlap (for example `startsWith("a")` and `equalTo("ab")`), an assignment that exists can be missed and the assertion can fail. This can only make the assertion fail where it could pass, never pass where it should fail, since every item still consumes exactly one matcher and the counts must be equal.

## 13. Retry APIs in client libraries

Each class or method below was confirmed in the library's source at the named tag. The checker matches them by name, so the packages are recorded to show which simple names are shared.

### Armeria 1.42.0

Tag `armeria-1.42.0` (commit `f61e15c7e1e4cc586dbb1983ff8e210dfb53c51f`), folder https://github.com/line/armeria/tree/armeria-1.42.0/core/src/main/java/com/linecorp/armeria/client/retry

* `com.linecorp.armeria.client.retry.RetryingClient`, `public final class`, "An HttpClient decorator that handles failures of an invocation and retries HTTP requests" (`RetryingClient.java` lines 60 to 63).
* `com.linecorp.armeria.client.retry.RetryRule`, `public interface` (`RetryRule.java` line 48).
* The same package also holds `RetryingClientBuilder`, `RetryingRpcClient`, `RetryingRpcClientBuilder`, `RetryRuleWithContent`, `RetryRuleBuilder`, `RetryConfig` and `Backoff`.

### Spring Retry 2.0.13 and Spring Framework 7.0.9

Spring Retry, tag `v2.0.13` (commit `f1012127f6084800ef5d3b8f8f2bc3b51c53997a`), https://github.com/spring-projects/spring-retry/tree/v2.0.13/src/main/java/org/springframework/retry

* `org.springframework.retry.support.RetryTemplate`, `public class RetryTemplate implements RetryOperations` (`RetryTemplate.java` line 83).
* `org.springframework.retry.annotation.Retryable`, an annotation with `@Target({ ElementType.METHOD, ElementType.TYPE })` (`annotation/Retryable.java` lines 38 to 41), enabled by `@EnableRetry` in the same package.
* A second, unrelated `Retryable` exists: the interface `org.springframework.retry.interceptor.Retryable` (`interceptor/Retryable.java` line 27).
* The project README opens with "Project Status: Maintenance Only. This project has been superseded by Spring Framework 7 and is no longer accepting enhancements."

Spring Framework, tag `v7.0.9` (commit `82a6b40b9366ec181ededdad282b307ee5381a52`):

* `org.springframework.core.retry.RetryTemplate`, `public class RetryTemplate implements RetryOperations`, `@since 7.0` (https://github.com/spring-projects/spring-framework/blob/v7.0.9/spring-core/src/main/java/org/springframework/core/retry/RetryTemplate.java lines 49 and 56).
* `org.springframework.core.retry.Retryable`, an interface (line 34 of `Retryable.java` in the same folder).
* `org.springframework.resilience.annotation.Retryable`, an annotation with `@Target({ElementType.TYPE, ElementType.METHOD})`, `@since 7.0` (https://github.com/spring-projects/spring-framework/blob/v7.0.9/spring-context/src/main/java/org/springframework/resilience/annotation/Retryable.java lines 47 and 55 to 59), enabled by `@EnableResilientMethods` in the same package.

So "Spring `RetryTemplate`" names two classes in two packages, and "`@Retryable`" names two annotations, with two further interfaces that share the simple name `Retryable`.

### resilience4j 2.4.0

Tag `v2.4.0` (commit `f1c3be8e79d09c70d457b2f89c3f89d03f1b5720`):

* `io.github.resilience4j.retry.Retry`, `public interface Retry` (https://github.com/resilience4j/resilience4j/blob/v2.4.0/resilience4j-retry/src/main/java/io/github/resilience4j/retry/Retry.java line 39), configured through `RetryConfig` and `RetryRegistry` in the same package.
* An annotation with the same simple name: `io.github.resilience4j.retry.annotation.Retry`, `@Target(value = {ElementType.METHOD, ElementType.TYPE})` (https://github.com/resilience4j/resilience4j/blob/v2.4.0/resilience4j-annotations/src/main/java/io/github/resilience4j/retry/annotation/Retry.java lines 27 to 29). The Micronaut module has a third, `io.github.resilience4j.micronaut.annotation.Retry`.

### Failsafe 3.3.2 and 2.4.4

* 3.x: `dev.failsafe.RetryPolicy`, `public interface RetryPolicy<R> extends Policy<R>`, "A policy that defines when retries should be performed", built with `RetryPolicyBuilder` (tag `failsafe-parent-3.3.2`, commit `8ae344fda21eb00c32a34bbd9517babe5b6fadc5`, https://github.com/failsafe-lib/failsafe/blob/failsafe-parent-3.3.2/core/src/main/java/dev/failsafe/RetryPolicy.java lines 18 to 29).
* 2.x: the class is `net.jodah.failsafe.RetryPolicy` (file `net/jodah/failsafe/RetryPolicy.java` in `net.jodah:failsafe:2.4.4`, sources jar on Maven Central).
* This Failsafe library (failsafe-lib) is unrelated to the Maven Failsafe plugin.

### OkHttp 5.5.1

Tag `parent-5.5.1` (commit `be5151ac3b63d3db405b9dbb62632c36cc6a657e`), https://github.com/square/okhttp/blob/parent-5.5.1/okhttp/src/commonJvmAndroid/kotlin/okhttp3/OkHttpClient.kt

* OkHttp has no retry handler class. Retrying is a builder option, `okhttp3.OkHttpClient.Builder.retryOnConnectionFailure(Boolean)` (lines 733 to 754), read back with `OkHttpClient.retryOnConnectionFailure()` (line 428). The builder's field defaults to `true` (line 596): "By default, this client silently recovers from" unreachable IP addresses, stale pooled connections and unreachable proxy servers; "Set this to false to avoid retrying requests when doing so is destructive."
* So an OkHttp client in a test retries connection failures unless the code sets the option to `false`; adding `retryOnConnectionFailure(true)` changes nothing, while removing a `retryOnConnectionFailure(false)` turns retries on.

### Apache HttpClient 5.6.5 and 4.5.14

HttpClient 5, tag `rel/v5.6.5` (commit `e6970b4b9da00485bc6e702a3c632933d19d6f3f`), under https://github.com/apache/httpcomponents-client/tree/rel/v5.6.5/httpclient5/src/main/java/org/apache/hc/client5/http

* The 5.x name is a retry *strategy*, not a handler: `org.apache.hc.client5.http.HttpRequestRetryStrategy`, `public interface` (`HttpRequestRetryStrategy.java` line 47), and `org.apache.hc.client5.http.impl.DefaultHttpRequestRetryStrategy` (`impl/DefaultHttpRequestRetryStrategy.java` line 68).
* `HttpClientBuilder` (`impl/classic/HttpClientBuilder.java`) has `setRetryStrategy(HttpRequestRetryStrategy)` (line 564) and `disableAutomaticRetries()` (line 574). Unless retries are disabled, the builder falls back to `DefaultHttpRequestRetryStrategy.INSTANCE` (lines 1002 to 1005).

HttpClient 4, tag `rel/v4.5.14` (commit `1778feefd4a3a342050061524d788b0cb646d40d`), under https://github.com/apache/httpcomponents-client/tree/rel/v4.5.14/httpclient/src/main/java/org/apache/http

* `org.apache.http.client.HttpRequestRetryHandler`, `public interface` (`client/HttpRequestRetryHandler.java` line 44); `org.apache.http.impl.client.DefaultHttpRequestRetryHandler` (line 58) and its subclass `StandardHttpRequestRetryHandler` (line 50); also `org.apache.http.client.ServiceUnavailableRetryStrategy` (line 40) with `DefaultServiceUnavailableRetryStrategy`.
* `org.apache.http.impl.client.HttpClientBuilder` has `setRetryHandler` (line 636), `disableAutomaticRetries()` (line 644) and `setServiceUnavailableRetryStrategy` (line 708). Unless retries are disabled, it falls back to `DefaultHttpRequestRetryHandler.INSTANCE` (lines 1145 to 1148).
* So both HttpClient lines retry by default; removing a `disableAutomaticRetries()` call turns retries on without naming any retry type.

## Not verified

* Surefire and Failsafe parameter names, user properties, and XML element names in versions older than 3.6.0.
* How Surefire 3.6.0 records: each `@RepeatedTest` repetition and the repetitions skipped by `failureThreshold`; a JUnit 4 test skipped by a failed assumption; a TestNG `SUCCESS_PERCENTAGE_FAILURE` result; TestNG attempts marked `SKIP` because they were retried.
* JUnit 6.x, AssertJ 4.0.0-M1, and JSONAssert 2.0-rc1.
* How Surefire 3.6.0 records the attempts that JUnit Pioneer's `@RetryingTest` and rerunner-jupiter's annotations turn into aborted executions (whether each appears as a `skipped` test case, and under which test case name), and the test case name it gives each invocation of a test template.
* That `-Dtest` leaves `groups` and `excludedGroups` in force was read from the 3.6.0 source, not observed in a run. For TestNG under 3.6.0 the provider does not add a tag filter (`JUnitPlatformProvider.java` line 520 onward), and how the TestNG engine applies `groups` and `excludedGroups` there was not checked. Versions older than 3.6.0 were not checked.
* rerunner-jupiter 2.1.6 beyond the file and class names in its sources jar, since it has no release tag.
* `BadApple` in Lucene versions other than 8.11.4, 9.0.0, 9.1.0, 9.12.0, 9.12.3, 10.0.0 and 10.5.2, and whether Solr or other projects define their own `BadApple`.
* How RandomizedRunner reports a test ignored by a disabled group (for example as JUnit 4 ignored, and so as `skipped` in Surefire's report).
* Hamcrest 1.3 and the older `hamcrest-library` artifact; only 3.0 and 2.2 were checked.
* Spring Framework releases before 7.0.9 were not read; the 7.0.9 `RetryTemplate` and `@Retryable` are marked `@since 7.0`. The retry libraries were checked at one version each (two for Failsafe), and older releases may use other names.
