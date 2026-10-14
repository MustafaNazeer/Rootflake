# Patch fixtures for the shortcut checker

Rootflake rejects a candidate fix before any rerun if it takes a shortcut: deleting or disabling the test, adding retries, removing or weakening assertions, or catching and swallowing the failure. Two deterministic steps enforce this: a patch path validator that refuses edits to build, CI and test runner configuration, and the shortcut checker that reads the content of the diff. Both are built test first against the patches in this folder.

Every patch here comes from a real commit or pull request in an open source Java project. None of them is invented. Where a real example of a variant could not be found, I changed a real hunk as little as possible to express it and recorded the change.

## Layout

Each fixture is a folder named after its id:

```
<id>/patch.diff   a unified diff, trimmed to the hunks that matter
<id>/meta.json    what the gate should do with it, and where it came from
```

`meta.json` has these keys:

| Key | Meaning |
|---|---|
| `id` | Folder name. Starts with `reject-` or `accept-`. |
| `expected` | `"reject"` if the gate must refuse the patch before any rerun, `"accept"` if it must let it through to the build and rerun steps. |
| `reject_kind` | For a reject fixture, `"shortcut"` (a rule over the diff can recognise it) or `"policy"` (a maintainer merged fix that no diff rule can separate from a shortcut, rejected on principle). `null` for an accept fixture. |
| `rejected_by` | Which step must reject it: `"shortcut_checker"`, `"patch_validator"`, or both. Empty for an accept fixture. |
| `shortcut_type` | `"disable"`, `"retry"`, `"weaken_assertion"`, `"swallow_failure"`, or `null` for an accept fixture. |
| `variant` | Short slug for the form of the shortcut or fix, for example `junit4_ignore_fully_qualified` or `sort_both_sides`. |
| `disputed` | Kept for compatibility. Every fixture has a ruling, so it is `false` everywhere. |
| `notes` | What the patch does, which rule decides it, and, for an adapted patch, exactly what was changed. |
| `derivation` | `"verbatim"` if every hunk is copied unchanged from the source diff, `"adapted"` if a hunk was edited. |
| `source` | `repo`, `commit` (40 hex; the merge commit for a pull request), `pr` when there is one, `path` of the main file, `license` as reported by the GitHub API, and `fetched` (UTC date). |

A verbatim patch keeps whole hunks with their original headers, so its line numbers match the source commit. Files and hunks that do not bear on the case are dropped, which means a patch may not apply to the source tree on its own; it is meant to be parsed, not applied. Every patch parses with `git apply --stat`.

## The rules the fixtures encode

* **Strict assertion principle.** A removed or changed assertion passes only if an added assertion in the same test method provably keeps its strength: the same assertion method and matcher with changed arguments; an allowlisted order only relaxation, such as `containsExactly` to `containsExactlyInAnyOrder` or string equality to equality of parsed objects; or the same comparison moved into an Awaitility `until` or `untilAsserted` condition.
* **Policy rejects.** Some maintainer merged fixes delete an assertion about an order the code never promised, or turn an exact count into a range. They may be right for their project, but no diff rule can tell them from a real weakening, so they are rejected with `reject_kind` set to `"policy"`.
* **Timing.** Every widened timing bound in test code is rejected: a raised or removed timeout, a raised Awaitility `atMost`, a looser wall clock assertion, a fixed sleep added before an assertion. Replacing a fixed sleep with a condition wait is the allowed fix.
* **Retries.** A retry added to test code or to the build is rejected. A retry added to production code is flagged for review, not rejected; no fixture covers that case yet.
* **Paths.** The patch validator rejects any change to `pom.xml` at any depth, `.mvn/`, `mvnw`, `mvnw.cmd`, CI configuration (`.github/`, `Jenkinsfile`, `.gitlab-ci.yml`), and test runner configuration (`junit-platform.properties`, `META-INF/services/`, TestNG suite XML files). A fixture that touches such a path and also carries a shortcut the checker should see lists both steps in `rejected_by`.

`shortcut_type` keeps four values. Timing changes are filed under `weaken_assertion`, because they loosen what the test checks. Pinning the test order, renaming a class out of the Surefire includes, an early return and an emptied test body are filed under `disable`, because each one stops the failing case from running.

## Where the patches come from

**Accept fixtures** are legitimate flaky test fixes. Where possible they come from fixes linked in [IDoFT](https://github.com/TestingResearchIllinois/idoft) `pr-data.csv` at commit `4903dc9233eead4e4191c66f9ed0b14eea091052`, the same pinned commit the corpus builder reads, and only from rows whose Status is not `Accepted` (`Opened`, `InspiredAFix`, `DeveloperFixed`). None of those fixes is linked from an `Accepted` row, and the corpus draws only from `Accepted` rows, so no corpus entry's fix is used here. For `Opened` rows the linked pull request was checked on GitHub and has since been merged; for `InspiredAFix` and `DeveloperFixed` rows the fix is the merged commit or pull request the row names. The `notes` field of each of these fixtures names the IDoFT row it was found through.

IDoFT has few merged fixes outside `Accepted` for some categories, so six accept fixtures come from merged pull requests outside IDoFT: three NOD fixes, one NIO fix, one TZD fix, and the rocketmq fix in `accept-assertion-moved-into-await-condition`. For each of them, neither the pull request nor the test class appears in `pr-data.csv` under any status, and the notes say so. The paimon fix in `reject-larger-sample-proportional-tolerance` also comes from outside IDoFT under the same check. The rocketmq and paimon fixtures were reviewed when the set was first built and are kept for that reason.

**Reject fixtures** come from merged commits and pull requests in which maintainers disabled, retried, or weakened a test they considered flaky, found through GitHub search, plus the IDoFT linked fixes that the rules above reject. Some of those changes were reasonable decisions for the project at the time; they are still shortcuts by Rootflake's definition, because a fix that does the same thing proves nothing about the flakiness.

Known overlaps with IDoFT rows, none of which reuses a corpus fix:

* `reject-nod-sleep-replaced-by-await` uses log4j2 pull request 3957, linked from an `Opened` row for `AsyncThreadContextTest.testStateLeakage`. That test also has `Accepted` rows with other fix PRs (3945 and 3956); the patch itself changes a different test.
* `reject-id-contains-instead-of-exact-sql` changes `TableUtilsTest`, the same class as the `Accepted` row `com.j256.ormlite.table.TableUtilsTest.testMissingCreate` with fix PR j256/ormlite-core#310. The method and the PR differ.
* `reject-junit5-timeout-raised` and `reject-junit5-timeout-removed` change netty's `PooledByteBufAllocatorTest`, which has an IDoFT row with an empty Status; it has no `Accepted` row.

Twenty one fixtures are `adapted`. Each one names the verbatim fixture or source hunk it was built from and states the edit. In most of them every context and removed line is copied from the source diff's pre image and only added lines are new. The exceptions are listed in their notes: `reject-assert-throws-broadened` is built on the post image of its source commit, `reject-early-return-at-top-of-test` is a merged hunk applied in reverse, and `reject-maven-config-rerun` and `reject-extension-autodetection-registration` add new files whose single lines are hand written.

## Licenses and attribution

The code in these patches belongs to the projects below and is used under their licenses. Each `meta.json` links the exact commit. IDoFT itself declares no license; only its row links are used to find fixes, and no IDoFT content is copied here.

| Project | License | Source | Fixtures |
|---|---|---|---|
| [Accenture/Mercury](https://github.com/Accenture/Mercury) | Apache-2.0 | [commit 47663ff](https://github.com/Accenture/Mercury/commit/47663ff9c7803aa8613a81b3dff71ec9908e2d6e) | 1 |
| [adobe/regola](https://github.com/adobe/regola) | Apache-2.0 | [#92](https://github.com/adobe/regola/pull/92) | 1 |
| [Ahoo-Wang/CosId](https://github.com/Ahoo-Wang/CosId) | Apache-2.0 | [#1158](https://github.com/Ahoo-Wang/CosId/pull/1158) | 2 |
| [alibaba/Sentinel](https://github.com/alibaba/Sentinel) | Apache-2.0 | [#3282](https://github.com/alibaba/Sentinel/pull/3282), [#3459](https://github.com/alibaba/Sentinel/pull/3459) | 3 |
| [amazon-mq/upstreaming-activemq](https://github.com/amazon-mq/upstreaming-activemq) | Apache-2.0 | [#19](https://github.com/amazon-mq/upstreaming-activemq/pull/19) | 1 |
| [apache/accumulo](https://github.com/apache/accumulo) | Apache-2.0 | [#6008](https://github.com/apache/accumulo/pull/6008) | 1 |
| [apache/activemq](https://github.com/apache/activemq) | Apache-2.0 | [#2624](https://github.com/apache/activemq/pull/2624) | 1 |
| [apache/bookkeeper](https://github.com/apache/bookkeeper) | Apache-2.0 | [#4543](https://github.com/apache/bookkeeper/pull/4543) | 1 |
| [apache/camel](https://github.com/apache/camel) | Apache-2.0 | [#26040](https://github.com/apache/camel/pull/26040), [#26193](https://github.com/apache/camel/pull/26193) | 2 |
| [apache/commons-lang](https://github.com/apache/commons-lang) | Apache-2.0 | [commit 08d1f66](https://github.com/apache/commons-lang/commit/08d1f6623678e8cd0b37a9d2d721b95545bcc151) | 1 |
| [apache/commons-math](https://github.com/apache/commons-math) | Apache-2.0 | [commit 5097729](https://github.com/apache/commons-math/commit/50977290cdda79896ccb6304067341486fe104d2) | 2 |
| [apache/directory-scimple](https://github.com/apache/directory-scimple) | Apache-2.0 | [#201](https://github.com/apache/directory-scimple/pull/201) | 1 |
| [apache/druid](https://github.com/apache/druid) | Apache-2.0 | [#20453](https://github.com/apache/druid/pull/20453) | 1 |
| [apache/dubbo](https://github.com/apache/dubbo) | Apache-2.0 | [commit 669705c](https://github.com/apache/dubbo/commit/669705c7d3fa69244cb3301af0f2cd1360609857) | 1 |
| [apache/gravitino](https://github.com/apache/gravitino) | Apache-2.0 | [#13287](https://github.com/apache/gravitino/pull/13287) | 4 |
| [apache/hadoop](https://github.com/apache/hadoop) | Apache-2.0 | [#8083](https://github.com/apache/hadoop/pull/8083) | 1 |
| [apache/incubator-seata](https://github.com/apache/incubator-seata) | Apache-2.0 | [#7819](https://github.com/apache/incubator-seata/pull/7819) | 1 |
| [apache/iotdb](https://github.com/apache/iotdb) | Apache-2.0 | [#15698](https://github.com/apache/iotdb/pull/15698) | 1 |
| [apache/jackrabbit-oak](https://github.com/apache/jackrabbit-oak) | Apache-2.0 | [#3177](https://github.com/apache/jackrabbit-oak/pull/3177) | 1 |
| [apache/jena](https://github.com/apache/jena) | Apache-2.0 | [#2098](https://github.com/apache/jena/pull/2098) | 1 |
| [apache/logging-log4j2](https://github.com/apache/logging-log4j2) | Apache-2.0 | [#3957](https://github.com/apache/logging-log4j2/pull/3957) | 1 |
| [apache/ozone](https://github.com/apache/ozone) | Apache-2.0 | [#11062](https://github.com/apache/ozone/pull/11062) | 2 |
| [apache/paimon](https://github.com/apache/paimon) | Apache-2.0 | [#7387](https://github.com/apache/paimon/pull/7387), [#8850](https://github.com/apache/paimon/pull/8850) | 3 |
| [apache/pdfbox](https://github.com/apache/pdfbox) | Apache-2.0 | [commit c1c910a](https://github.com/apache/pdfbox/commit/c1c910ad7f9bb7d2fd26e7e01e8c90bcc3910f36) | 1 |
| [apache/ratis](https://github.com/apache/ratis) | Apache-2.0 | [#1621](https://github.com/apache/ratis/pull/1621) | 1 |
| [apache/rocketmq](https://github.com/apache/rocketmq) | Apache-2.0 | [#7445](https://github.com/apache/rocketmq/pull/7445), [#10374](https://github.com/apache/rocketmq/pull/10374), [#10390](https://github.com/apache/rocketmq/pull/10390) | 6 |
| [apache/servicecomb-java-chassis](https://github.com/apache/servicecomb-java-chassis) | Apache-2.0 | [#5018](https://github.com/apache/servicecomb-java-chassis/pull/5018) | 1 |
| [apache/shenyu](https://github.com/apache/shenyu) | Apache-2.0 | [#6261](https://github.com/apache/shenyu/pull/6261) | 1 |
| [apache/tika](https://github.com/apache/tika) | Apache-2.0 | [commit 0bf30f0](https://github.com/apache/tika/commit/0bf30f0e653ff7262cd146cee6b0bfc81a6410ff) | 1 |
| [Apicurio/apicurio-registry](https://github.com/Apicurio/apicurio-registry) | Apache-2.0 | [#8861](https://github.com/Apicurio/apicurio-registry/pull/8861), [commit ead084d](https://github.com/Apicurio/apicurio-registry/commit/ead084d4c69b4b994e82673816122ba469b3977d) | 4 |
| [Azure/azure-sdk-for-java](https://github.com/Azure/azure-sdk-for-java) | MIT | [#26548](https://github.com/Azure/azure-sdk-for-java/pull/26548) | 1 |
| [codeheadsystems/hofmann-elimination](https://github.com/codeheadsystems/hofmann-elimination) | Apache-2.0 | [#112](https://github.com/codeheadsystems/hofmann-elimination/pull/112) | 2 |
| [Consensys-Incorporated/teku](https://github.com/Consensys-Incorporated/teku) | Apache-2.0 | [#10603](https://github.com/Consensys-Incorporated/teku/pull/10603) | 1 |
| [debezium/debezium-connector-spanner](https://github.com/debezium/debezium-connector-spanner) | Apache-2.0 | [#176](https://github.com/debezium/debezium-connector-spanner/pull/176) | 1 |
| [ebean-orm/ebean](https://github.com/ebean-orm/ebean) | Apache-2.0 | [#3409](https://github.com/ebean-orm/ebean/pull/3409) | 1 |
| [felleslosninger/efm-kosmos](https://github.com/felleslosninger/efm-kosmos) | BSD-3-Clause | [#212](https://github.com/felleslosninger/efm-kosmos/pull/212) | 1 |
| [FoundationDB/fdb-record-layer](https://github.com/FoundationDB/fdb-record-layer) | Apache-2.0 | [#4716](https://github.com/FoundationDB/fdb-record-layer/pull/4716) | 1 |
| [frankframework/frankframework](https://github.com/frankframework/frankframework) | Apache-2.0 | [#11561](https://github.com/frankframework/frankframework/pull/11561) | 1 |
| [googleapis/java-firestore](https://github.com/googleapis/java-firestore) | Apache-2.0 | [#2273](https://github.com/googleapis/java-firestore/pull/2273) | 3 |
| [haraldk/TwelveMonkeys](https://github.com/haraldk/TwelveMonkeys) | BSD-3-Clause | [commit be34854](https://github.com/haraldk/TwelveMonkeys/commit/be348543d8a41d4677f83e1c69988221527977a9) | 1 |
| [hiero-ledger/hiero-block-node](https://github.com/hiero-ledger/hiero-block-node) | Apache-2.0 | [#3418](https://github.com/hiero-ledger/hiero-block-node/pull/3418) | 1 |
| [infinispan/infinispan](https://github.com/infinispan/infinispan) | Apache-2.0 | [#18137](https://github.com/infinispan/infinispan/pull/18137) | 1 |
| [j256/ormlite-core](https://github.com/j256/ormlite-core) | ISC | [#240](https://github.com/j256/ormlite-core/pull/240) | 3 |
| [jenkinsci/remoting](https://github.com/jenkinsci/remoting) | MIT | [#706](https://github.com/jenkinsci/remoting/pull/706) | 1 |
| [killbill/killbill](https://github.com/killbill/killbill) | Apache-2.0 | [commit e3b153e](https://github.com/killbill/killbill/commit/e3b153e9c9f65e698c8660bb8dfa901856359bb4) | 4 |
| [line/decaton](https://github.com/line/decaton) | Apache-2.0 | [#194](https://github.com/line/decaton/pull/194) | 1 |
| [lukewalker85/automation-framework](https://github.com/lukewalker85/automation-framework) | MIT | [#101](https://github.com/lukewalker85/automation-framework/pull/101) | 1 |
| [mongodb/mongo-java-driver](https://github.com/mongodb/mongo-java-driver) | Apache-2.0 | [#2073](https://github.com/mongodb/mongo-java-driver/pull/2073), [commit 01aff5a](https://github.com/mongodb/mongo-java-driver/commit/01aff5a0789f71b9d0b56190d4996e8ac5436827) | 3 |
| [mybatis/mybatis-dynamic-sql](https://github.com/mybatis/mybatis-dynamic-sql) | Apache-2.0 | [#988](https://github.com/mybatis/mybatis-dynamic-sql/pull/988) | 1 |
| [netty/netty](https://github.com/netty/netty) | Apache-2.0 | [#17590](https://github.com/netty/netty/pull/17590), [#17763](https://github.com/netty/netty/pull/17763) | 3 |
| [objectionary/eo](https://github.com/objectionary/eo) | MIT | [#6540](https://github.com/objectionary/eo/pull/6540) | 1 |
| [Open-J-Proxy/ojp-framework-integration](https://github.com/Open-J-Proxy/ojp-framework-integration) | Apache-2.0 | [#40](https://github.com/Open-J-Proxy/ojp-framework-integration/pull/40) | 1 |
| [OpenHFT/Chronicle-Core](https://github.com/OpenHFT/Chronicle-Core) | Apache-2.0 | [commit bae5b40](https://github.com/OpenHFT/Chronicle-Core/commit/bae5b403ab3b76893d3db55909ebb560481405e3) | 1 |
| [openlogiclab/kafka-pipeline](https://github.com/openlogiclab/kafka-pipeline) | Apache-2.0 | [#36](https://github.com/openlogiclab/kafka-pipeline/pull/36) | 1 |
| [opensearch-project/ml-commons](https://github.com/opensearch-project/ml-commons) | Apache-2.0 | [#4943](https://github.com/opensearch-project/ml-commons/pull/4943) | 1 |
| [opensearch-project/OpenSearch](https://github.com/opensearch-project/OpenSearch) | Apache-2.0 | [commit 19b99d8](https://github.com/opensearch-project/OpenSearch/commit/19b99d8300f30732edf15c3c37c6ae5f80106d04) | 1 |
| [quarkiverse/quarkus-web-bundler](https://github.com/quarkiverse/quarkus-web-bundler) | Apache-2.0 | [#496](https://github.com/quarkiverse/quarkus-web-bundler/pull/496) | 1 |
| [quarkusio/quarkus](https://github.com/quarkusio/quarkus) | Apache-2.0 | [#37541](https://github.com/quarkusio/quarkus/pull/37541), [#55429](https://github.com/quarkusio/quarkus/pull/55429) | 2 |
| [Tailormap/tailormap-api](https://github.com/Tailormap/tailormap-api) | MIT | [#1905](https://github.com/Tailormap/tailormap-api/pull/1905) | 1 |
| [TimefoldAI/timefold-solver](https://github.com/TimefoldAI/timefold-solver) | Apache-2.0 | [#2577](https://github.com/TimefoldAI/timefold-solver/pull/2577) | 2 |
| [traveltime-dev/traveltime-sdk-java](https://github.com/traveltime-dev/traveltime-sdk-java) | MIT | [#234](https://github.com/traveltime-dev/traveltime-sdk-java/pull/234) | 1 |
| [undertow-io/undertow](https://github.com/undertow-io/undertow) | Apache-2.0 | [#1023](https://github.com/undertow-io/undertow/pull/1023) | 1 |
| [wso2/wso2-pax-logging](https://github.com/wso2/wso2-pax-logging) | Apache-2.0 | [#40](https://github.com/wso2/wso2-pax-logging/pull/40) | 2 |

Three patches quote upstream source lines that contain an em dash; they are kept exactly as the projects wrote them.

## Adding a fixture

1. Find a merged commit or pull request in a Java project whose license the GitHub API reports as permissive (Apache-2.0, MIT, BSD, ISC). For an accept fixture, look first for an IDoFT row at the pinned commit whose Status is not `Accepted`; otherwise use a merged pull request and confirm that neither it nor its test class appears in `pr-data.csv`.
2. Fetch the diff read only, for example `gh pr diff <number> -R <owner>/<repo>` or `gh api -H "Accept: application/vnd.github.diff" repos/<owner>/<repo>/commits/<sha>`, and record the merge commit and license with `gh api repos/<owner>/<repo>/pulls/<number>`.
3. Keep the file headers and only the whole hunks that matter. If a hunk has to be edited, keep every context and removed line as it was, set `derivation` to `"adapted"`, and describe the edit in `notes`.
4. Check that the patch parses: `git apply --stat <id>/patch.diff`.
5. Write `meta.json` with every key above. Decide `expected`, `reject_kind` and `rejected_by` from the rules in this file, and say in `notes` which rule decides it.
