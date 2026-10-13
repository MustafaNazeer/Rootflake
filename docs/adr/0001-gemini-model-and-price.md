# ADR 0001: Gemini model and pinned price

Status: Accepted (2026-10-12)
Date: 2026-10-12

## Context

Rootflake's runtime agents (Detector, Reproducer, Diagnoser, Fixer, Verifier) and the single agent baseline call one Gemini model on Google Cloud. Three things about that model feed directly into reported numbers, so they have to be pinned before any eval code exists:

1. **Price.** Cost per fixed test is a headline metric and is computed from a committed price table, so the price must come from Google's official pricing page and nowhere else.
2. **Training knowledge cutoff.** IDoFT fixes are public. Results are split into fixes merged before and after the model's cutoff, so the cutoff has to be documented by Google, not inferred.
3. **Lifecycle.** A model that retires in the middle of the project would invalidate the frozen test set run, since the 100 test split runs only once per tagged version.

The monthly budget for cloud and model spend is $25.

Two facts surfaced while checking sources:

* Google has renamed Vertex AI to **Gemini Enterprise Agent Platform**. The old pricing URL (`cloud.google.com/vertex-ai/generative-ai/pricing`) now redirects to `cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing`, titled "Agent Platform Pricing". This ADR still says Vertex AI where it means the product, and cites the current URLs.
* **Gemini 2.5 Flash, the model the original cost estimate was built on, retires on October 20, 2026**, eight days after this ADR was written. Its model page says: "This model is being retired on October 20th, 2026." It is not a viable choice.

## Options considered

All prices are USD per 1M tokens, Standard tier, **global endpoint**, from the official pricing page. Non global (regional) endpoints are listed on the same page at 10 percent more for every Gemini 3 model shown below. Output prices include reasoning ("thinking") tokens: the page labels the row "Text output (response and reasoning)".

| | gemini-3.5-flash-lite | gemini-3.5-flash | gemini-3.8-flash |
|---|---|---|---|
| Launch stage on Vertex AI | GA | GA | GA |
| Release date | July 21, 2026 | May 19, 2026 | September 2, 2026 |
| Listed retirement date | July 21, 2027 or later | May 19, 2027 or later | none listed |
| Input, up to 200K tokens | $0.30 | $1.50 | $0.75 through Dec 31, 2026; $1.50 from Jan 1, 2027 |
| Input, over 200K tokens | $0.30 | $1.50 | same as above |
| Cached input | $0.03 | $0.15 | $0.075 through Dec 31, 2026; $0.15 from Jan 1, 2027 |
| Output (response and reasoning) | $2.50 | $9.00 | $3.75 through Dec 31, 2026; $7.50 from Jan 1, 2027 |
| Flex or Batch input / output | $0.15 / $1.25 | $0.75 / $4.50 | $0.375 / $1.875 through Dec 31, 2026; $0.75 / $3.75 from Jan 1, 2027 |
| Context cache storage | $0.000001 per token per hour, as printed | same | same |
| Context window / max output | 1,048,576 / 65,536 | 1,048,576 / 65,536 | 1,048,576 / 65,536 |
| Documented knowledge cutoff | March 2026, with some domains limited to January 2025 | January 2025, by reference (see below) | March 2026, with some domains limited to January 2025 |

Notes on the table:

* **Long context tier.** For these three models the over 200K price equals the under 200K price, so the long context tier does not change cost. The page also states that if a query's input is longer than 200K tokens, all of that request's tokens are charged at long context rates, which matters only for models where the tiers differ (for example Gemini 3.1 Pro Preview, $2.00 against $4.00 input).
* **Gemini 3.8 Flash introductory price.** The page says the introductory price for Gemini 3.8, 3.7 and 3.6 Flash is "provided through 50% credits back on net spend", so an invoice may show the full price with a credit applied rather than the lower price directly. It ends on December 31, 2026, inside the project's expected timeline if the project slips.
* **Context cache storage.** The column header reads "Price Tok/hr", with $0.000001 for every Flash and Flash-Lite model listed. Read as per token per hour that is $1 per 1M token hours. The unit label is ambiguous, so this figure is **unverified** until it is confirmed against a billing SKU. Rootflake does not plan to use explicit caching, so this does not affect the decision.
* **Function declarations.** The page states: "Starting with Gemini 3.5, function declarations are included in the input token count." Every tool schema exposed through the three MCP servers is therefore billed on every model call, for all three candidates.
* **Knowledge cutoffs.** Google's model cards are the only official source found; the Vertex AI model pages and the Gemini API model pages do not list a cutoff.
  * The Gemini 3.5 Flash-Lite model card states directly that "The knowledge cutoff date for Gemini 3.5 Flash-Lite is March 2026", and adds that "users can expect updated information for some domains while in others they may experience the model's knowledge is limited to January 2025 (in line with the Gemini 3 Model Family)."
  * The Gemini 3.5 Flash model card states no cutoff. It says the model "is based on Gemini 3 Flash"; the Gemini 3 Flash card defers to the Gemini 3 Pro card, which says "The knowledge cutoff date for Gemini 3 Pro was January 2025." January 2025 for 3.5 Flash is therefore an inference through two "based on" links, not a direct statement.
  * The Gemini 3.8 Flash model card states directly that "The knowledge cutoff date for Gemini 3.8 Flash is March 2026", with the same "some domains January 2025" wording as 3.5 Flash-Lite. The Gemini 3.7 Flash card it is based on says the same for 3.7 Flash.
* **Gemini API routing.** On the separate Gemini Developer API (not Vertex AI), the 3.5 Flash model page lists "Deprecated: gemini-3.5-flash (automatically routed to gemini-3.6-flash)". The Vertex AI page for the same model still lists it as GA with a retirement date of May 19, 2027 or later. Rootflake calls Vertex AI only, so the Vertex AI page governs, but this is a sign that 3.5 Flash is being superseded.

Rejected without a column: **Gemini 2.5 Flash** (retires October 20, 2026), **Gemini 3.1 Pro Preview** and **Gemini 3 Flash Preview** (preview models carry no stability commitment, and the Standard tier row for 3 Flash Preview on the pricing page shows input prices but no output price).

### Per test cost estimate (unmeasured)

**This is an estimate from an assumption, not a measurement.** It assumes roughly 200,000 input tokens and 20,000 output tokens (reasoning included) per test per system, with no cache hits, on the global endpoint at Standard tier. Real token use will be measured on 5 dev tests before any larger run, and the run plan will be sized to the measured figure.

| Model | Per test per system | 50 dev tests, pipeline and baseline (100 runs) | 150 tests, pipeline and baseline (300 runs) |
|---|---|---|---|
| gemini-3.5-flash-lite | 0.2 x $0.30 + 0.02 x $2.50 = **$0.11** | $11 | $33 |
| gemini-3.5-flash-lite, Flex or Batch | 0.2 x $0.15 + 0.02 x $1.25 = $0.055 | $5.50 | $16.50 |
| gemini-3.8-flash, until Dec 31, 2026 | 0.2 x $0.75 + 0.02 x $3.75 = $0.225 | $22.50 | $67.50 |
| gemini-3.8-flash, from Jan 1, 2027 | 0.2 x $1.50 + 0.02 x $7.50 = $0.45 | $45 | $135 |
| gemini-3.5-flash | 0.2 x $1.50 + 0.02 x $9.00 = $0.48 | $48 | $144 |

Cache hits would lower these figures, since cached input is billed at one tenth of the input price for every candidate. Reasoning tokens could raise the output side well past 20,000 at higher thinking levels.

## Decision

Use **`gemini-3.5-flash-lite`** on Vertex AI, called on the **global** endpoint at the **Standard** tier, with these prices pinned in the committed price table:

* Input $0.30, cached input $0.03, output (response and reasoning) $2.50, per 1M tokens.

Reasons:

1. **It is the only compared candidate whose estimated cost fits the budget with room to spare.** At the assumed token use, a full dev run of both systems is about $11. The other two put a single dev run of both systems near or above the $25 monthly cap. (Gemini 3.1 Flash-Lite is cheaper still at $0.25 input and $1.50 output, but its launch stage, lifecycle and cutoff were not checked, so it is not a candidate here.)
2. **It has the longest documented support window** of the GA candidates: retirement July 21, 2027 or later, well past the project's timeline.
3. **Its knowledge cutoff is stated directly** in its own model card rather than inferred through a chain of references.

The model ID, the endpoint, the tier, the thinking level, and the price table version are recorded with every eval run, so a later model or price change produces a new, separately labelled result rather than silently changing old ones.

For the contamination split, the cutoff is taken as **March 2026**, the later of the two dates Google gives. A fix merged after March 31, 2026 counts as post cutoff. Using the later date is conservative: it can only move fixes into the "possibly seen" group, never out of it.

## Consequences

* The SPEC's cost estimate, which used third party prices for Gemini 2.5 Flash, is replaced by the figures above. The headline input and output prices happen to be identical ($0.30 and $2.50), so the earlier budget arithmetic still holds, but the source is now Google's own page.
* **The post cutoff group will be small.** With a March 2026 cutoff, only IDoFT fixes merged from April 2026 onward count as uncontaminated. That group may be too small to report a meaningful rate on its own; if so, it is reported with its count and interval and labelled as small, never dropped. A model with an earlier cutoff (Gemini 3.5 Flash, January 2025 by reference) would give a larger clean group at about four times the cost. That trade is open for review.
* The client must set the location to `global` explicitly. The LangChain Google integration defaults to `us-central1`, a non global endpoint that costs 10 percent more for Gemini 3 models.
* MCP tool schemas count as input on every call, so tool descriptions are kept short and only the tools a runtime agent needs are bound to it.
* The thinking level is pinned in configuration and recorded per run, because reasoning tokens are billed at the output rate.
* Flex or Batch pricing halves the cost and may suit offline eval runs that tolerate latency. That is a separate decision, since it changes run timing and possibly behaviour, and it is not adopted here.
* Google has already introduced 3.6, 3.7 and 3.8 Flash in the months after 3.5 Flash. A model newer than this choice will exist before the project ends. The pinned model does not change mid evaluation; a switch means a new ADR and a fresh, separately labelled run.

## Sources

All checked on 2026-10-12.

* Pricing (the only price source): https://cloud.google.com/vertex-ai/generative-ai/pricing, which redirects to https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing
* Gemini 3.5 Flash-Lite on Vertex AI (model ID, GA, release and retirement dates, token limits): https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash-lite (page last updated 2026-10-12)
* Gemini 3.5 Flash on Vertex AI: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash (page last updated 2026-10-12)
* Gemini 3.8 Flash on Vertex AI: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash (page last updated 2026-10-12)
* Gemini 2.5 Flash on Vertex AI (retirement October 20, 2026): https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/2-5-flash (page last updated 2026-10-12)
* Gemini 3.5 Flash-Lite model card (knowledge cutoff): https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/
* Gemini 3.5 Flash model card: https://deepmind.google/models/model-cards/gemini-3-5-flash/
* Gemini 3 Flash model card (published December 2025): https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-3-Flash-Model-Card.pdf
* Gemini 3 Pro model card (knowledge cutoff January 2025; last updated May 2026): https://storage.googleapis.com/deepmind-media/Model-Cards/Gemini-3-Pro-Model-Card.pdf
* Gemini 3.8 Flash model card (knowledge cutoff): https://deepmind.google/models/model-cards/gemini-3-8-flash/
* Gemini 3.7 Flash model card (knowledge cutoff): https://deepmind.google/models/model-cards/gemini-3-7-flash/
* Gemini API model page for 3.5 Flash (routing note): https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash
* LangChain Google integration, default location and backend selection: https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai
