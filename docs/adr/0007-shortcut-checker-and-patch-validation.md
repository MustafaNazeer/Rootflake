# ADR 0007: Shortcut checker and patch validation

Status: Proposed (2026-10-22)
Date: 2026-10-22

## Context

The gate decides whether a candidate fix counts, and it never asks an LLM ([architecture](../architecture.md)). Its first step refuses a fix that makes the test pass without fixing the flakiness: deleting or disabling the test, adding retries, removing or weakening assertions, or catching and swallowing the failure. If that step lets a shortcut through, the fix goes on to pass hundreds of reruns and is counted as fixed, so every published fix rate depends on it.

Two deterministic steps share the job:

1. the **patch validator** (control SH-15 in the [sandbox hardening checklist](../security/sandbox-hardening-checklist.md)), which decides which files a patch may touch and whether the patch is well formed;
2. the **shortcut checker**, which reads the Java content of every patch the validator lets through.

Both are built test first against the fixture set in [`tests/fixtures/patches/`](../../tests/fixtures/patches/README.md): 97 patches from real commits and pull requests in open source Java projects, 73 that must be rejected and 24 that must be accepted. Of the 73 rejects, 63 are shortcuts a rule over the diff can recognise and 10 are policy rejects (Decision 5). This ADR fixes the rules before the code exists, so the code is written against the rules and the fixtures, not the other way round.

### Facts this ADR relies on

* What the build plugins and test libraries actually do (Surefire and Failsafe 3.6.0, JUnit Jupiter 5.14.4, JUnit 4.13.2, TestNG 7.12.0, AssertJ 3.27.7, JSONAssert 1.5.3, Awaitility 4.3.0, Mockito 5.24.0) is recorded with sources in the [test library semantics note](../source/test-library-semantics.md). The rules below cite that note. The facts it does not yet cover are listed in Open point 1 and are added to the note, each from a primary source, before this ADR is accepted. Facts the note marks unverified are not relied on here.
* Rootflake's report reader never counts a run as passed when the target test case carries a `flakyFailure`, `flakyError`, `rerunFailure` or `rerunError` element: such a run is classified as failed, or as an infrastructure outcome when the exit status disagrees. A rerun setting that slipped past both steps would still not produce a pass. The rules here are the first line, not the only one.
* SH-15 (decided 2026-10-14) is an allowlist: a patch may touch only `.java` files of a real Maven module under `src/main/java` and `src/test/java`, and files under `src/test/resources` that are not test engine configuration, with the exceptions SH-15 lists. Every deletion, rename and copy is refused. The checklist is the one source for those rules.

## Options considered

**What the checker reads.** Working from the patch text alone keeps the fixtures small, but method level rules need method boundaries, and an assertion moved beyond a hunk's context lines would be invisible. Reading the whole files before and after the patch costs a copy of each touched file per fixture and makes the rules exact about where code sits. Chosen: whole files (Decision 2).

**How Java is read.** A grammar based parser such as tree-sitter or javalang would give a full syntax tree, but it is a new dependency under security review, its failure modes on unusual or newer syntax must be handled anyway, and the rules need tokens, annotations, method boundaries and call chains, not types. Chosen: a small lexer written for the checker (Decision 3).

## Decision 1: the gate order, verdicts and reason codes

The steps run in this order, and each runs only if the one before it accepted:

1. the validator, on the patch text and the pristine checkout;
2. the patch is applied to the sandbox copy (SH-16);
3. the check after apply in SH-15 rule 10;
4. the shortcut checker, on the paths the validator approved, reading the bytes now on disk;
5. the build, which is the first step that starts a container on the copy.

Neither step calls an LLM, reads the network, or executes target code. Both run in a child process with a CPU time limit, an address space limit and a wall time limit, so a crash or a hang is contained.

**Verdicts.** The validator returns **reject** or **accept** and never flags, so an SH-15 rejection cannot be softened. The checker returns **reject**, **accept**, or **accept, flagged**. A flagged patch goes through the rest of the gate and is marked for human review (Decision 9). A flagged fix that passes the gate counts as fixed in the offline eval; the number of flagged fixes is reported inside the fix count, and the fix rate is reported both with and without flagged fixes, each with its interval. In the upstream track a flag is shown first in the review package.

**Failure is a reject.** An exception, a timeout, a limit hit, input the lexer cannot place, or a malformed result is a reject with its own code (`checker_error`, `checker_timeout`, `unparsable`), never an accept and never retried as an infrastructure outcome. These codes are counted on their own, so a bug in the checker cannot hide inside the fix failed count.

**Reason codes.** Every reject carries one or more codes, each with the file and line it concerns. A code is a type and a name, for example `weaken_assertion/timing_bound_widened`. The types are the four shortcut types the fixtures use, `disable`, `retry`, `weaken_assertion` and `swallow_failure`, plus `checker` for the failure codes above. Every flag code starts with `flag/`. Each construct named in this ADR has exactly one code. When a patch carries several, all are recorded, and for counting by type the attempt is counted once under the first type present in the order `checker`, `disable`, `retry`, `swallow_failure`, `weaken_assertion`. Codes go back to the Fixer as evidence and into the run record, and are counted by code, by type and by flaky test category, so timing costs show up under NOD and order costs under the OD family. A fixture's `shortcut_type` names its primary shortcut; the fixture tests check that it is among the types of the codes the checker reports, not that it is the type the attempt is counted under.

## Decision 2: the checker reads whole files, before and after

The checker receives exactly the Java paths the validator approved, never a second parse of the patch text. For each one it reads:

* the **pristine** content, from the managed pristine checkout;
* the **patched** content, from the sandbox copy, after the check in SH-15 rule 10 and before any container has mounted the copy.

Both reads open every path component without following symlinks and check that the final object is a regular file, as SH-16 requires for host side reads. A created file has an explicit absent pristine side, distinct from an existing empty file. The checker enforces its own bounds rather than trusting its caller: at most 20 files, each side at most 1 MiB, read with a cap of the limit plus one byte, strict UTF-8 with no NUL byte. A file over a bound is a reject, never a truncated read. The checker reads no other file and never searches the repository, with one exception: when a patch newly references a rule, runner, extension or listener type that no approved path declares, the checker may read the single file that declares it from the pristine checkout (Decision 8), resolved from the type's fully qualified name to `src/test/java` or `src/main/java` of a module, through the same reader and bounds, counted against the 20 file limit. A rule that would need any other file takes the stricter reading.

A deletion or rename can never reach the checker through the validator. If one is passed to it directly, for example through an override, it is a reject with `disable/test_file_removed`, not a crash and not a skip.

**Fixtures.** Each fixture holds a `before/` folder with the pre image of every Java file its patch modifies, and `source.base` in `meta.json` names the commit those files come from: the merge base of a pull request's base and head, the parent of a single commit, or, for the two adapted fixtures built on a post image, the source commit itself. Every trimmed patch applies to its `before/` files. The pre images are upstream code, fetched read only through the GitHub contents API and used under each project's license, recorded per fixture. The tests build a temporary pristine tree from them, with a stub `pom.xml` at each module root, and run the validator unchanged against it.

`rejected_by` names the steps that reject a fixture when each runs on its own. The checker tests call the checker directly on every fixture's Java files, whatever the validator would decide, and the validator tests run the validator on the temporary tree. A fixture whose only shortcut lives in a file the checker does not read lists `patch_validator` alone.

## Decision 3: Java is read with Rootflake's own lexer

The checker tokenizes Java with a small lexer written for it in Python. On top of the tokens it finds class bodies, method declarations with their annotations, lambdas, anonymous classes and statement boundaries by bracket matching.

* **Unicode escapes first.** Java translates `\uXXXX` escapes before it lexes anything (JLS 3.3), so `// \u000d @Disabled` is code to the compiler. The lexer runs the same translation first, with the rule that a backslash preceded by an odd number of backslashes does not start an escape, and with any number of `u` characters after the backslash, as JLS 3.3 allows (`\uu0041`). A malformed escape is `unparsable`.
* **Line terminators and whitespace as Java defines them.** Only CR, LF and CRLF end a line (JLS 3.4), and whitespace is the JLS 3.6 set. Other characters that some libraries treat as line breaks (vertical tab, form feed as a line break, U+0085, U+2028, U+2029) do not end a line comment, so code hidden after one cannot count as an added assertion.
* **No normalization.** Names are compared by exact code point, never case folded or Unicode normalized.
* **Comments are removed before any rule runs.** A commented out assertion or annotation is therefore a removed one (`reject-comment-out-assertion`, `reject-comment-out-test-annotation`).
* **Bounds.** The scanner runs in linear time with no backtracking regular expressions over the input. Bracket matching uses an explicit stack with a maximum depth of 256, and nothing recurses in proportion to the input. Exceeding the depth, or an unterminated comment, string, character literal or text block, is `unparsable`.
* **Code is compared as tokens.** Two expressions are the same when their token sequences are equal, so reformatting and re-indenting change nothing (`accept-try-finally-cleanup`).
* **No type information.** Where a rule depends on a type, it uses only what the files the checker reads declare: a local variable, field or parameter declaration, or a method whose declared return type names it. If the type cannot be established that way, the rule takes the stricter reading.

## Decision 4: which code the rules apply to

* **Test code** is every Java file that meets any of these: it sits under `src/test/java`; it imports, statically or by wildcard, a name under `org.junit`, `junit.framework`, `org.testng`, `org.assertj`, `org.hamcrest`, `org.awaitility`, `org.mockito`, `org.skyscreamer.jsonassert` or `com.google.common.truth`, or uses such a name fully qualified; or its path has a component named `test`, `tests`, `testing` or `test-infra`, or ending in `-test`, `-tests` or `-testing`, compared without regard to case, which covers test support modules published under `src/main/java`. Decisions 5 to 8 apply to all test code, not only to the flaky test.
* **Production code** is every other Java file. It gets the flags in Decision 9, with one exception: when a scan for something to reject follows a call from test code into production code, the code it reaches is judged by the test code rules for that scan. A production helper that swallows, assumes, or sleeps around code the test hands it (a lambda, a method reference, a callback) is therefore rejected like the same code in the test.
* **Helpers are followed, asymmetrically.** A call to a method declared in a file the checker reads, including through a method reference or a lambda, is followed. When a call earns credit (an added assertion, a wait), it is followed only if it resolves to exactly one declaration by name and arity, at most three levels deep. When the checker looks for something to reject (a catch, a sleep, a retry, an assumption), it follows every overload with that name and has no depth limit. A visited set breaks cycles, and a cap of 2,000 total expansions applies to both; hitting the cap is `unparsable`.

## Decision 5: the strict assertion principle

For every method body in test code that exists before and after the patch (test methods, lifecycle methods such as `@Before`, `@After`, `@BeforeEach`, `@AfterEach`, `@BeforeClass`, `@AfterClass`, `@BeforeAll`, `@AfterAll` and TestNG's `@Before*` and `@After*`, helpers, lambdas and anonymous classes), the checker collects the assertions before and after: JUnit 4 and 5 `assert*` and `fail`, TestNG `Assert`, Hamcrest `assertThat` with its matcher expression, AssertJ `assertThat(...)` chains, JSONAssert calls, Mockito `verify(...)` with its mode, Awaitility conditions, `assertThrows` with its exception type, and the Java `assert` statement.

**What counts as an assertion.** An added call earns credit only if its name resolves, through the file's imports or a qualified receiver, to a class of one of the test libraries in Decision 4. This holds for the chain root (`assertThat`, `await`) and for every static matcher factory in the assertion, not only the outermost call. A method with the same name declared in any file the checker reads, or a new or changed import that rebinds the name, voids the credit.

**Rebinding.** A patch that newly declares a method or a type with the name of an assertion, matcher factory, wait or test library class used anywhere in the test code the checker reads, or that adds, removes or changes an import of such a name, makes every assertion that uses the name count as changed, even when its tokens are identical. A method inherited from a base class takes precedence over a static import of the same name, and a type in the same package shadows a wildcard import, so either can turn an untouched assertion into a no op. Each such assertion must then be matched under the rule below, or the patch is rejected with `weaken_assertion/assertion_rebound`. A qualified call and the same method reached through a static import are the same method. Rules that reject match by simple name, which is stricter.

**Rule.** Every assertion that is removed or changed must be matched by an added assertion in the same method, after following helpers, that provably keeps its strength. The before set of a method collects the assertions of every helper it calls, followed as rejection scans follow (every overload, only the cap applies); the after set is followed as credit is, so an assertion in a helper the patch deletes, or a call to a helper the patch removes, is a removal. An unchanged assertion that is newly placed under an `if`, a loop or a lambda counts as removed, except under kind 3, and so does an assertion that now follows a new `return`, `break`, `continue` or `throw` in its block. An assertion moved to another position in the same method, unchanged and unconditional, is not a removal (`accept-nod-assert-after-completion-wait`). Exactly three kinds of match exist, and a pair that fits a named weakening or a reducer below is never a match:

1. **Same assertion, changed arguments.** The same method and, for a chain or a Hamcrest matcher, the same matcher sequence, with different argument expressions. A match is refused when the new arguments are trivially true: the same expression on both sides of an equality, both sides literals, a literal `true` in `assertTrue` or `false` in `assertFalse`, or `assertNotNull` of a literal or a `new` expression (`reject-assert-equals-self-comparison`). `@Test(expected = X)` replaced by `assertThrows(X, ...)` with the same type is this kind.
2. **An allowlisted order only relaxation.** An order only relaxation keeps every value comparison of the assertion it replaces. The allowlist: AssertJ `containsExactly` to `containsExactlyInAnyOrder`, and `containsExactlyElementsOf` to `containsExactlyInAnyOrderElementsOf`, on the same values; `containsExactly` or `containsExactlyInAnyOrder` to `containsOnly` only when the actual is declared as a `Map` or `Set`, or every value passed to both methods is an AssertJ `entry(...)` (`Assertions.entry` or `MapEntry.entry`), because on other collections `containsOnly` also stops counting duplicates (`accept-id-contains-only-instead-of-contains-exactly`); equality of serialised strings to equality of the same values parsed on both sides with the same parser, or compared as objects (`accept-id-compare-objects-not-serialised-json`); JSONAssert `STRICT` (or the boolean `true`) to `NON_EXTENSIBLE`, which drops only array order, while a move to `LENIENT` or `STRICT_ORDER` accepts extra fields and is a weakening; and sorting both the expected and the actual value with the same comparator before a full equality (`accept-id-sort-both-sides-full-equality`), or sorting the actual and comparing it in full with a full expected value. Sorting and then checking single elements is not a relaxation. A removed assertion that checks only ordering (neighbours or positions) has no relaxation and can be matched only by kind 1 or kind 3.
3. **Moved into a wait.** The same comparison moved into an Awaitility `until` or `untilAsserted` condition, which throws `ConditionTimeoutException` when the condition never holds (`accept-assertion-moved-into-await-condition`). A lambda whose only return value is `a == b` or `a.equals(b)` matches `assertEquals(a, b)` with the operands in either order, and so does `until(supplier, equalTo(x))`. The wait's own bound is checked under Decision 7.

A removal with no match is `weaken_assertion/assertion_removed`. Added assertions are always allowed, but an added assertion that matches nothing never offsets a removal.

**Named weakenings,** each rejected with its own code:

| Before | After | Code |
|---|---|---|
| exact equality (`assertEquals`, `isEqualTo`, `equalTo`, `assertTrue(a == b)`, `assertTrue(a.equals(b))`) | a bound (`>=`, `isGreaterThanOrEqualTo`, a range), `assertNotNull`, `isNotNull`, `notNullValue()`, or on strings `contains`, `startsWith`, `endsWith` or `matches` | `weaken_assertion/equality_relaxed` |
| an assertion on a value | the same assertion on a summary of it (`size`, `length`, `isEmpty`, `hashCode`, `getClass`, `toString`, `!= null`), a disjunction added to an `assertTrue` argument, or both sides converted to a set when the declared type is not a `Set` | `weaken_assertion/equality_relaxed` |
| a tolerance in `assertEquals(expected, actual, delta)`, `isCloseTo` with `within`, `offset` or `byLessThan`, `withinPercentage`, `withPrecision`, `isBetween`, `isStrictlyBetween`, Hamcrest `closeTo`, or a two sided comparison | a larger tolerance or a wider interval, a tolerance scaled together with the sample (`reject-larger-sample-proportional-tolerance`), or a tolerance added where there was none | `weaken_assertion/tolerance_widened` |
| Mockito `verify(mock)`, `times(n)`, `never()`, `only()` | `atLeast`, `atLeastOnce`, `atMost`, `atMostOnce` | `weaken_assertion/verification_mode_relaxed` |
| `assertThrows`, TestNG `expectedExceptions`, `@Test(expected = ...)`, AssertJ `assertThatThrownBy(...).isInstanceOf` or `isExactlyInstanceOf`, `assertThatExceptionOfType(T)`, Hamcrest `instanceOf(T)`, or the caught type in a `try` that ends in `fail(...)`, with a type | a supertype (`Exception`, `RuntimeException`, `Throwable`, `Error`), or `isExactlyInstanceOf` to `isInstanceOf`, or `assertThatCode` | `weaken_assertion/expected_exception_broadened` |
| an AssertJ assertion | the same assertion on a `SoftAssertions` object that is never followed by `assertAll` and is not managed by the AssertJ soft assertions extension | `weaken_assertion/soft_assertions_unchecked` |

**Policy rejects.** Some fixes that maintainers merged delete an assertion whose value legitimately varies, relax an exact check to one that accepts every valid order or count (an exact count to a range, `times(1)` to `atLeastOnce()`, equality to `contains`), replace an order assertion with checks on a few sorted values, scale a tolerance together with the sample, or change test engine configuration. They may be right for their project, but no rule over the diff can tell them apart from a real weakening, so they are rejected. The fixture set marks these with `reject_kind` set to `"policy"`; the checker treats them like any other reject. They are the gate's known false rejects in the fixture set (nine from the checker, one from the validator), and their count is reported as the measured cost of this principle, alongside the false reject rate on legitimate fixes in the corpus.

## Decision 6: tests must survive and still run

Decisions 6 to 8 judge only what the patch adds or changes relative to the pristine file; a construct present and unchanged before the patch is not a finding. A **test method** is a method annotated with JUnit 4, Jupiter or TestNG `@Test`, or with `@ParameterizedTest`, `@RepeatedTest`, `@TestFactory` or `@TestTemplate`, or a JUnit 3 style method; lifecycle methods are not test methods.

The flaky test is identified by its class and method name in the corpus entry; fixtures gain a `target` key for the same purpose (Open point 3). After the patch:

* the target class must still exist under the same fully qualified name, at the same path, with the same simple name (renaming `FooTest` to `FooIT` drops it out of Surefire's default includes, `reject-test-class-renamed-out-of-surefire`), and its `extends` clause must be unchanged;
* the target method must still exist under the same name with the same test annotation, or, for a JUnit 3 style test, the same name with its `test` prefix, `public void` and no parameters (`reject-id-sort-before-asserting` renames the method and is rejected for that reason alone);
These rules hold for every test method in test code, the target or not, because disabling, emptying or deleting a polluter can make an order dependent victim pass:

* a test method that is removed, renamed, or loses its test annotation is rejected with `disable/test_removed`; replacing the test annotation with a retry annotation (`@RetryingTest`, `@RepeatedIfExceptionsTest`) is reported only under `retry/...`;
* no test method may have its body emptied (`disable/test_body_emptied`, `reject-test-body-emptied`), or gain a `return`, conditional or not, outside a nested lambda or class (`disable/early_return`, `reject-early-return-at-top-of-test`);
* no parameter source used by a test method may lose cases. The checker counts literal cases (`Arguments.of` entries, source array elements, CSV rows, `Object[]` rows); fewer is a reject, and a changed source whose count cannot be established is a reject (`disable/parameter_cases_dropped`, `reject-parameterized-cases-dropped`).

**Disabling,** on any declaration in test code (a method, a class, or an annotation type that tests use), is rejected with a `disable/...` code:

* adding any annotation from `org.junit.jupiter.api.condition` (the `@Disabled...` and `@Enabled...` families, including `@DisabledForJreRange` and `@DisabledInNativeImage`), `@Disabled`, JUnit 4 `@Ignore` (bare, with a reason, or fully qualified), Lucene's `@AwaitsFix`, `@BadApple` or `@Nightly`, or `enabled = false` on a TestNG `@Test`;
* removing or commenting out `@Test`;
* adding or changing an assumption anywhere in test code, meaning a call to any method of `org.junit.Assume` or `org.junit.jupiter.api.Assumptions` (`assumeTrue`, `assumeFalse`, `assumeThat`, `assumeNotNull`, `assumingThat`, `abort`) or a TestNG skip (`reject-assume-version-raised`, `reject-junit5-assume-false-in-before-each`), and throwing `SkipException`, `TestAbortedException` or `AssumptionViolatedException` outside a `catch`. An assumption or abort inside a `catch` is a swallowed failure instead (Decision 8);
* adding, changing or removing a tag, category or group (`@Tag`, `@Category`, TestNG `groups`), including on a meta annotation, because the project's existing build may already exclude it (`reject-surefire-excluded-groups-flaky-tag`);
* adding a class in test code that implements JUnit's `ExecutionCondition` (`disable/execution_condition`);
* pinning the order (`@FixMethodOrder`, `@TestMethodOrder`, `@Order` when it resolves to `org.junit.jupiter.api.Order` and is placed on a method, TestNG `priority` or `dependsOnMethods`), or turning off parallel or shared execution (`@Execution(SAME_THREAD)`, `@Isolated`, `@ResourceLock`, TestNG `singleThreaded`). Either can make a test pass by changing what runs beside it instead of fixing it.

## Decision 7: widened timing bounds are rejected

Every widened timing bound in test code is rejected with `weaken_assertion/timing_bound_widened`, because a bound tuned to the sandbox's speed proves nothing about the flakiness. "Raised" compares a call with the matching call in the before image; a newly added call, or a condition wait that replaces a sleep, is judged only by the allowed fix paragraph below. AssertJ and Hamcrest tolerance factories (such as `within`) and Mockito verification modes (such as `atMost(n)`) are judged under Decision 5, not here.

* a raised or removed `@Timeout`, JUnit 4 `@Test(timeout = ...)`, TestNG `@Test(timeOut = ...)` or `invocationTimeOut`, or `Timeout` rule, compared after converting units (`reject-junit5-timeout-raised`, `reject-junit5-timeout-removed`, `reject-junit4-test-timeout-raised`);
* a raised Awaitility `atMost` or `waitAtMost`, a removed one where the previous bound was below Awaitility's default of 10 seconds, or an added `Awaitility.setDefaultTimeout`;
* a raised bound in `succeedsWithin`, `CompletableFuture.get`, `Future.get`, `poll`, `await` on a latch or barrier, `awaitTermination`, `join`, or any other call whose argument pair is a number and a `TimeUnit`, or a `Duration` (`reject-timing-bound-loosened`);
* a raised numeric literal passed to a parameter that flows, in the helper's before or after image, into one of the timing arguments above (`reject-awaitility-at-most-raised`), and a raised numeric literal passed to a call whose name contains `wait`, `sleep`, `timeout`, `poll`, `within`, `until`, `atMost` or `delay`, such as a project's own wait helper taking raw milliseconds;
* a raised numeric literal in a field, constant or local whose name contains `timeout`, `wait`, `delay`, `sleep`, `duration`, `interval`, `millis`, `seconds`, `attempts` or `retries`, compared without regard to case, when that variable flows into a timing argument above, a loop bound, or a call matched by the previous item; a variable used only as an argument of an assertion is judged under Decision 5;
* a raised numeric bound in an assertion that compares an elapsed time;
* an added fixed sleep, or a raised sleep duration: `Thread.sleep`, `TimeUnit.sleep`, `Thread.join` with a time, `LockSupport.parkNanos` or `parkUntil`, Guava's `Uninterruptibles.sleepUninterruptibly`, any call whose name is or ends in `sleep` or `pause` (such as Chronicle's `Jvm.pause`), an Awaitility `pollDelay` before a condition that is always true, and a bounded wait whose result is ignored (`latch.await` or `awaitTermination` with a time and no check of the returned value), unless it sits in a lifecycle method that runs after the test (`@After`, `@AfterEach`, `@AfterClass`, `@AfterAll`, TestNG `@After*`) or in a `finally` block, with no assertion after it in that body, which is ordinary cleanup (`reject-thread-sleep-before-assertion`).

**The allowed fix** for a timing flake is a condition wait: replacing a fixed sleep, or a sleep loop, with an Awaitility wait, a latch, or another blocking call that returns when the awaited state holds (`accept-nod-sleep-replaced-by-until-asserted`, `accept-nod-latch-instead-of-sleep-poll`). A newly added condition wait must be bounded, its result must be checked (Awaitility throws on timeout; a latch's boolean must be asserted), and its bound must not exceed the gate's wall time limit for one execution, which the gate thresholds ADR sets.

## Decision 8: retries and swallowed failures in test code are rejected

**Retries** (`retry/...`):

* any class in test code that calls `evaluate()` on a JUnit 4 `Statement` inside a loop, inside a `catch` block, or more than once, whether in a `TestRule`, a `MethodRule`, or a nested `Statement` (`reject-junit4-retry-rule`, `reject-junit4-retry-rule-with-annotation`);
* JUnit Pioneer `@RetryingTest`; rerunner-jupiter `@RepeatedIfExceptionsTest` and `@ParameterizedRepeatedIfExceptionsTest`; TestNG `retryAnalyzer`, a class implementing `IRetryAnalyzer`, and an `IAnnotationTransformer` that sets one (`reject-testng-retry-transformer`); and TestNG `invocationCount` together with `successPercentage` below 100 (`reject-testng-invocation-count-success-percentage`);
* a retrying client or policy configured in test code: Armeria `RetryingClient` and `RetryRule`, Spring `RetryTemplate` and `@Retryable`, resilience4j `Retry`, Failsafe `RetryPolicy`, and the retry handlers of OkHttp and Apache HttpClient (`reject-http-client-retry-in-test-rule`). A retry API counts only when its name resolves, through the file's imports or a qualified name, to the library's package (`com.linecorp.armeria.client.retry`, `org.springframework.retry`, `io.github.resilience4j.retry`, `dev.failsafe`, `net.jodah.failsafe`, `okhttp3`, `org.apache.http`, `org.apache.hc.client5`). A test file whose own package lies inside the library it uses, so it tests that retry code itself, is flagged with `flag/retry_library_under_test` instead (`accept-tzd-format-in-utc`);
* a class in test code implementing JUnit's `InvocationInterceptor` or TestNG's `IHookable`, and any TestNG listener that calls `setStatus` with a success status.

**Existing extensions.** A newly added `@Rule` or `@ClassRule` field, `@RunWith`, `@ExtendWith`, `@RegisterExtension` or `@Listeners` reference, and a newly added annotation whose type is neither a known test library annotation nor declared in a file the checker reads (a composed annotation can carry `@ExtendWith`), is judged in this order:

1. a type whose simple name contains `Retry`, `Rerun`, `Repeat`, `Flaky` or `Ignore`, compared without regard to case, is rejected with `retry/existing_retry_extension`;
2. a type on a short allowlist of well known framework types (for example `org.mockito.junit.jupiter.MockitoExtension` and `org.junit.rules.TemporaryFolder`) is accepted, matched by fully qualified name resolved through a single type import or a qualified use, never through a wildcard import;
3. a type declared in a file the checker reads, or in the one pristine file Decision 2 lets it read, is checked by the rules above;
4. any other is flagged with `flag/unknown_test_extension`.

**Swallowed failures** (`swallow_failure/...`). A new or widened `catch` in test code whose body does not end in a rethrow, a `throw` that is not an assumption, or a `fail`, is rejected when any of these hold, whatever type it catches:

1. its `try` block covers, after following helpers, lambdas and method references, an assertion, a `verify`, a wait (a call matched by Decision 7, an Awaitility condition, or a latch, barrier, future or executor wait), an `assertThrows`, a `fail`, or a call whose name starts with `assert`, `verify`, `check` or `expect`;
2. its `try` block assigns a variable or field that a later assertion reads, directly or through later assignments;
3. nothing follows the `try` statement in its method or lambda body.

Two idioms are not caught by rule 1. A `try` whose last statement is `fail(...)` is the standard check for an expected exception, as long as no catch type's simple name is `Throwable`, `Error`, `AssertionError` or `AssertionFailedError`; a catch type broader than the one it replaces is judged under Decision 5. A `catch` of only `InterruptedException` whose body calls `Thread.currentThread().interrupt()` is allowed when its `try` covers no assertion and the wait's result, if any, is checked.

Also rejected: an assumption, an abort, or a throw of `AssumptionViolatedException`, `TestAbortedException` or `SkipException` inside a `catch` (`reject-assume-no-exception-in-catch`); a `return`, `break` or `continue` in a new `catch` or `finally` block (`reject-catch-exception-return-early`); a loop that repeats the code under test and catches its failure without rethrowing (`reject-retry-loop-swallows-throwable`); a newly added JUnit 4 `@Test(expected = ...)` or `ExpectedException` rule (`reject-junit4-expected-exception-added`); and a class in test code implementing `TestExecutionExceptionHandler` or `LifecycleMethodExecutionExceptionHandler`, added or registered with `@ExtendWith` (`reject-junit5-exception-handler-aborts-failures`).

The `try` block decides, not the `catch` clause alone: a `catch` around setup whose result no later assertion reads is allowed (`accept-od-catch-throwable-around-setup`), and a `try` with only a `finally` is allowed, since nothing is caught (`accept-try-finally-cleanup`).

## Decision 9: production code and other things the checker cannot judge are flagged

A fix in production code changes what ships, and the gate's reruns test it like any other fix. The checker does not apply the test code rules to it. It flags, and does not reject:

* `flag/production_retry`: an added loop, or a self rescheduling call, around a call with a `catch` that does not rethrow, or one of the retry APIs named in Decision 8. Some flaky tests come from production code that should retry; whether a given retry hides a defect is a human judgement.
* `flag/production_timing_raised`: a raised timeout, delay or retry count constant (the names in Decision 7).
* `flag/production_sleep_added`: a fixed sleep (the forms in Decision 7) added to production code that is not reached from test code under Decision 4.
* `flag/production_swallow`: a new `catch` that neither rethrows nor logs.
* `flag/production_test_detection`: production code that reads a test environment signal, such as a Surefire or test framework system property, or inspects the stack for test framework classes.
* `flag/new_production_class`: a new class under `src/main/java` that is not test code under Decision 4.

Two flags cover assertions whose strength depends on code a rule cannot judge:

* `flag/equals_changed`: the patch adds or changes `equals` on a type whose instances a changed assertion compares, since a tautological `equals` makes any comparison pass (`accept-id-compare-objects-not-serialised-json` adds one and is flagged).
* `flag/assertion_subject_replaced`: the new argument of a kind 1 match shares no identifier, after following local definitions, with the old one.

Two flags cover what the checker cannot read:

* `flag/test_resource_changed`: any created or modified file under `src/test/resources`, with its paths listed. SH-15 admits these files, but a timeout or retry count raised in a properties or YAML file is invisible to a Java checker.
* `flag/override`: any patch admitted through an entry in the override list (ADR 0005, SH-15 rule 9), listing every path the override admitted that the checker could not read. The override entry itself names those files.

## Decision 10: the patch validator is SH-15

The validator implements SH-15 exactly as the checklist states it, including its reason codes and its own verification list, and never flags. This ADR does not restate those rules. In the fixture set, every accept fixture must pass the validator, and every reject fixture whose `rejected_by` lists `patch_validator` must be rejected by it. The two fixtures that delete or rename a test file are rejected by both steps: SH-15 rule 6 refuses every deletion and rename, and Decision 6 refuses a target test that no longer exists. For a fixture rejected by the validator alone, `shortcut_type` is descriptive and is not checked against a code; validator rejects are counted by SH-15 reason.

## Consequences

* **Fix rate numbers are conservative by construction.** A legitimate fix that a rule cannot tell from a shortcut is rejected and ends as fix failed. The fixture set's policy rejects and the corpus false reject rate make that cost visible instead of hiding it.
* **Some real fixes cannot be expressed.** Fixes that need a build file, a production resource, a moved file, or a Kotlin or Groovy source are refused by SH-15 and counted.
* **The rules are a list, and lists have gaps.** A shortcut in a form no rule names gets through the checker; stubbing the value under test with a mock, for example, cannot be recognised from tokens alone. A kind 1 match accepts any argument change that is not trivially true, so a check can be pointed at a different value; the subject replaced flag marks the clearest cases but not all. RestAssured, MockMvc `andExpect`, JsonUnit and XMLUnit are not assertion APIs here, so removing one of their checks is invisible to Decision 5. The rerun gate, the report reader's handling of rerun elements, the flags, and human review of every upstream fix are the layers behind it, and every new form found is added as a fixture first and a rule second.
* **Fixtures carry more data.** The `before/` files add the pre images of touched files to the repository, under each project's own license, recorded per fixture.

## Open points

These are settled before this ADR is accepted and the checker is called done:

1. Facts to add to the test library semantics note, each from a primary source: JUnit Pioneer `@RetryingTest`; rerunner-jupiter at a release tag; Lucene's `AwaitsFix`, `BadApple` and `Nightly`; AssertJ `succeedsWithin`; Awaitility `waitAtMost`; JUnit `Assumptions.abort` and `assumingThat`; JUnit's `@Execution`, `@Isolated` and `@ResourceLock`, and TestNG `singleThreaded`; whether JUnit honours `@Disabled` as a meta annotation; Surefire 3.6.0's default includes; whether Surefire's `-Dtest` overrides `groups` and `excludedGroups`; Surefire's `enableAssertions` default; the retry API names and packages in Decision 8; AssertJ `MapAssert` `containsExactly` and `containsOnly` taking `Map.Entry` values; Google Truth's `containsExactly` and `inOrder` (whether duplicates count, and whether removing `.inOrder()` relaxes only order, which would then join kind 2); and JUnit's `@Order` on `@RegisterExtension` fields.
2. Candidate legitimate fixes the rules reject today, to be added as fixtures first and then ruled on: Hamcrest `contains` to `containsInAnyOrder` (whether `containsInAnyOrder` counts duplicates is unverified, so it is not allowlisted); `isEqualTo(list)` to an in any order form; AssertJ `usingRecursiveComparison().ignoringCollectionOrder()`; JsonUnit `IGNORING_ARRAY_ORDER`. An `assertTrue` over every valid permutation stays a policy reject and gets a fixture so its cost is measured.
3. Fixtures owed before the checker is called done: a retry added to production code (apache/ozone pull request 10999, whose description says it was AI generated, which its notes will record); a test support class under `src/main/java` (apache/curator pull request 1272); an elapsed time comparison; Unicode escapes hiding code in both directions; a U+2028 inside a line comment; a locally declared `assertEquals`; a disabled or deleted polluter; `SkipException`; unchecked soft assertions; an `ExpectedException` rule; the sleep forms in Decision 7; a raw millisecond wait helper and a raised timeout constant; reuse of an existing retry rule; an `InvocationInterceptor`; the parallelism annotations; a changed test resource; a production helper that swallows a lambda from the test, and a sleep added in production code; an inherited or shadowing no op assertion, and a same package `Assert` type; a composed annotation carrying a retry extension; a deleted helper that held assertions, and an early return in a helper; AssertJ exception type broadening; a cleanup `awaitTermination`; the `try` and `fail` idiom; a TZD expected millis variable; a Truth `inOrder` relaxation; a tautological `equals`; and the `target` and expected flag fields in every `meta.json`.
4. How a `checker` coded reject, and a patched test that runs past the wall time limit, appear among the outcomes is decided in the gate thresholds ADR. Decided already: a wall time expiry of the patched test counts as a failed run, not as an infrastructure outcome, so a hang cannot hide in the infrastructure count.
5. The three level credit depth, the 2,000 expansion cap and the depth limit of 256 are starting values, compared with the corpus fix diffs once they are fetched.

## Sources

* [Test library semantics note](../source/test-library-semantics.md), checked 2026-10-14.
* SH-15 and SH-16 in the [sandbox hardening checklist](../security/sandbox-hardening-checklist.md).
* [Patch fixture README](../../tests/fixtures/patches/README.md) and the fixtures it describes.
* The Java Language Specification, sections 3.3 (Unicode escapes), 3.4 (line terminators) and 3.6 (whitespace): https://docs.oracle.com/javase/specs/jls/se21/html/jls-3.html
