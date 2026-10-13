# Rootflake

Rootflake finds flaky tests in open source Java projects, diagnoses why they flake, writes fixes, and accepts a fix only after hundreds of sandboxed reruns pass.

**Status: early development. Nothing here is usable yet.** The repository currently holds the design, the security documents, notes on prior work, and the first architecture decision records. There is no working code and there are no results. When results exist, they will be published from a committed metrics file, with intervals and every outcome counted.

## How it is meant to work

A pipeline of LLM agents, built with LangGraph, handles each flaky test in five steps:

1. **Detector** confirms the test is flaky and records how often it fails, using iDFlakies for order dependent tests, NonDex for tests that rely on unspecified Java API behavior, and plain reruns otherwise.
2. **Reproducer** finds a reliable trigger: a failing test order, a NonDex seed, or a stress loop.
3. **Diagnoser** names the root cause in IDoFT's category vocabulary, citing files and lines, with the most similar past fixes retrieved from a pgvector index.
4. **Fixer** writes a patch.
5. **Verifier** runs the gate and sends failures back to the Fixer, up to 3 attempts per test.

The agents reach the code only through three MCP servers (repo, runner, history), and the test code itself runs only inside a locked down Docker sandbox with no network and no credentials.

**A deterministic gate, never a model, decides whether a fix counts.** It rejects shortcuts (disabling or deleting the test, adding retries, weakening assertions, swallowing the failure), then requires the project to build, the reproduction trigger to pass, 50 quick reruns to pass, and finally 500 reruns to pass. A fix that still failed 1 percent of the time would survive 500 straight reruns only about 0.66 percent of the time. That is a measured bound, not a guarantee that a test is no longer flaky.

## How it will be judged

* **Offline:** a verified fix rate on a frozen, held out set of 100 real flaky tests from [IDoFT](https://github.com/TestingResearchIllinois/idoft) that humans already fixed, with a 95 percent Wilson interval, every outcome counted, and a paired comparison against a single agent baseline using McNemar's test. Results for fixes merged after the model's training cutoff are reported separately.
* **Upstream:** fixes for flaky tests nobody has fixed yet. Rootflake never opens a pull request; I review every fix and submit it by hand, following each project's contribution and AI policies, and record every outcome, including rejections.

## Stack

Python, LangGraph, the MCP Python SDK, Gemini on Vertex AI, pgvector, BigQuery, Docker, Cloud Run Jobs, Terraform, GitHub Actions, and a static React dashboard. Exact versions, the model choice, and its pinned price are in the [architecture decision records](docs/adr/). The monthly budget for cloud and model spend is 25 USD. Hard per test and per run caps stop a run before it overspends; the GCP budget alert alone only notifies.

## Documentation

* [Architecture](docs/architecture.md): the pipeline, the gate, the sandbox, the evaluation method, and cost controls.
* [Architecture decision records](docs/adr/)
* [Threat model](docs/security/threat-model.md) and [sandbox hardening checklist](docs/security/sandbox-hardening-checklist.md)
* [Source notes](docs/source/README.md) on IDoFT, iDFlakies, NonDex, iFixFlakies, and FlakyFix

## License

[MIT](LICENSE)
