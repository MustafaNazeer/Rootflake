# Source notes

Notes on the datasets, tools, and papers Rootflake builds on. Each note is written from the primary source (the repository at a pinned commit, the paper, or Maven Central), gives the link, and records the date it was checked. Anything the primary source does not establish is marked **unverified**.

| Note | Subject | Last checked |
|---|---|---|
| [idoft.md](idoft.md) | IDoFT dataset: `pr-data.csv` columns, Status values, category labels and their definitions, counts | 2026-10-12 |
| [idflakies.md](idflakies.md) | iDFlakies detector: Maven coordinates and release, platform limits, invocation | 2026-10-12 |
| [nondex.md](nondex.md) | NonDex detector: Maven coordinates and release, Java and Maven requirements, invocation | 2026-10-12 |
| [ifixflakies.md](ifixflakies.md) | iFixFlakies: victim and brittle definitions behind OD-Vic and OD-Brit, how it repairs order dependent tests | 2026-10-12 |
| [flakyfix.md](flakyfix.md) | FlakyFix (arXiv 2307.00012): what it claims and how it measured it | 2026-10-12 |
| [test-library-semantics.md](test-library-semantics.md) | Surefire and Failsafe rerun, skip, and filter settings and rerun XML; JUnit 5, JUnit 4, TestNG, AssertJ, JSONAssert, Awaitility, and Mockito behaviors the shortcut checker relies on | 2026-10-14 |

## Pinned versions at a glance

| Tool | Coordinates | Latest on Maven Central | Released |
|---|---|---|---|
| iDFlakies | `edu.illinois.cs:idflakies-maven-plugin` | 2.0.0 | 2022-07-12 |
| NonDex | `edu.illinois:nondex-maven-plugin` | 2.2.5 | 2026-03-23 |

Versions and counts change. Recheck a note against its source before relying on it for a pinned dependency or a reported number, and update its checked date when you do.
