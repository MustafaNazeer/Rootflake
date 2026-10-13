# IDoFT, the International Dataset of Flaky Tests

Checked on 2026-10-12 against the repository at commit `4903dc9233eead4e4191c66f9ed0b14eea091052` (the head of `main`, committed 2026-05-04).

## Primary sources

* Repository: https://github.com/TestingResearchIllinois/idoft
* README (the file is lowercase `readme.md`): https://github.com/TestingResearchIllinois/idoft/blob/4903dc9233eead4e4191c66f9ed0b14eea091052/readme.md
* Java Maven data: https://github.com/TestingResearchIllinois/idoft/blob/4903dc9233eead4e4191c66f9ed0b14eea091052/pr-data.csv
* Website built from the data: http://mir.cs.illinois.edu/flakytests

## What it is

The README describes the repository as "all the data used to build the flaky tests' website". It holds three data files: `pr-data.csv` for Java projects built with Maven, `gr-data.csv` for Java projects built with Gradle, and `py-data.csv` for Python. Rootflake uses only `pr-data.csv`. The repository also contains `odr-tests.csv`, `tic-fic-data.csv`, `tso-iso-rates.csv`, and several maintenance scripts; I have not reviewed those and Rootflake does not depend on them.

The README names Wing Lam as the author of the dataset and asks users to cite the website and the iDFlakies ICST 2019 paper (BibTeX in the README).

**License.** The GitHub API reports no license for the repository, and the root listing has no LICENSE file. Redistribution terms for the data are therefore not stated by the source. See "Open points" below.

## `pr-data.csv` columns

The header row, copied exactly:

```
Project URL,SHA Detected,Module Path,Fully-Qualified Test Name (packageName.ClassName.methodName),Category,Status,PR Link,Notes
```

The README's own definitions, summarized:

| Column | Definition in the README |
|---|---|
| Project URL | The GitHub project where the flaky test was found. |
| SHA Detected | The commit SHA where the flaky test was detected; "may or may not be the latest SHA". |
| Module Path | Path of the Maven module that contains the test; `.` when the test is at the repository root. |
| Fully-Qualified Test Name (packageName.ClassName.methodName) | Usually `packageName.ClassName.methodName`; for Cucumber or parameterized tests it carries the full test description. |
| Category | One or more category labels, separated by `;`, "sorted alphabetically, and contains no spaces". `UD` when the category is unknown. |
| Status | Exactly one status value at a time; blank means detected but not yet inspected. |
| PR Link | The pull request in the project that fixes the test. |
| Notes | "The only acceptable values are URLs", usually a link to an IDoFT issue with reproduction details. |

The README states there should be only one row per tuple of Project URL, Module Path, and Fully-Qualified Test Name.

Observed in the data, not stated in the README: some multi label values are not alphabetically sorted (for example `NDOD;UD;NOD` and `TD;NDOD`), so a parser must not rely on the documented sort order.

## Category labels and IDoFT's definitions

Quoted from the README table, with the reference each one links to:

| Label | IDoFT description | Source the README cites |
|---|---|---|
| OD | "Order-Dependent flaky tests as defined in iDFlakies" | iDFlakies, ICST 2019 |
| OD-Brit | "Order-Dependent Brittle tests as defined in iFixFlakies" | iFixFlakies, ESEC/FSE 2019 |
| OD-Vic | "Order-Dependent Victim tests as defined in iFixFlakies" | iFixFlakies, ESEC/FSE 2019 |
| ID | "Implementation-Dependent Tests found by Nondex" | NonDex, ICST 2016 |
| ID-HtF | "Implementation-Dependent tests that are hard to fix" | https://github.com/kaiyaok2/ID-HtF |
| NIO | "Non-Idempotent-Outcome Tests ... Tests that pass in the first run but fail in the second." | Wei et al., ICSE 2022 |
| NOD | "Non-Deterministic tests" | none given |
| NDOD | "Non-Deterministic Order-Dependent tests that fail non-deterministically but with significantly different failure rates in different orders" | Lam et al., ISSRE 2020 |
| NDOI | "Non-Deterministic Order-Independent tests that fail non-deterministically but similar failure rates in all orders" | Lam et al., ISSRE 2020 |
| UD | "Unknown Dependency tests that pass and fail in a test suite or in isolation" | none given |
| OSD | "Operating System Dependent tests that pass and fail depending on the operating system" | none given |
| TD | "Time-Dependent flaky tests as defined in FlakeSync" | FlakeSync, ICSE 2024 |
| TZD | "Tests that fail in machines on different time zones, usually failing time-related assertions" | none given |

That is 13 labels. The precise victim and brittle definitions behind OD-Vic and OD-Brit are in [ifixflakies.md](ifixflakies.md).

## Status values and IDoFT's definitions

The README lists blank plus 17 named values. Summarized from the README:

| Status | Meaning |
|---|---|
| (blank) | Detected, not yet inspected. |
| Opened | A PR was opened to fix the test. |
| Accepted | A PR to fix the test was accepted. |
| InspiredAFix | The work inspired a developer fix without directly changing code; PR Link points at the developer's PR. |
| DeveloperWontFix | Developers said they do not want a fix. |
| DeveloperFixed | A developer fixed the test before a PR was made. |
| Deleted | The test was removed from the repository after detection. |
| Rejected | The PR was rejected or closed because developers did not think a fix was necessary. |
| RepoRenamed | The repository was renamed; set on rows with the old name. |
| Skipped | Inspected and should not be fixed (for example annotated `@Ignore`). |
| Irreproducible | The test cannot be reproduced or rerun. |
| MovedOrRenamed | The test has a different fully qualified name at a newer SHA; set on the older row. |
| RepoArchived | The repository is archived on GitHub. |
| Deprecated | The repository is marked deprecated. |
| RepoDeleted | The repository returns 404. |
| MovedToGradle | The repository moved from Maven to Gradle. |
| FixedOrder | The test has a fixed order, for example `@Order` in JUnit 5. |
| Unmaintained | No commits to the main branch in the past 2 years. |

## Counts at the checked commit

Computed by parsing `pr-data.csv` (SHA256 `4d9a8b9a0cbb597792efe328f4bf29300de585681b77d97fdaa98c90d96f93fb`) with Python's `csv` module. These are a snapshot; the file changes as contributors add rows.

* 8,075 data rows.
* Status counts: Accepted 2,303; blank 1,961; Opened 1,508; Deleted 459; DeveloperFixed 340; DeveloperWontFix 334; Unmaintained 304; MovedOrRenamed 245; RepoArchived 204; InspiredAFix 162; Rejected 105; MovedToGradle 93; Deprecated 19; Skipped 14; RepoRenamed 8; RepoDeleted 8; Irreproducible 6; FixedOrder 2.
* Label occurrences over all rows (a multi label row counts once per label): ID 5,201; OD 1,081; NOD 700; OD-Vic 505; NIO 231; UD 185; TD 178; NDOD 89; OD-Brit 33; NDOI 8; TZD 5; OSD 1; ID-HtF 0.
* Label occurrences among the 2,303 Accepted rows: ID 1,769; OD-Vic 235; NIO 159; OD 104; NOD 34; OD-Brit 12; UD 7; TZD 4. No Accepted row carries TD, NDOD, NDOI, OSD, or ID-HtF.

## Things the data does not contain

* No column records the Java version a project needs. Any filter on Java version has to be determined by building the project.
* No column records the fixing commit; the fix is reached through PR Link.
