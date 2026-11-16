# ADR 0007: A notification-only outbox that never re-drives writes

## Context

Downstream consumers want to know when a task reaches a terminal state. The
usual answer is a transactional outbox. The usual outbox implementation is
also a write driver: a worker retries undelivered messages until they succeed.

That conflicts with ADR 0002 and ADR 0005: once a write outcome is uncertain,
the system stops writing and reconciles read-only. Re-sending
`publish_listing` or `update_price` from an outbox could duplicate a committed
mall write and would make the retry policy depend on an at-least-once queue.

## Decision

Add an outbox for **notifications only**, in `orchestrator.db`:

| Column | Purpose |
|---|---|
| `dedupe_key` (unique) | `task-terminal:<task_id>:<status>`; repeated terminal updates keep one event |
| `topic` | currently `task.terminal` |
| `task_id`, `payload_json` | whitelisted task facts (sku, spec, status, listing/approval ids, final price cents, error code) |
| `created_at`, `delivered_at`, `attempts`, `last_error`, `dead_lettered_at` | delivery state |

### Enqueue is transactional with the task

`tasks.update_task()` writes the terminal status and calls
`notifications.enqueue_terminal(tx, task_id)` inside the same
`BEGIN IMMEDIATE` transaction. A task that reached a terminal state always has
its event; a failed enqueue rolls the task update back. Repeating a terminal
update is idempotent via the unique `dedupe_key`.

### The worker has no tool client

`OutboxWorker` is constructed with a database path and a
`NotificationSender` only. Its default sender writes one structured log line;
tests inject a recording or failing sender. The worker calls
`deliver_pending()`, which sends, then marks delivered:

- crash after send, before mark → one duplicate on the next pass
  (at-least-once; consumers dedupe by `dedupe_key`);
- sender failure → row stays pending, `attempts` increments, `last_error` is
  recorded, the next pass retries;
- after `STOCK_PILOT_OUTBOX_MAX_ATTEMPTS` failures (default 60, about two
  minutes at the default poll interval) the row is dead-lettered via
  `dead_lettered_at` and excluded from later passes. It is visible to
  `stock_pilot_outbox_dead` and stays in the table for inspection;
- the worker never imports or receives `ToolClient`, so it cannot call
  `publish_listing`, `update_price`, or any other MCP tool.

### Senders and dead-letter operations

`LogSender` is the default. Setting `STOCK_PILOT_WEBHOOK_URL` switches to
`WebhookSender`, which POSTs the event with a bounded timeout and an optional
HMAC-SHA256 signature (`X-StockPilot-Signature: sha256=...`). Delivery errors
never include the URL, so a token embedded in it cannot reach a log.

`GET /outbox` lists pending, delivered, or dead events (read-only).
`POST /outbox/{id}/retry` (operator) clears `dead_lettered_at`, resets
`attempts`, and lets the worker try again. It is idempotent for pending rows
and refuses delivered rows. Neither endpoint imports or receives a tool
client; a retry can only re-send a notification.

### Retention

Delivered and dead-lettered rows are pruned after
`STOCK_PILOT_OUTBOX_RETENTION` (default 7 days). Pruning never touches pending
rows and never touches `tasks` or `tool_events`; the notification row is not
the audit record. The worker runs the prune at most once per hour.

`GET /metrics` exposes `stock_pilot_outbox_pending` and
`stock_pilot_outbox_dead`, both derived from the orchestrator store. The API
lifespan starts and stops the worker; tests and the demo do not need a
separate process.

## Consequences

- Notification delivery is decoupled from the HTTP request and from the graph.
- The same durability rule applies as elsewhere: one durable point (the task
  transaction), then at-most-once *effect* semantics are traded for
  at-least-once delivery with an explicit dedupe key.
- No new write can originate from the outbox. This is enforced structurally
  (no client dependency), not just by convention.
- Remaining limit: the webhook sender is generic HTTP; provider-specific
  adapters (SNS, event bus) can be added behind the same interface without
  changing the write-side rules.
- Tests cover atomic enqueue and rollback, non-terminal no-op, dedupe,
  delivery once, sender failure and retry, dead-lettering after the attempt
  budget, retention pruning, webhook signing, dead-letter retry auth and
  idempotency, both metrics, and lifespan wiring end to end.
