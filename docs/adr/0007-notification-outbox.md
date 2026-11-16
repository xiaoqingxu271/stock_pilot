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
| `created_at`, `delivered_at`, `attempts`, `last_error` | delivery state |

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
- the worker never imports or receives `ToolClient`, so it cannot call
  `publish_listing`, `update_price`, or any other MCP tool.

`GET /metrics` exposes `stock_pilot_outbox_pending` derived from the
orchestrator store. The API lifespan starts and stops the worker; tests and
the demo do not need a separate process.

## Consequences

- Notification delivery is decoupled from the HTTP request and from the graph.
- The same durability rule applies as elsewhere: one durable point (the task
  transaction), then at-most-once *effect* semantics are traded for
  at-least-once delivery with an explicit dedupe key.
- No new write can originate from the outbox. This is enforced structurally
  (no client dependency), not just by convention.
- Remaining limits: only a log sender ships today; no dead-letter queue or
  retention/GC for delivered rows; an external webhook/SNS sender can be
  added behind the same interface without changing the write-side rules.
- Tests cover atomic enqueue and rollback, non-terminal no-op, dedupe,
  delivery once, sender failure and retry, the pending metric, and lifespan
  wiring end to end.
