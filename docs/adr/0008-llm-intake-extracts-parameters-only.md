# ADR 0008: The model extracts parameters; it never executes

## Context

Every write in StockPilot is deliberately deterministic: inventory is checked,
mapping ambiguities suspend the task, prices are staged, and a human approves
before `publish_listing` commits. Adding a natural-language front door raises
an obvious question: should the model call the tools?

If it did, prompt injection, hallucinated SKUs, or a model that "helpfully"
publishes would bypass the properties the rest of the system is built to
guarantee. The safety story would depend on prompt quality rather than on code.

## Decision

Add an optional intake step that extracts **parameters only**:

```
POST /tasks/intake  {text}  ->  TaskDraft {sku_id, spec, target_price, reason, warnings}
                                 (not persisted, no tool calls, no authority)
POST /tasks          {draft} ->  the existing graph and all of its guards
```

1. `LLMClient.extract(text)` returns raw fields. `intake.extract_task()`
   validates them with a strict Pydantic model:
   - `extra="forbid"` rejects injected action fields;
   - `sku_id` / `spec` / `target_price` must be present and non-empty;
   - the price is bounded and normalized to cents;
   - a missing reason produces a warning and a default, never a guessed price.
2. The draft is **not persisted** and is not a token of authorization. The
   caller confirms it through `POST /tasks`, where `check_inventory`,
   `get_listing`, approval, stock recheck, and publish rules run unchanged.
3. An unknown or nonexistent SKU is not resolved at intake (that would require
   a tool call and a guess). It fails safely in `check_inventory` as
   `ABORTED_NOT_FOUND`, with zero writes.
4. The intake module never receives a `ToolClient`. It cannot read inventory,
   stage a price, approve, or publish, regardless of the prompt.
5. The provider is optional and off by default:
   - `STOCK_PILOT_LLM=off` returns 503 with an actionable message;
   - `STOCK_PILOT_LLM=anthropic` uses the official SDK's structured-output
     helper (`messages.parse` with a Pydantic schema), one request, no SDK
     retries, bounded by `STOCK_PILOT_LLM_TIMEOUT`;
   - the API key comes only from the local environment, is marked
     `repr=False`, and is never logged; errors carry no prompt or key;
   - tests inject a fake client, so CI performs no network calls and needs no
     credentials.

## Consequences

- The interview answer to "where is the model?" becomes concrete: at the
  edge, turning text into a *proposal*; the deterministic core is unchanged.
- Prompt injection can at worst produce a wrong draft. It cannot publish:
  approval and the write guards still stand, and invalid fields are rejected
  before a task exists.
- Intake adds one failure mode — upstream model errors — mapped to stable
  HTTP statuses (503 unavailable/timeout, 502 invalid output). No task is
  created on failure.
- Not implemented: a console text box, model-assisted SKU disambiguation, and
  any provider other than Anthropic. Ambiguity still goes to the human mapping
  interrupt; the model is never asked to pick a listing.
