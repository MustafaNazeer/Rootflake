# iDFlakies

Checked on 2026-10-12 against the repository at commit `d9474c664fb3f1215763dc9ac7336c81520f20dd` (the head of `master`, committed 2026-10-04) and against Maven Central.

## Primary sources

* Repository: https://github.com/UT-SE-Research/iDFlakies. The older address https://github.com/idflakies/iDFlakies returns a permanent redirect (HTTP 301) to it.
* README: https://github.com/UT-SE-Research/iDFlakies/blob/d9474c664fb3f1215763dc9ac7336c81520f20dd/README.md
* Maven Central metadata: https://repo1.maven.org/maven2/edu/illinois/cs/idflakies-maven-plugin/maven-metadata.xml
* Paper: Wing Lam, Reed Oei, August Shi, Darko Marinov, Tao Xie. "iDFlakies: A Framework for Detecting and Partially Classifying Flaky Tests." ICST 2019, pages 312 to 322. DOI https://doi.org/10.1109/ICST.2019.00038 (title and pages confirmed through Crossref). PDF: http://mir.cs.illinois.edu/winglam/publications/2019/LamETAL19iDFlakies.pdf

## What it does

From the README: "tools for detecting/classifying flaky tests". From the paper's abstract and introduction: iDFlakies detects flaky tests by reordering and rerunning a project's tests, and partially classifies them as likely order dependent (OD) or non order dependent (NOD). The paper states the tool "does not further classify the causes of the flaky tests". It is a Maven plugin for Java projects with JUnit tests; the README also documents Gradle use in `README-gradle.md`.

The paper defines OD tests as flaky tests "whose only source of non-determinism is order dependencies", and NOD tests as all other flaky tests. A test that passes and fails in the same order is NOD by definition.

## Current release on Maven Central

| Field | Value |
|---|---|
| groupId | `edu.illinois.cs` |
| artifactId | `idflakies-maven-plugin` |
| Latest version | `2.0.0` (the only version published) |
| Release date | 2022-07-12 (Last-Modified of the 2.0.0 POM on repo1.maven.org, and the Central search timestamp) |

The repository's POMs are at `2.0.1-SNAPSHOT`, which is not on Maven Central. The README's manual pom example uses `2.0.1-SNAPSHOT`; using it requires building the plugin locally.

License: the root POM declares the University of Illinois/NCSA Open Source License.

## Platform and version requirements

* **Operating system.** The README says: "Linux based systems only (in Windows system there are some problems)."
* **Java.** The README does not state a supported Java range. The root POM compiles with `<source>8</source>` and `<target>8</target>`, and every GitHub Actions workflow in the repository sets up only JDK `8.0.292+10`. Support for Java 11, 17, or newer is **unverified** from the primary source.
* **Maven and Surefire.** The README does not state a minimum Maven version or a Surefire requirement. The released 2.0.0 POM declares no `<prerequisites>`. **Unverified.**

## Documented invocation

Detection, with the plugin added to the project's pom:

```shell
mvn idflakies:detect -Ddetector.detector_type=random-class-method -Ddt.randomize.rounds=10 -Ddt.detector.original_order.all_must_pass=false
```

Detection without modifying the pom, using the released version:

```shell
mvn edu.illinois.cs:idflakies-maven-plugin:2.0.0:detect -Ddetector.detector_type=random-class-method -Ddt.randomize.rounds=10 -Ddt.detector.original_order.all_must_pass=false
```

Options documented in the README:

* `detector.detector_type`: the configurations described in the paper; README default `random` (`random-class-method`).
* `dt.randomize.rounds`: number of test suite runs; default `20`.
* `dt.detector.original_order.all_must_pass`: default `true`, meaning iDFlakies exits if it does not see all tests pass in one of three runs of the original order.
* `dt.original.order`: a file listing the exact tests to run, one fully qualified name per line.

The README also offers `pom-modify/modify-project.sh` to add the plugin to a pom automatically, and a Docker based batch script, `scripts/docker/create_and_run_dockers.sh`.

Output relevant to IDoFT: the IDoFT README asks contributors of OD tests to attach `flaky-list.json` and `original-order` from the `.dtfixingtools` directory that iDFlakies creates.

## iFixFlakies is bundled

The README states "iDFlakies now includes iFixFlakies" and documents two more goals, `idflakies:minimize` and `idflakies:fix`. The released 2.0.0 jar does not contain them: its plugin descriptor (`META-INF/maven/plugin.xml`) declares only `bulk`, `detect`, `help`, `replay`, and `testTime`. They exist only in the unreleased `2.0.1-SNAPSHOT`. See [ifixflakies.md](ifixflakies.md).
