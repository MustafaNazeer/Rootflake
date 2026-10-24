# ADR 0004: Repository layout

Status: Accepted (2026-10-12)
Date: 2026-10-12

## Context

Rootflake has several parts with different toolchains: a Python package (runner, gate, MCP servers, pipeline, evals), a Docker image for the sandbox, committed corpus files, a React dashboard, and Terraform. Tests need fixture Maven projects, shortcut and legitimate patch fixtures, and recorded model responses so the end to end test is deterministic and free. Prompts are versioned and every eval run records which version it used. These locations need to be fixed before code lands so paths in tests, CI, and the eval records stay stable.

## Options considered

* **Fixtures:** under `tests/fixtures/`, or in a top level `fixtures/` folder shared by tests and demo runs. Keeping them inside `tests/` makes it obvious that nothing there is product data.
* **Prompts:** as package data in `src/rootflake/prompts/`, or in a top level `prompts/` folder. Package data ships with the code and is loaded the same way in every environment, including the container that runs the pipeline.
* **Lint:** ruff for lint and format, optionally with a type checker in CI from the start.

## Decision

```
.
├── pyproject.toml           project metadata, dependencies, ruff and pytest settings
├── uv.lock                  committed lock file
├── src/rootflake/
│   ├── corpus/              corpus builder
│   ├── sandbox/             runner: build, run once, run N times, iDFlakies, NonDex
│   ├── gate/                patch validation, shortcut checker, rerun gate, outcomes
│   ├── mcp/                 repo, runner, and history MCP servers
│   ├── pipeline/            LangGraph graph and the runtime agents
│   ├── baseline/            single agent baseline
│   ├── evals/               eval harness and statistics
│   ├── storage/             JSON Lines writers, BigQuery loader, pgvector access
│   ├── upstream/            PR package builder
│   └── prompts/             versioned prompt files, one per runtime agent
├── tests/
│   ├── unit/, integration/, e2e/
│   └── fixtures/
│       ├── maven/           fixture Maven projects with planted flaky tests
│       ├── patches/         shortcut and legitimate fix diffs, with provenance
│       └── model-responses/ recorded responses for the end to end test
├── sandbox/                 Dockerfile and entrypoint for the sandbox image
├── corpus/                  versioned corpus files (format in ADR 0006)
├── dashboard/               React, Vite, TypeScript, Vitest
├── infra/                   Terraform
├── tools/                   maintenance scripts run by hand, such as regenerating checker data
├── .github/workflows/       CI
└── docs/
```

* Package names are added as each part is built; an empty subpackage is not created ahead of its code.
* Fixtures live under `tests/fixtures/`. Every fixture taken from a real project records its source (repository, commit, file, license) next to it.
* Prompts live in `src/rootflake/prompts/`. Each prompt file carries a version field, and every eval record stores the version of each prompt it used.
* Lint and format use **ruff 0.17.0** (released 2026-10-09 on PyPI), configured in `pyproject.toml` and run in CI on every push. No type checker in CI for now.

## Consequences

* Paths in eval records and test fixtures are stable from the first commit of code.
* A fixture Maven project inside `tests/` is never mistaken for part of the corpus.
* Adding a type checker later is a separate decision; nothing here prevents it.

## Sources

* ruff on PyPI, checked 2026-10-12: https://pypi.org/project/ruff/
