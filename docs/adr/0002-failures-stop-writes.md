# ADR 0002: Failures stop writes; reconcile read-only

## Context

Tool calls can time out or the tool service can disappear. Retrying a write
whose outcome is unknown risks double-applying a price change.

## Decision

- Read-only tools may retry (`STOCK_PILOT_READ_RETRIES`) with a per-tool
  circuit breaker.
- Write tools execute exactly once. No blind retry.
- After a write timeout the agent performs at most one read-only
  `get_listing` reconciliation and then stops. A confirmed commit is adopted
  (including its `approval_id`); an unknown outcome ends in
  `ABORTED_UNKNOWN_STATE`.
- Every tool attempt emits a `tool_events` row (`tool`, `attempt`, `outcome`,
  `error_code`, `latency_ms`) correlated with the task.
- Business failures (`ToolFailure`) return `ok=false` and do not trip the
  circuit breaker.

## Consequences

- Tasks fail closed, sometimes conservatively: a write that actually landed
  but cannot be observed ends as unknown and needs an operator.
- The audit timeline explains why the agent stopped.
- Inventory can change between the pre-publish recheck and commit; the commit
  transaction therefore re-validates stock and rolls back on depletion.
