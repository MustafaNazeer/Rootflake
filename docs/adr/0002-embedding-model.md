# ADR 0002: Embedding model for the fix history index

Status: Accepted (2026-10-12)
Date: 2026-10-12

## Context

The Diagnoser retrieves the most similar previously merged fixes before it writes a diagnosis. The history lives in Postgres with pgvector and an HNSW index on Supabase's free tier. Each stored document is a merged fix: a code diff plus short text (the IDoFT category, the test name, and a one paragraph description of the cause). The query side is a diagnosis in progress: a failing test, its category guess, and code excerpts.

Requirements:

* Handles Java code and English prose together.
* Produces vectors that pgvector can index with HNSW without workarounds.
* Costs a negligible amount at the expected scale (hundreds to low thousands of documents).
* Runs on Vertex AI, so it shares the project, service account and billing alert with the chat model (ADR 0001).

**pgvector's HNSW limit.** pgvector's README lists the maximum dimensions an HNSW index supports per type: `vector` up to 2,000, `halfvec` up to 4,000, `bit` up to 64,000. A plain `vector` column can store up to 16,000 dimensions, but only 2,000 can be indexed. The current pgvector release tag is v0.8.7.

## Options considered

From the Vertex AI (now Gemini Enterprise Agent Platform) text embeddings documentation:

| Model | Output dimensions | Max input per text | Notes from the docs |
|---|---|---|---|
| `gemini-embedding-001` | up to 3072 (default 3072), adjustable with `output_dimensionality` | 2048 tokens | "State-of-the-art performance across English, multilingual and code tasks." Unifies text-embedding-005 and text-multilingual-embedding-002. One input text per request. |
| `text-embedding-005` | up to 768 | 2048 tokens | "Specialized in English and code tasks." |
| `gemini-embedding-2` | up to 3072 | 8192 tokens | Multimodal. Priced on the page as "Gemini Embedding 2 (Unified Multimodal, Preview)". |
| `text-multilingual-embedding-002` | up to 768 | 2048 tokens | Multilingual focus; not relevant here. |

API limits: the guide says at most 250 input texts and 20,000 input tokens per request, while the request example on the same page says five texts per request (one for `gemini-embedding-001`); the batch size is **unverified** and will be confirmed against the API. Each text over its limit is **silently truncated** unless `autoTruncate` is set to false, in which case the request fails instead. Task types include `RETRIEVAL_DOCUMENT`, `RETRIEVAL_QUERY`, and `CODE_RETRIEVAL_QUERY` ("used for code retrieval for Java and Python. Use RETRIEVAL_DOCUMENT for the document side"). The docs state that the vectors are normalized, so cosine, dot product and Euclidean distance give the same ranking.

**Prices**, from the official pricing page, section "Embedding costs":

| Model | Online input | Batch input | Output |
|---|---|---|---|
| Gemini Embedding | $0.00015 | $0.00012 | no charge |
| Embeddings for Text (excluding Gemini Embedding) | $0.000025 | $0.00002 | no charge |
| Gemini Embedding 2 (Preview), text input | $0.20 per 1M tokens | $0.10 per 1M tokens | no charge |

The first two rows sit under a column headed "Price / 1,000 count (USD)". The page does not say what is being counted (tokens or characters). **The unit is unverified.** It does not change the decision, because even the most expensive reading is negligible: 2,000 documents of 8,000 characters each is 16M characters, which at $0.00015 per 1,000 characters is $2.40 for the whole index, and far less if the unit is tokens.

**Fitting pgvector.** `gemini-embedding-001` at its default 3072 dimensions exceeds the 2,000 dimension HNSW limit for `vector`. It would need `halfvec` (limit 4,000) or a smaller `output_dimensionality`. `text-embedding-005` at 768 fits as is.

**Lifecycle.** Retirement dates for the embedding models were not found on the pages checked and are **unverified**.

## Decision

Use **`gemini-embedding-001`** with **`output_dimensionality` set to 768**, stored in a `vector(768)` column with an HNSW index using cosine distance (`vector_cosine_ops`).

* Documents are embedded with task type `RETRIEVAL_DOCUMENT`; queries with `CODE_RETRIEVAL_QUERY` when the query is mostly code and `RETRIEVAL_QUERY` otherwise. The task type used is recorded with each stored vector.
* `autoTruncate` is set to **false**. A document over 2048 tokens fails loudly and is shortened deliberately by Rootflake's own code (the description and the changed hunks first, then context lines), rather than being cut at an arbitrary point by the API.
* The model ID, dimension, and task type are stored alongside every vector, so a later model change means embedding everything again into a new column, never a mix of vectors from two models in one index.

Reasons:

1. The docs describe it as covering both code and English and as superseding `text-embedding-005`.
2. 768 dimensions fits the plain `vector` HNSW limit with a wide margin and keeps index memory small on a free tier database.
3. Cosine distance is safe whether or not reduced dimension outputs are exactly unit length. The docs state normalization for the default output; this was not confirmed for reduced dimensions.
4. Cost is negligible under any reading of the price unit.

`text-embedding-005` is the fallback if `gemini-embedding-001` turns out to be unsuitable in practice, for example because of its one text per request limit. `gemini-embedding-2` is rejected for now because it is listed as Preview.

## Consequences

* Retrieval quality at 768 dimensions is assumed rather than measured. Retrieval is evaluated on the dev set (does the right category's past fix appear in the top results), and the dimension is revisited only through a new ADR.
* One text per request means indexing is a loop of single calls; at hundreds to thousands of documents this is acceptable.
* Raising the dimension above 2,000 later would require switching the column to `halfvec` or using half precision indexing.
* Truncation handling is Rootflake's responsibility and needs a test that an oversized diff is shortened, not silently cut.

## Sources

All checked on 2026-10-12.

* Text embeddings guide (models, dimensions, limits, truncation, normalization): https://cloud.google.com/vertex-ai/generative-ai/docs/embeddings/get-text-embeddings, which redirects to https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/embeddings/get-text-embeddings (page last updated 2026-10-09)
* Text embeddings API reference (task types, `autoTruncate`, `outputDimensionality`): https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/text-embeddings-api, which redirects to https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/text-embeddings-api
* Pricing, "Embedding costs" and "MultiModal Embeddings": https://cloud.google.com/vertex-ai/generative-ai/pricing, which redirects to https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
* pgvector README (HNSW dimension limits, storage limit, distance operators): https://github.com/pgvector/pgvector/blob/master/README.md
* pgvector release tags (v0.8.7 latest): https://github.com/pgvector/pgvector/tags
