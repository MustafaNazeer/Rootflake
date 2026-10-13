# NonDex

Checked on 2026-10-12 against the repository at commit `f1f407ce3e026f597567e7d96ee08cb08875690f` (the head of `master`, committed 2026-03-23) and against Maven Central.

## Primary sources

* Repository: https://github.com/TestingResearchIllinois/NonDex
* README: https://github.com/TestingResearchIllinois/NonDex/blob/f1f407ce3e026f597567e7d96ee08cb08875690f/README.md
* Supported APIs list (linked from the README): https://github.com/TestingResearchIllinois/NonDex/wiki/Supported-APIs
* Maven Central metadata: https://repo1.maven.org/maven2/edu/illinois/nondex-maven-plugin/maven-metadata.xml
* Paper (cited by IDoFT for the ID label): August Shi, Alex Gyori, Owolabi Legunsen, Darko Marinov. "Detecting Assumptions on Deterministic Implementations of Non-deterministic Specifications." ICST 2016, pages 80 to 90. DOI https://doi.org/10.1109/ICST.2016.40 (confirmed through Crossref). PDF: http://mir.cs.illinois.edu/marinov/publications/ShiETAL16NonDex.pdf

## What it does

From the README: NonDex "is a tool for detecting and debugging wrong assumptions on under-determined Java APIs", for example assuming that iteration over a `java.util.HashMap` has a deterministic order. It "explores different behaviors of under-determined APIs and reports test failures under different explored behaviors", and "only explores behaviors that are allowed by the specification". IDoFT labels tests found this way as ID.

License: MIT (root `LICENSE` file).

## Current release on Maven Central

| Field | Value |
|---|---|
| groupId | `edu.illinois` |
| artifactId | `nondex-maven-plugin` |
| Latest version | `2.2.5` |
| Release date | 2026-03-23 (Last-Modified of the 2.2.5 POM on repo1.maven.org; `maven-metadata.xml` lastUpdated 20260323064903) |
| Previous version | `2.2.1`, 2024-12-03 |

Published versions: 1.0.0, 1.0.1, 1.1.0, 1.1.1, 1.1.2, 2.1, 2.1.1, 2.1.7, 2.2.1, 2.2.5.

The README still shows `2.2.1` in every command and pom example. The newest Git tag is `nondex-2.2.1`; there is no tag or GitHub release for 2.2.5. The Maven Central search API (search.maven.org) did not yet list 2.2.5 when checked, while `maven-metadata.xml` on repo1 did. I treat repo1 as authoritative.

## Platform and version requirements

The README's Prerequisites section, quoted:

```
- Java 8 ~ 21 (Oracle JDK, OpenJDK).
- Maven 3.6+ and Surefire present in the POM (for the NonDex Maven plugin).
- Gradle 5.0+ (for the NonDex Gradle plugin).
```

* **Java.** Documented range is 8 to 21. The repository's CI (`.github/workflows/ci.yml`) builds on JDK 8, 11, 17, 21, and 25 on `ubuntu-latest`, with a second job on 8, 11, 17, and 21. The README has not been updated to claim 25, so I take 8 to 21 as the supported range.
* **Maven and Surefire.** Maven 3.6 or newer, with Surefire present in the project's POM.
* **Operating system.** The README states no OS restriction for the Maven plugin. The repository carries an `appveyor.yml` alongside the GitHub Actions workflow. The README notes only that the Gradle helper script `add-nondex.sh` works "on Linux or MacOS".
* **Java 9 and newer on the command line.** For direct command line use (outside Maven), the README says `-Xbootclasspath/p:` works for Java 8 and must be replaced by `--add-exports ... --patch-module java.base=` on Java 9 or newer.

## Documented invocation

Without modifying the pom (use `-Dtest=...` to select individual tests):

```shell
mvn edu.illinois:nondex-maven-plugin:2.2.1:nondex
mvn edu.illinois:nondex-maven-plugin:2.2.1:debug
mvn edu.illinois:nondex-maven-plugin:2.2.1:help
```

With the plugin added under `<build><plugins>`:

```shell
mvn nondex:nondex
mvn nondex:debug
mvn nondex:help
```

Output: flaky tests are reported under a `NonDex SUMMARY:` section and logged as `failure` files under `.nondex/`, keyed by execution ID. The `debug` goal reports the command line arguments that reproduce a failure and writes `debug` files naming the single invocation point that makes the test fail.

The IDoFT README asks contributors of ID tests to include `nondexMode` and `nondexSeed` from `.nondex/{testid}/config`, which is what makes a NonDex detection reproducible.
