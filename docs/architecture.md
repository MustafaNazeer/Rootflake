# Architecture

This document describes how Rootflake is designed to work. None of it is built yet: the repository currently holds this design, the security documents, the source notes, and the first architecture decision records. Where a detail is still open, it says so.

## Goal

Rootflake takes a flaky test in an open source Java project, confirms that it is flaky, works out why, writes a fix, and accepts the fix only if a deterministic gate passes it after hundreds of sandboxed reruns. A pipeline of LLM agents does the diagnosis and repair. The gate is ordinary code; no model can change its verdict.

The project is meant to be judged on two kinds of evidence:

1. **Offline.** A verified fix rate on a frozen, held out set of real flaky tests from IDoFT that humans already fixed, compared against a single agent baseline on the same tests.
2. **Upstream.** Fixes for flaky tests that nobody has fixed yet, reviewed and submitted by me by hand, with every outcome (merged, rejected, pending) recorded.

### Scope

* Java projects built with Maven. Gradle and Python projects are out of scope for now.
* The Java range is whatever the sandbox can actually build and test. NonDex documents Java 8 to 21 ([nondex.md](source/nondex.md)); iDFlakies documents no range and its CI tests only JDK 8 ([idflakies.md](source/idflakies.md)). The supported range is therefore settled by testing in the sandbox, not taken from either README.
* Rootflake never opens a pull request on its own, and never claims a test is "no longer flaky". The gate gives a measured bound, described below.

## Overview

```
corpus builder --> sandbox (Docker: JDKs, Maven, iDFlakies, NonDex)
                         ^
                         |  MCP servers: repo, runner, history
                         |
LangGraph pipeline: Detector -> Reproducer -> Diagnoser -> Fixer <-> Verifier
single agent baseline (same tools, same model, same budget)
                         |
                         v
deterministic gate --> JSON Lines --> BigQuery --> metrics JSON --> dashboard
                                      pgvector (past fixes)
upstream track --> PR package --> human review queue
```

## Stack

| Area | Choice | Decision record |
|---|---|---|
| Language | Python 3.14, packaged with uv | [ADR 0003](adr/0003-python-and-library-versions.md) |
| Agent framework | LangGraph | [ADR 0003](adr/0003-python-and-library-versions.md) |
| Tool protocol | MCP, official Python SDK (v2 API) | [ADR 0003](adr/0003-python-and-library-versions.md) |
| Model | `gemini-3.5-flash-lite` on Vertex AI, which Google now brands Gemini Enterprise Agent Platform | [ADR 0001](adr/0001-gemini-model-and-price.md) |
| Embeddings | `gemini-embedding-001` at 768 dimensions | [ADR 0002](adr/0002-embedding-model.md) |
| Vector index | pgvector with an HNSW index, on Supabase's free tier | [ADR 0002](adr/0002-embedding-model.md) |
| Results store | BigQuery, loaded from JSON Lines | not yet written |
| Sandbox | One Docker image with JDKs, Maven, iDFlakies, and NonDex, used locally and on Cloud Run Jobs | not yet written |
| Cloud | GCP: Artifact Registry, Cloud Run Jobs, BigQuery, Vertex AI | not yet written |
| Infrastructure as code | Terraform | not yet written |
| CI | GitHub Actions | not yet written |
| Dashboard | React, Vite, TypeScript, Vitest; static site | not yet written |

Exact library versions and model prices live in the ADRs, not here, so there is one place to update them.

## Components

### Corpus builder

The corpus comes from IDoFT's `pr-data.csv` ([idoft.md](source/idoft.md)), which lists flaky tests in Java Maven projects with the commit where each was detected, its module, its fully qualified name, a root cause category, a status, and a link to the fix PR.

* Only rows with Status `Accepted` are used, because those have a human fix that the project merged.
* A row is kept only if the project builds in the sandbox at the detected commit. `pr-data.csv` has no Java version column, so this is the only reliable filter.
* For each row the builder records the project URL, the detected commit, the module path, the test name, the IDoFT category, and the merged fix diff fetched from the PR link.
* A stratified sample across categories is drawn with a fixed seed and written to a versioned corpus file that is committed. Every reported number traces back to that script and that file.
* The plan is 150 tests: a **dev set of 50** used freely for tuning prompts and the pipeline, and a **test set of 100** that is frozen, never used for tuning, and run only on a tagged version. Only test set numbers are reported as results.

Two things are still open. First, which categories to include: at the IDoFT commit I checked, no Accepted row carries TD, and OD-Brit and TZD have only 12 and 4 Accepted rows ([idoft.md](source/idoft.md)), so the inclusion list and the stratification need another look before the corpus is drawn. Second, IDoFT declares no license, so how much of each row the committed corpus file carries (for example, only row keys plus the upstream commit) still has to be decided in an ADR.

### Sandbox and runner

The sandbox image holds the JDKs, Maven, iDFlakies, and NonDex. For each target it:

1. checks out the project at the pinned commit;
2. downloads dependencies in a separate step that has network access but no credentials;
3. runs everything after that with no network.

The runner exposes four commands: run a test once, run a test N times, run iDFlakies, and run NonDex. Every command has a wall time limit, and every container has memory, CPU, and process count limits. Image layers and Maven caches are keyed per project and commit, so repeated runs do not rebuild and one project cannot contaminate another.

A run counts as passed only when the process exit status and the parsed Surefire report agree; any disagreement is recorded as an infrastructure outcome. A sandbox timeout or crashed container is retried once and then recorded as infrastructure, never as a failed fix.

The full set of controls, and how each one is enforced in local Docker versus Cloud Run Jobs, is in the [sandbox hardening checklist](security/sandbox-hardening-checklist.md). The threats they answer are in the [threat model](security/threat-model.md).

### MCP servers

The runtime agents reach the sandbox and the fix history only through three MCP servers. Every tool has a JSON schema and a contract test on its input and output.

* **repo:** read a file, search code, view a diff, apply a patch to the sandbox copy. Every path must be relative, must resolve inside the workspace after following symlinks, and is refused otherwise (control SH-14).
* **runner:** the four sandbox commands, returning structured results: pass and fail counts, failure logs, and timing.
* **history:** past rerun results for a test, and a similarity search over previously merged fixes in pgvector.

### Agent pipeline

The pipeline is a LangGraph state graph with five runtime agents:

* **Detector** confirms flakiness with the method that fits the category: iDFlakies for order dependence, NonDex for implementation dependence, plain reruns otherwise. It records the failure rate before any fix.
* **Reproducer** finds a reliable trigger and saves it as a repro recipe: a failing test order, a NonDex seed, or a stress loop. Order matters, because an order dependent test can pass every time when run alone.
* **Diagnoser** classifies the root cause using IDoFT's category vocabulary, retrieves the most similar past fixes from pgvector, and writes a diagnosis that cites files and lines as evidence. For order dependent tests, iFixFlakies' victim, polluter, cleaner, brittle, and state setter terms give it precise vocabulary ([ifixflakies.md](source/ifixflakies.md)).
* **Fixer** writes a patch.
* **Verifier** runs the gate and sends failures back to the Fixer with the evidence.

Each test gets at most 3 fix attempts and a fixed token and cost budget. Prompts are versioned files in the repository, and every eval run records which prompt version it used, along with the model ID, endpoint, thinking level, price table version, and lockfile hash.

### Single agent baseline

One agent with the same three MCP servers, the same model, and the same per test budget. It exists so that the claim "the multi agent pipeline helps" is tested on the same tests under the same constraints, rather than assumed.

## The gate

The gate is deterministic code. It runs in this order:

1. **Patch validation.** Before a patch touches the workspace, it is parsed and rejected if any path fails the repo server's confinement rules, if it creates symlinks, binaries, or executable files, or if it is too large. Which files a patch may touch is an allowlist: Java sources under `src/main/java` and `src/test/java` of a Maven module, and test resources that are not test engine configuration. Build files, CI files, deletions and renames are rejected (control SH-15, [ADR 0007](adr/0007-shortcut-checker-and-patch-validation.md)).
2. **Shortcut checker.** Rejects fixes that make the symptom go away without fixing the cause: deleting or disabling the test (`@Ignore`, `@Disabled`, and equivalents), adding retries (retry rules, rerun annotations, Surefire rerun settings), removing or weakening assertions, and catching and swallowing the failure. It is built test first against a large fixture set of shortcut diffs and legitimate fixes.
3. **Build.** The project still builds.
4. **Repro recipe.** The saved trigger now passes.
5. **50 quick reruns.** All must pass. These exist only to give fast feedback inside the fix loop.
6. **Retry loop.** On any failure, the evidence goes back to the Fixer, up to 3 attempts in total.
7. **500 final reruns.** On success, the test is rerun 500 times, plus extra NonDex seeds or test orders where the category calls for them.

### Why 500 final reruns

A fix that still fails 1 percent of the time passes 500 straight runs with probability 0.99^500, about 0.66 percent. Put the other way: if a fixed test passes all 500 reruns, the 95 percent upper bound on its remaining failure rate is about 0.6 percent. That is a measured bound, not a guarantee, and it is reported as such. Fifty reruns alone would only bound the failure rate at about 5.8 percent, which is why they are used for fast feedback and not for acceptance.

### Outcomes

Every test ends in exactly one outcome: **fixed**, **fix failed**, **not reproduced**, **build failed**, **budget exhausted**, or **infrastructure**. Every step records tokens, cost, time, and result.

## Evaluation

### Metrics

* **Verified fix rate**, over all entries and over reproducible entries, each with a 95 percent Wilson score interval. At n = 100 the interval's half width is up to about 10 percentage points (near a rate of 50 percent), so small differences between configurations will not be distinguishable.
* **Outcome counts** for every outcome, always shown next to the fix rate.
* **Diagnosis accuracy** against the IDoFT category labels, with a confusion matrix.
* **Cost per fixed test, time per test, and attempts used.** Cost is computed from a committed price table taken from Google's official pricing page ([ADR 0001](adr/0001-gemini-model-and-price.md)), so results are reproducible even if prices change.
* **Pipeline against baseline**, paired on the same tests, with McNemar's test. A difference that is not significant is reported as not significant.
* **Upstream record:** submitted, merged, rejected, pending, with every rejection counted.

The human fix is stored and shown side by side with Rootflake's diff, but it is not scored automatically, because two different fixes can both be correct.

A committed metrics JSON, written by the eval harness, is the only source for any published number.

### Contamination

IDoFT fixes are public, so the model may have seen some of them in training. Results are reported separately for fixes merged after the model's documented training cutoff. The cutoff used, and why, is recorded in [ADR 0001](adr/0001-gemini-model-and-price.md). The post cutoff group may be small; if so it is reported with its count and interval and labelled as small, never dropped.

The upstream track has no contamination by construction, since those tests have no published fix. That makes merged upstream fixes the strongest evidence the project can produce.

### Related work

* **iFixFlakies** repairs order dependent tests without a learned model, by searching the suite for helper tests and patching from them ([ifixflakies.md](source/ifixflakies.md)). It is the natural non LLM point of comparison for order dependent categories.
* **FlakyFix** predicts a fix category with fine tuned CodeBERT and UniXcoder models, then prompts GPT-3.5 Turbo with that category to repair the test. Its headline repair metric is CodeBLEU similarity to the developer's fix; only 35 repairs were executed, 10 runs each, and the rest were projected ([flakyfix.md](source/flakyfix.md)). Rootflake measures something different (a gate pass after hundreds of reruns), so the two numbers should not be compared as if they were the same quantity.

## Results storage

* Local and cloud runs both write JSON Lines run logs.
* A loader pushes them into BigQuery tables for runs, attempts, and eval runs, so free local runs and cloud runs land in the same place. Records pass a secret pattern scan before loading.
* The eval harness summarizes the tables into the committed metrics JSON, which contains aggregates only.
* The fix history used by the Diagnoser lives in Postgres with pgvector, `vector(768)` columns, and an HNSW index using cosine distance. The model ID, dimension, and task type are stored with every vector, so a model change means embedding everything again into a new column rather than mixing vectors from two models ([ADR 0002](adr/0002-embedding-model.md)).
* The dashboard is a static, read only React site with an overview (fix rate with intervals, pipeline against baseline, cost and time), a per category breakdown with the diagnosis confusion matrix, and a page per test showing the diagnosis, Rootflake's diff, the human fix, and the rerun evidence. Logs are rendered as escaped text. Hosting is not decided yet.

## Upstream track

For flaky tests nobody has fixed, the same pipeline runs, and a gate pass produces a **PR package**: the diff, the diagnosis, the rerun evidence, and a draft PR description that says the fix was produced by Rootflake and reviewed by me.

* Rootflake never opens a pull request. I read every package and submit any PR myself.
* Before submitting to a project, I read and follow its contribution guidelines and its policy on AI generated contributions.
* Every outcome is recorded honestly, including rejections.
* If a run surfaces what looks like a real security vulnerability in a target project, it is removed from the package, kept out of public logs and the dashboard, and reported privately to the maintainers (control SH-24).

## Security

Rootflake runs test code from repositories nobody on this project wrote, and applies patches written by an LLM that has read that code. The design treats both as untrusted:

* No network after the dependency download step.
* A non root user, all Linux capabilities dropped, no privilege escalation, and a read only root filesystem except the workspace, locally.
* Memory, CPU, process count, and wall time limits on every run.
* Nothing sensitive is mounted into the sandbox, and credentials for Vertex AI, BigQuery, and Supabase exist only in the pipeline process.
* Patches apply only to a fresh sandbox copy of the target repository, and the repo server refuses any path outside the workspace.
* Repository content and sandbox output reach the runtime agents only as delimited data, and no agent has a tool that reaches the network, a credential, or a path outside the workspace.

Cloud Run Jobs cannot enforce every one of these the same way Docker can; for example, there is no read only root filesystem setting and no direct equivalent of `--network none`. The checklist records what differs for each control. Until the way the sandbox gets no network on Cloud Run is chosen and tested, no target project's tests run there. Forged test results from code inside the sandbox are a residual risk that the threat model accepts and states openly (TM-11).

See the [threat model](security/threat-model.md) and the [sandbox hardening checklist](security/sandbox-hardening-checklist.md).

## Cost controls

The monthly budget for cloud and model spend is 25 USD.

* A GCP billing budget alert at 25 USD. It is a notification, not a hard stop, so it is not relied on for enforcement.
* Hard caps on tokens and cost per test and per eval run, which stop the run and record the test as budget exhausted.
* At most 3 fix attempts per test, and capped tool calls per agent turn.
* Real token use is measured on 5 dev tests before any larger run, and run sizes are planned from the measured figure. The per test estimate in [ADR 0001](adr/0001-gemini-model-and-price.md) is an unmeasured assumption until then.
* Bulk sandbox reruns run locally on Docker, so they add no cloud cost.
* Terraform manages the cloud resources. `terraform plan` runs in CI on pull requests; `terraform apply` is run by hand.

## Testing Rootflake itself

* **Shortcut checker:** test first, against a large fixture set of shortcut diffs that must be rejected and legitimate fixes that must pass.
* **Gate and statistics:** test first; Wilson intervals and McNemar's test are checked against independently computed reference values.
* **MCP servers:** a contract test on every tool's input and output schema, including path traversal and symlink fixtures for the repo server.
* **End to end:** a small fixture Maven project with one planted flaky test per included category runs through the whole pipeline on recorded model responses, so it is deterministic and free.
* **Dashboard:** Vitest component tests.
* **CI:** lint, pytest, the end to end test, and Vitest on every push. A real model eval on 5 dev tests runs only on pull requests to main, under a spending cap.
