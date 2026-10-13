# ADR 0003: Python and core library versions

Status: Accepted (2026-10-12)
Date: 2026-10-12

## Context

Rootflake's Python system (sandbox runner, gate, MCP servers, LangGraph pipeline, eval harness) needs one interpreter version and a pinned set of core libraries before any code lands. Every eval run records the exact versions it used, so pins have to be deliberate.

Constraints:

* The interpreter must be supported upstream for the life of the project and receive bug fixes, not only security fixes, if possible.
* Every core library must support it, with prebuilt wheels for its compiled dependencies.
* Packaging uses uv, with a committed `uv.lock`.

## Options considered

### Python

From the Python developer guide's release status table:

| Branch | Status | First release | End of life |
|---|---|---|---|
| 3.15 | bugfix | 2026-10-09 | 2031-10 (scheduled) |
| 3.14 | bugfix | 2025-10-07 | 2030-10 (scheduled) |
| 3.13 | security | 2024-10-07 | 2029-10 (scheduled) |
| 3.12 | security | 2023-10-02 | 2028-10 (scheduled) |
| 3.10 | end of life | 2021-10-04 | 2026-10-01 |

* **3.15** was released three days before this ADR. Several libraries below do not declare it yet, and compiled dependencies may lack wheels.
* **3.13** is supported by every library below, but it has moved to security fixes only.
* **3.14** receives bug fixes for about 24 months after its first release (its release schedule lists the final regular bugfix release, 3.14.14, for 2027-10-05, around the release of 3.16.0) and security fixes until October 2030.

### Libraries

Latest stable release on PyPI on 2026-10-12, with the Python range each declares:

| Package | Latest stable | Released | `Requires-Python` | Python classifiers |
|---|---|---|---|---|
| `uv` | 0.13.0 | 2026-10-09 | >=3.8 | 3.8 to 3.15 |
| `langgraph` | 1.2.15 | 2026-10-12 | >=3.10 | 3.10 to 3.13 |
| `langchain-google-genai` | 4.4.1 | 2026-10-10 | >=3.10.0, <4.0.0 | none declared |
| `google-genai` (pulled in by the above) | 2.30.0 | 2026-10-13 (UTC) | >=3.10 | 3.10 to 3.14 |
| `mcp` (official MCP Python SDK) | 2.3.0 | 2026-10-02 | >=3.10 | 3.10 to 3.14 |
| `pytest` | 9.1.1 | 2026-06-19 | >=3.10 | 3.10 to 3.15 |
| `pgvector` (Python client) | 0.5.1 | 2026-10-09 | >=3.10 | none declared |

Notes:

* **LangGraph and Python 3.14.** LangGraph's PyPI classifiers stop at 3.13, but its CI test matrices on the main branch (`_test.yml` and `_test_langgraph.yml`) run 3.10 through 3.14. The missing classifier is metadata lag, not a lack of support.
* **Which LangChain Google package.** LangChain's Google provider page says: "For Gemini models, use `ChatGoogleGenerativeAI` from `langchain-google-genai`", and marks `ChatVertexAI` as deprecated for Gemini models. `langchain-google-vertexai` (latest 3.2.4, released 2026-06-09) remains supported for platform features beyond Gemini chat models. `ChatGoogleGenerativeAI` reaches Vertex AI with `vertexai=True` plus a `project`, or through the `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` environment variables. Its location defaults to `us-central1`, which ADR 0001 overrides with `global`.
* **MCP SDK v2 is a breaking release.** `mcp` 2.0.0 shipped on 2026-07-28. According to the SDK's "What's new in v2" page: `FastMCP` is renamed `MCPServer` (`from mcp.server import MCPServer`) and the old import path is removed; a single `Client` object replaces the nested v1 client setup; wire types moved to the separate `mcp-types` package; the WebSocket transport is removed; stdio and Streamable HTTP remain, and the client still accepts other transport context managers such as `sse_client`; and the SDK targets the 2026-07-28 MCP revision while still serving 2025 era clients. Most tutorials and examples online are written against v1 and will not run unchanged.
* **Wheels.** The compiled dependencies in this set (`pydantic-core`, `ormsgpack`, `xxhash`, `orjson`, `jiter`, `cryptography`) all publish CPython 3.14 wheels.
* **Resolution check.** A dry resolution with `uv pip compile --only-binary :all:` of `langgraph==1.2.15`, `langchain-google-genai==4.4.1`, `mcp==2.3.0`, `pytest==9.1.1` and `pgvector==0.5.1` succeeded for both `--python-version 3.13` and `--python-version 3.14` on Linux x86_64, resolving `langchain-core` 1.6.10, `google-genai` 2.30.0 and `pydantic` 2.14.1. Nothing was installed.

## Decision

* **Python 3.14**, declared as `requires-python = ">=3.14,<3.15"` and pinned in `.python-version`, so the same interpreter runs locally, in CI, and in the pipeline container.
* **uv 0.13.0** as the minimum uv version, set through `required-version` in the uv configuration, with `uv.lock` committed.
* Core libraries, pinned exactly in the lockfile and with compatible lower bounds in `pyproject.toml`:
  * `langgraph` 1.2.15
  * `langchain-google-genai` 4.4.1 (Gemini chat models on Vertex AI through `ChatGoogleGenerativeAI`)
  * `mcp` 2.3.0 (servers built on `MCPServer`, v2 API only)
  * `pytest` 9.1.1
  * `pgvector` 0.5.1
* Embeddings are called through `google-genai` directly unless the LangChain integration covers `output_dimensionality`, task type and `autoTruncate` (ADR 0002); that is checked when the history server is built.
* `langchain-google-vertexai` is not added.

Reasons: 3.14 is the newest branch still receiving bug fixes with an ecosystem that already supports it, its security support runs to 2030, and every library above either declares it or tests it in CI.

## Consequences

* The fallback is 3.13 if any later dependency lacks 3.14 support; the resolution check above shows the current set works on both.
* `langgraph` 1.2.15 was released on the day this ADR was written. If it shows a regression, pinning to the previous 1.2.x patch is acceptable without a new ADR; a minor or major version change needs one.
* MCP code is written against the v2 API. Contract tests on every tool's input and output schema matter more than usual: per the v2 notes, the low level `Server` no longer validates tool arguments against the schema before the handler runs.
* Version bumps happen through deliberate lockfile updates, never floating ranges in CI, and every eval run records the lockfile hash.
* Not covered here: a Postgres driver, a LangGraph checkpointer, and lint or type check tooling. Those are pinned when the code that needs them lands.

## Sources

All checked on 2026-10-12.

* Python release status: https://devguide.python.org/versions/
* Python 3.14 release schedule and lifespan (PEP 745): https://peps.python.org/pep-0745/
* uv on PyPI: https://pypi.org/project/uv/
* LangGraph on PyPI: https://pypi.org/project/langgraph/
* LangGraph CI test matrices: https://github.com/langchain-ai/langgraph/blob/main/.github/workflows/_test.yml and https://github.com/langchain-ai/langgraph/blob/main/.github/workflows/_test_langgraph.yml
* langchain-google-genai on PyPI: https://pypi.org/project/langchain-google-genai/
* langchain-google-vertexai on PyPI: https://pypi.org/project/langchain-google-vertexai/
* google-genai on PyPI: https://pypi.org/project/google-genai/
* LangChain Google provider page: https://docs.langchain.com/oss/python/integrations/providers/google
* ChatGoogleGenerativeAI integration (Vertex AI backend selection): https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai
* MCP Python SDK on PyPI: https://pypi.org/project/mcp/
* MCP Python SDK, What's new in v2: https://py.sdk.modelcontextprotocol.io/whats-new/
* MCP Python SDK repository: https://github.com/modelcontextprotocol/python-sdk
* pytest on PyPI: https://pypi.org/project/pytest/
* pgvector Python client on PyPI: https://pypi.org/project/pgvector/
* Wheel availability: the PyPI JSON API for pydantic-core, ormsgpack, xxhash, orjson, jiter and cryptography (https://pypi.org/pypi/PACKAGE/json)
