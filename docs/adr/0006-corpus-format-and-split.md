# ADR 0006: Corpus format, redistribution, and the dev and test split

Status: Accepted (2026-10-12)
Date: 2026-10-12

## Context

Every published Rootflake number is computed over a corpus of 150 flaky tests drawn from IDoFT's `pr-data.csv`: a dev set of 50 that I use freely for tuning, and a test set of 100 that is frozen and run only on a tagged version ([architecture](../architecture.md)). Before the corpus builder exists, six things have to be fixed in writing, because changing any of them after a draw would make earlier numbers incomparable:

1. what the committed corpus may contain, given that IDoFT declares no license;
2. the file format, the schema, how a corpus version is named, and its provenance record;
3. the categories included and the filtering funnel, with every exclusion reason recorded;
4. how 150 tests are allocated across strata and drawn, reproducibly;
5. what the headline numbers estimate and which statistics are reported;
6. how the drawn files are frozen and how later versions relate to earlier ones.

### Facts this ADR relies on

All checked on 2026-10-12 against IDoFT commit `4903dc9233eead4e4191c66f9ed0b14eea091052`, which `git ls-remote` reports as the head of `main` on that date. I downloaded `pr-data.csv` at that commit and parsed it with Python's `csv` module. Its SHA256 is `4d9a8b9a0cbb597792efe328f4bf29300de585681b77d97fdaa98c90d96f93fb`, matching the value in [the IDoFT source note](../source/idoft.md).

* **License.** The GitHub API returns `"license": null` for the repository and 404 for its license endpoint, and the root listing has no LICENSE file. The README asks users of the dataset to cite the website and the original dataset paper; it states no other terms.
* **What "no license" means on GitHub.** GitHub's licensing page says that without a license "the default copyright laws apply", that "no one may reproduce, distribute, or create derivative works from your work", and that for a public repository "other users of GitHub.com have the right to view and fork your repository". I am not drawing a legal conclusion from this; I am treating it as a reason to copy as little as possible.
* **Rows.** 8,075 data rows, 2,303 with Status `Accepted`. Every Accepted row has a 40 character `SHA Detected` and a `PR Link` of the form `https://github.com/OWNER/REPO/pull/N` under the row's own Project URL.
* **Accepted label occurrences** (a multi label row counts once per label): ID 1,769; OD-Vic 235; NIO 159; OD 104; NOD 34; OD-Brit 12; UD 7; TZD 4; TD 0. No Accepted row carries TD, NDOD, NDOI, OSD, or ID-HtF.
* **Multi label rows.** 21 Accepted rows carry more than one label: `ID;NIO` 6, `NIO;OD-Vic` 4, `OD;UD` 3, `NIO;NOD` 3, and one each of `ID;NOD`, `NIO;OD`, `NOD;OD-Brit`, `NIO;UD`, `ID;OD-Vic`. None of them combines only labels from the OD family.
* **Duplicates.** The README says there should be only one row per tuple of Project URL, Module Path, and fully qualified test name. Among Accepted rows, 9 such tuples appear twice (18 rows). In all 9 the two rows point at different PRs; in 6 they were detected at different SHAs; in 4 they carry different categories.
* **Fixes are shared.** The 2,303 Accepted rows point at only 647 distinct PRs. One PR fixes up to 101 ID tests, 87 OD-Vic tests, and 72 NIO tests.
* **Fix diffs live in the target projects.** A PR's diff is code from the target project, under that project's own license, which varies from project to project (for one sampled TZD project the GitHub API reports `NOASSERTION`). For a sampled PR, the GitHub API returned `merged: true`, `merged_at`, `merge_commit_sha`, and `head.sha`; the head commit belonged to a fork, yet `refs/pull/N/head` was still fetchable from the base repository.
* **Test names.** 4 Accepted test names contain a comma and 6 contain a space (parameterized and Cucumber style names), so a hand written CSV would need careful quoting.
* **No Java column.** `pr-data.csv` records no Java version; the build has to tell us.

## Decision 1: Redistribution

### Options

**A. Identifiers only, everything else fetched at build time.** The committed corpus carries, per test, only identifying facts: Project URL, SHA Detected, Module Path, fully qualified test name, IDoFT category, and PR Link, plus the IDoFT commit and the SHA256 of `pr-data.csv` it came from. It also carries values Rootflake derives itself: the PR's merge commit SHA, head SHA, and merge time from the GitHub API, the SHA256 of the canonical fix diff, and the build outcome. The diff itself is never committed; the builder fetches it from the target repository and checks it against the recorded hash.

**B. Full rows.** Commit the selected IDoFT rows verbatim, including Status and Notes. This adds nothing a run needs (Status is constant after filtering, and Notes are links to IDoFT issues), and it copies more of an unlicensed file than necessary.

**C. Derived data including fix diffs.** Commit the human fix diffs next to the rows, so the corpus is self contained. This redistributes code from up to 150 projects under as many licenses, some of them unstated, and it makes the human fixes trivially available to anything that reads the repository, including retrieval over the fix history.

### What can be committed, by source

| Source | Owner and terms | Committed? |
|---|---|---|
| Row identifiers (URL, SHA, module, test name, PR link) | Facts that point into public repositories; taken from IDoFT | Yes, for the 150 drawn rows only, with attribution |
| IDoFT category label | IDoFT's classification; needed as ground truth for diagnosis accuracy | Yes, for the drawn rows, with attribution |
| IDoFT Status and Notes | IDoFT | No |
| Excluded rows | IDoFT | Only as row numbers in `pr-data.csv` at the pinned commit plus a reason; never row contents |
| Merge commit SHA, head SHA, merge time | GitHub metadata of the target project | Yes |
| Human fix diff | Each target project, under its own license | No; fetched at build time, only its SHA256 is committed |
| Build outcomes, JDK used, timings | Produced by Rootflake | Yes |

### Decision

**Option A.** It is the most conservative choice that still makes every reported number reproducible: anyone with the IDoFT commit, the committed corpus files, and network access to GitHub can rebuild the exact checkout, fetch the same fix, and verify it byte for byte against the committed hash. The README credits IDoFT and cites the paper the IDoFT README asks for.

The human fix is shown on the dashboard's per test page. Under Option A the dashboard links to the PR rather than embedding the diff, unless I later confirm the project's license permits redistribution and add its notice; that choice is deferred to the dashboard's own ADR.

### Optional request to the IDoFT maintainers

If I ask, it would be through an issue on the IDoFT repository, worded like this:

> Hello, and thank you for maintaining IDoFT. I am building an open source tool that tries to repair flaky Java tests and evaluates itself on a sample of 150 Accepted rows from `pr-data.csv` at commit 4903dc9. The repository has no license file, so I would like to check what you are comfortable with. My plan is to commit only the identifying columns of the sampled rows (Project URL, SHA Detected, Module Path, test name, Category, PR Link), cite the website and the dataset paper as your README asks, and fetch the fixes from the original PRs at build time rather than redistributing them. Is that acceptable to you, and would you consider adding a license (for example CC BY 4.0 for the data) so others can reuse the dataset with clear terms? Thanks again.

Option A does not depend on an answer. A reply that permits more would be recorded here as an amendment; a reply that permits less would be followed.

## Decision 2: File format

### Options

* **CSV per split.** Familiar and diffable, but types are implicit, nulls are ambiguous, and test names containing commas need quoting rules.
* **JSON Lines per split.** One JSON object per line, explicit types and nulls, safe for any test name, and BigQuery loads newline delimited JSON directly, which matches how run results are already stored.
* **Parquet.** Typed and compact, but binary, so a reviewer cannot read a diff of it on GitHub.

### Decision

**JSON Lines, one file per split, plus a manifest and a funnel file.** A corpus version is a directory:

```
corpus/v1/
  manifest.json   provenance, rules, seed, allocation, file hashes, funnel counts
  dev.jsonl       50 rows
  test.jsonl      100 rows
  funnel.jsonl    one row per examined IDoFT row number with its outcome
```

Versions are named `v1`, `v2`, and so on, and the string `corpus-v1` is what every eval run records, together with the SHA256 of the split file it ran on. Versions are never deleted.

**Serialization**, so rebuilding produces identical bytes: UTF-8 without a byte order mark, LF line endings, one object per line, keys sorted, no insignificant whitespace (Python `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`), rows sorted by `idoft_row`, and a final newline. `manifest.json` uses the same settings with two space indentation.

### Split file schema (`dev.jsonl`, `test.jsonl`)

| Field | Type | Meaning |
|---|---|---|
| `idoft_row` | integer | 1 based data row number in `pr-data.csv` at the pinned commit, header excluded |
| `project_url` | string | IDoFT Project URL |
| `sha_detected` | string, 40 hex | IDoFT SHA Detected; the checkout point |
| `module_path` | string | IDoFT Module Path; `.` for the root |
| `test_name` | string | IDoFT fully qualified test name, verbatim |
| `test_class` | string | Fully qualified class derived from `test_name` (below) |
| `category` | string | IDoFT Category, verbatim; the ground truth for scoring |
| `stratum` | string | The sampling stratum after the fold in Decision 3, for example `"OD"` for an `OD-Vic` row |
| `pr_url` | string | IDoFT PR Link |
| `pr_head_sha` | string, 40 hex | PR head commit, from the GitHub API |
| `pr_merge_commit_sha` | string, 40 hex | Merge commit, from the GitHub API |
| `pr_merged_at` | string, RFC 3339 UTC | Merge time; used for the training cutoff split |
| `fix_diff_sha256` | string, 64 hex | Hash of the canonical fix diff (below) |
| `jdk` | string | JDK major version the sandbox build succeeded with, for example `"11"` |
| `rank_key` | string, 64 hex | The seeded rank hash that placed this row (Decision 4) |
| `split` | string | `"dev"` or `"test"` |

**Canonical fix diff:** `git diff --no-color --no-ext-diff M H`, where `H` is `pr_head_sha` fetched through `refs/pull/N/head` and `M` is `git merge-base H B` for the PR's base commit `B`. Computing it locally from commits, instead of hashing GitHub's rendered `.diff`, avoids depending on how GitHub formats diffs over time.

**Test class:** `test_name` is cut at its first character outside `[A-Za-z0-9_$.]` (which removes parameter and Cucumber suffixes), and the last dot separated segment of what remains (the method) is dropped. The class key used by the split rule in Decision 4 is `project_url` plus `test_class`. The derivation is unit tested against every Accepted test name at the pinned commit.

### Manifest (`manifest.json`)

| Field | Content |
|---|---|
| `corpus_version` | `"corpus-v1"` |
| `source` | IDoFT repository URL, commit SHA, file path `pr-data.csv`, file SHA256, date fetched |
| `citation` | The citation the IDoFT README requests |
| `inclusion` | The included and excluded IDoFT labels (Decision 3) |
| `fold_map` | Every included IDoFT label and the stratum it maps to |
| `filters` | The ordered funnel rules from Decision 3 with their parameters: supported JDKs, `R`, project cap, minimum stratum size |
| `allocation` | Method, parameters (floor, minimum stratum size, dev fraction), the planned per stratum dev and test counts, every shortfall and reallocation, and the final counts |
| `seed` | The seed string and the rank function, written out |
| `clustering` | Drawn tests per project per stratum, drawn tests per class, and the number of projects present in both splits |
| `builder` | Git commit of the builder and the SHA256 of `uv.lock` |
| `sandbox_image` | Digest of the sandbox image used for the checks |
| `files` | SHA256 of `dev.jsonl`, `test.jsonl`, and `funnel.jsonl` |
| `funnel_counts` | Rows removed at each rule, overall and per stratum |
| `created_at` | UTC timestamp, excluded from any reproducibility comparison |

## Decision 3: Categories and the filtering funnel

### Inclusion list and the OD fold

The inclusion list is the set of categories the pipeline is built to handle:

* **Included:** ID; the OD family (`OD`, `OD-Vic`, `OD-Brit`); NIO; NOD; TZD.
* **Excluded:** TD (no Accepted rows), UD (unknown cause, so there is no label to diagnose against), OSD (needs another operating system), ID-HtF (hard to fix by definition, and no Accepted rows), NDOD and NDOI (no Accepted rows).

For sampling, `OD`, `OD-Vic`, and `OD-Brit` are folded into one `OD` stratum, because `OD-Brit` alone is too thin to stand as a stratum and the three share a detection method. The fold map is recorded in the manifest. Scoring keeps the verbatim IDoFT category: diagnosis accuracy is computed at family level for every test, and at fine level (victim or brittle) only for tests whose IDoFT label is `OD-Vic` or `OD-Brit`. A test labelled plain `OD` is scored at family level only.

The inclusion list is not the same thing as the strata that end up in the corpus. A category can be included and still be removed by the thin stratum rule in Decision 4: at the pinned commit TZD has 3 eligible PRs and drops out, and NOD (7 eligible PRs) may fall short after the build filter. Both effects are reported as limitations, separately.

### Funnel

Each row is assigned the first rule it fails, in this fixed order, so every row has exactly one outcome and the counts add up to 8,075.

| Order | Reason code | Rule |
|---|---|---|
| 1 | `status_not_accepted` | Status is not exactly `Accepted` |
| 2 | `duplicate_test` | Another Accepted row has the same Project URL, Module Path, and test name; every copy is excluded |
| 3 | `multiple_labels` | Category holds more than one label |
| 4 | `category_excluded` | The label is not on the inclusion list |
| 5 | `stratum_too_thin` | The row's stratum has fewer eligible PRs than the minimum in Decision 4 |
| 6 | `same_pr_already_drawn` | Another test from the same PR is already drawn |
| 7 | `project_cap_reached` | Two tests from the same project are already drawn in this stratum |
| 8 | `class_in_other_split` | The row's class key is already drawn into the other split |
| 9 | `fix_diff_unavailable` | The PR is not merged according to the GitHub API, or its head commit cannot be fetched, or the canonical diff cannot be computed |
| 10 | `java_out_of_range` | The project's declared compiler release or target is outside the supported JDK range |
| 11 | `build_failed` | The project does not build at `sha_detected` in the sandbox with any supported JDK, after one retry for infrastructure errors |
| 12 | `test_not_runnable` | The test is not discovered at `sha_detected`, or it never passes in its passing configuration within `R` runs |
| 13 | `not_examined` | Never reached, because its stratum's quota filled first |
| 14 | `drawn` | In the corpus |

**Why duplicates are excluded.** The duplicates are mostly separate episodes of flakiness: in all 9 pairs the rows point at different PRs, 6 were detected at different SHAs, and 4 carry different categories. For such a test, both the ground truth label and the human fix are ambiguous, and the two rows could land in different splits, putting the same test in dev and in test. Excluding every copy removes 18 rows and all of that ambiguity.

**Why multi label rows are excluded.** A test can genuinely have more than one cause: NIO tests, for example, can also be order dependent victims or polluters, which is what `NIO;OD-Vic` and `NIO;OD` record. If any of a row's labels counted as a correct diagnosis, multi label rows would be easier to score as correct and would inflate diagnosis accuracy. Excluding them keeps one unambiguous label per test, at a cost of 15 rows (the other 6 multi label rows are already removed as duplicates).

**Passing configuration for rule 12.** Rule 12 checks only that the test can pass, in the configuration where it is expected to: the original module test order for the OD family, a run without NonDex for ID, the first run for NIO, the default time zone for TZD, and any run for NOD. `R` is a named parameter whose value is set once the pass rates of real targets in the sandbox have been measured; until then it has no value, and the corpus is not drawn. Its value is recorded in the manifest. Observing the flaky failure is deliberately not a filter: whether Rootflake can reproduce a failure is an eval outcome (not reproduced), and filtering on it would remove exactly the hard cases.

**Evaluation order.** Rules 1 to 5 are cheap and run on every row. Rules 6 to 12 run lazily during the ranked walk in Decision 4, in table order, so the cheap walk constraints (6 to 8) are checked before any network access or sandbox build (9 to 12). That is why `not_examined` exists, and it is published rather than hidden. The supported JDK range is decided by testing the sandbox, not here; this ADR only records whatever range is in force in the manifest.

At the pinned commit, rules 1 to 3 remove 5,772, 18, and 15 rows, and rule 4 removes the 3 single label UD rows, leaving 2,267 eligible rows. Rule 5 then removes the 4 TZD rows. `funnel.jsonl` lists every Accepted row by `idoft_row` with its reason code; the 5,772 rows removed by rule 1 appear only as a count, since listing them adds nothing.

## Decision 4: Allocation and the seeded draw

### The sampling unit is the fix PR, not the row

Because one PR can fix dozens of tests, sampling rows would fill the corpus with near copies of the same fix and would let one fix appear in both dev and test, where tuning on the dev copy leaks the answer to the test copy. So the unit is a PR within a stratum, and **each PR contributes at most one test to the whole corpus**.

Two further clustering limits apply during the walk:

* **Project cap:** at most 2 drawn tests per project per stratum (rule 7).
* **Class split:** no class key (project plus fully qualified class) appears in both dev and test (rule 8). Tests in the same class share fixtures and often share a cause, so tuning on one would leak into the other.

After rules 1 to 4 at the pinned commit, with the OD fold:

| Stratum | Eligible rows | Eligible PRs | Projects | PRs reachable under the project cap |
|---|---|---|---|---|
| ID | 1,754 | 527 | 232 | 327 |
| OD (OD, OD-Vic, OD-Brit) | 338 | 70 | 44 | 55 |
| NIO | 143 | 30 | 9 | 14 |
| NOD | 28 | 7 | 6 | 7 |
| TZD | 4 | 3 | 2 | 3 |

5 PRs appear in more than one stratum. These counts are before the build and runnable filters, which will lower them.

### Allocation options considered

1. **Proportional to eligible rows.** The headline rate would mirror IDoFT's Accepted mix, but ID would take about three quarters of the corpus and every other category would be too small to say anything about.
2. **Equal per stratum with caps.** Every category visible, but rare categories heavily overweighted and the uncapped strata pushed to the same size regardless of how common they are.
3. **A floor per stratum, the rest proportional.** Each stratum first gets a fixed floor, and the remaining slots follow each stratum's size.

### Decision: floor plus proportional remainder

* **Minimum stratum size:** a stratum needs at least 6 eligible PRs after rules 1 to 4, counted before any build or runnable check, to be drawn at all; below that its rows are removed under rule 5. Six is the smallest size that plans at least 2 dev and 4 test tests. At the pinned commit this removes TZD.
* **Floor:** 12 per stratum, or all of a stratum's eligible PRs if it has fewer.
* **Remainder:** the slots left from 150 are shared among strata in proportion to each stratum's eligible PRs above its floor, capped at that same number.
* **Dev and test within each stratum:** the 50 dev slots are shared among strata in proportion to each stratum's total `k`, capped at `k`; the stratum's test quota is `k` minus its dev quota. Totals are exactly 50 and 100 by construction.

**Largest remainder with iterative capping**, used for both the remainder step and the dev step: give each active stratum the quota `total * weight / sum of active weights`. If any quota reaches its cap, fix those strata at their caps, subtract their caps from the total, remove them from the active set, and recompute the rest. When no quota reaches its cap, give each active stratum the integer part of its quota, then hand the remaining slots one each to the strata with the largest fractional parts. Ties on the fractional part go to the stratum name that comes first in Unicode code point order.

### Illustrative allocation

Applied to the counts after rules 1 to 5 at the pinned commit (OD folded, TZD removed), but before the build, runnable, and project cap filters:

| Stratum | Floor | Remainder | Total `k` | Dev | Test |
|---|---|---|---|---|---|
| ID | 12 | 93 | 105 | 35 | 70 |
| OD | 12 | 11 | 23 | 8 | 15 |
| NIO | 12 | 3 | 15 | 5 | 10 |
| NOD | 7 | 0 | 7 | 2 | 5 |
| Total | 43 | 107 | 150 | 50 | 100 |

This table is illustrative. The real plan is computed by the builder from the same rule and written into the manifest. It already shows one expected shortfall: NIO's 30 PRs come from only 9 projects, so the project cap leaves at most 14 reachable against a quota of 15.

### Shortfall and reallocation

A stratum is short when its ranked list is exhausted before it holds its quota.

1. Because each stratum fills test before dev (step 7 of the draw), a short stratum loses dev slots first. Its lost slots are counted separately as lost test slots and lost dev slots. A short stratum can end with zero dev tests.
2. **Recipients** are the strata not yet walked that still have unexamined eligible PRs. Strata are walked rarest first and a stratum already walked is never reopened, so slots only ever move to strata later in the order.
3. **Weights and caps** for the reallocation are each recipient's number of unexamined eligible PRs remaining at that moment (its eligible PRs minus any claimed by strata already walked). Lost test slots and lost dev slots are reallocated separately with largest remainder and iterative capping, and each reallocated slot keeps the split it was lost from, so the totals stay at 50 and 100 whenever enough PRs exist.
4. If a recipient later runs short as well, its shortfall, including any slots it received, is reallocated in the same way to the strata after it. This repeats down the order.
5. If the last stratum runs short and no recipient remains, the corpus is published smaller than 150. No rule is relaxed to fill it. The manifest records the planned and final dev and test sizes per stratum, and every reported rate uses the final `n`.

Every shortfall and reallocation is recorded in the manifest.

### The seeded draw, exactly

Library samplers are avoided on purpose. Python's documentation states that "most of the random module's algorithms and seeding functions are subject to change across Python versions", and guarantees only two things: a backward compatible seeder, and that `random()` keeps its sequence for the same seed. It makes no such promise for `sample()` or `shuffle()`. A hash ranking is fully specified and can be reimplemented in any language:

1. **Seed.** A string recorded in the manifest. For `corpus-v1` it is `rootflake-corpus-v1`. This ADR, with the seed in it, is committed before any network or sandbox check (rules 9 to 12) runs. The seed is never changed to obtain a different draw; any redraw is a new corpus version with the reason recorded.
2. **Units.** For each stratum, the eligible PR links, as written in IDoFT, sorted in code point order.
3. **Rank.** Each unit's `rank_key` is the lowercase hex SHA256 of the UTF-8 bytes of `seed + "\n" + stratum + "\n" + pr_url`, where `stratum` is the folded name. Units are processed in ascending `rank_key` order, ties (not expected) broken by `pr_url` in code point order.
4. **Strata order.** Strata are walked from fewest eligible PRs to most, ties by name in code point order, so a PR shared between strata is claimed first by the rarer one; later strata skip it under rule 6.
5. **Row within a unit.** If a PR fixes several eligible tests in the stratum, they are ordered by the SHA256 of `seed + "\n" + str(idoft_row)` and examined in that order against rules 7 to 12; the first one to pass all of them is drawn, and the PR's other tests are recorded under rule 6.
6. **Walk.** The builder walks each stratum's ranked units until the stratum holds its quota or runs out. Taking the first eligible units of a uniformly random ordering is a uniform random sample of the units that pass the checks, so lazy checking does not bias the draw while saving hundreds of builds.
7. **Dev and test.** Within a stratum, drawn tests in walk order go to test until the test quota is full, then to dev. Rule 8 is checked against the split the candidate would go to.

**Reproducibility.** Given the IDoFT file, the seed, and the committed `funnel.jsonl` (which records each examined row's rule 9 to 12 outcome), the selection is a pure function and rebuilds byte for byte. Rerunning the checks themselves can differ over time (a dependency disappears from Maven Central, a repository is deleted); a rebuild compares its fresh outcomes with the committed ones and reports any drift instead of silently producing a different corpus.

## Decision 5: Estimand and statistics

* **Estimand.** The verified fix rate is a rate per fix PR: each PR contributes at most one test, so the tests of a PR that fixed many tests are underweighted relative to IDoFT's rows. With the floors and the project cap, the rate describes this stratified corpus, not IDoFT's Accepted population, and the dashboard says so next to the per stratum counts.
* **Headline.** The raw, unweighted fix rate over the test set with a 95% Wilson score interval, reported twice: over all entries, and over reproducible entries. Every outcome count is shown next to it. No weighted or population adjusted figure is reported in this version.
* **Pipeline against baseline.** Paired on the same tests, using the two sided exact binomial McNemar test on the discordant pairs, at the 5 percent level. A difference that is not significant is reported as not significant.
* **Per stratum rates** are shown with their own Wilson intervals. With as few as 5 test set tests in a stratum, those intervals are wide, and they are presented as descriptive, not as findings.

## Decision 6: Freezing and later versions

* Once `corpus/v1/test.jsonl` is committed, it is never edited. Its SHA256 is in the manifest, and every eval run records and checks it before running.
* Any change at all (a different IDoFT commit, inclusion list, fold, filter rule, `R`, allocation parameter, JDK range, or a correction to a single row) produces a new version directory, `corpus/v2/`, with its own manifest and a short note of what changed and why.
* `dev.jsonl` follows the same rule, so that dev results stay comparable across prompt versions.
* A test set row later found to be broken (for example the repository was deleted) stays in the file and is reported under its run outcome; it is not quietly replaced.
* **Later versions.** A PR that was in the dev set of any earlier version never enters a later test set, because I have tuned on it. Test items from an earlier version may be reused in a later one. Results from different corpus versions are never pooled or compared as if they measured the same thing.
* **Test set runs.** Every test set run is logged with the tag it ran on, including runs that are not reported. The tag whose results will be reported is named before its test set run starts; if it is not, every test set run is reported.

## Consequences

* The corpus is small, readable, and carries no third party code. The cost is that building it needs network access to GitHub and Maven Central, and that the human fix is linked rather than shown inline on the dashboard until license handling for that page is decided.
* The fix history that the Diagnoser searches must exclude every dev and test PR in the corpus during evals, matched on `pr_url` and `fix_diff_sha256`; otherwise retrieval could hand the Diagnoser the human fix for the very test it is working on. The rest of the rule belongs to the history index design.
* `pr_merged_at` makes the training cutoff split from [ADR 0001](0001-gemini-model-and-price.md) possible without another lookup.
* Two separate limitations are reported: categories excluded from the inclusion list, and included categories that did not survive as strata (TZD at the pinned commit, and NOD if the build filter leaves it short). An included category with no stratum has no test set result.
* `R` must be measured and recorded before the draw; until then the corpus cannot be built.
* Some projects can still appear in both dev and test through different classes and PRs. The manifest reports how many do.
* The builder is written test first: the funnel order, the class key derivation, the largest remainder allocation with capping, the shortfall reallocation, the rank function, and the serialization each get unit tests, including a check that two runs over the same inputs produce identical bytes.

## Sources

All checked on 2026-10-12.

* IDoFT repository: https://github.com/TestingResearchIllinois/idoft
* IDoFT README at the pinned commit: https://github.com/TestingResearchIllinois/idoft/blob/4903dc9233eead4e4191c66f9ed0b14eea091052/readme.md
* IDoFT `pr-data.csv` at the pinned commit: https://github.com/TestingResearchIllinois/idoft/blob/4903dc9233eead4e4191c66f9ed0b14eea091052/pr-data.csv
* GitHub API, repository and license endpoints: https://api.github.com/repos/TestingResearchIllinois/idoft and https://api.github.com/repos/TestingResearchIllinois/idoft/license
* GitHub Docs, Licensing a repository: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository
* GitHub REST API, pull requests (fields `merged`, `merged_at`, `merge_commit_sha`, `head.sha`): https://docs.github.com/en/rest/pulls/pulls
* Python `random` module, Notes on Reproducibility: https://docs.python.org/3/library/random.html
* BigQuery, loading JSON data ("JSON data must be newline-delimited"): https://docs.cloud.google.com/bigquery/docs/loading-data-cloud-storage-json
* IDoFT source note with the counts at this commit: [idoft.md](../source/idoft.md)
