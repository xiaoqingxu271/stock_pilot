# 0009 — Audit export is a read-only projection

## Context

Tasks, approvals, tool attempts, and notification metadata already live in
SQLite, but an operator had to query each endpoint (or open the database) to
review one incident. The README lists audit export as a productionization
direction. The export must not become a second source of truth, must not call
a tool, and must not leak credentials or arbitrary upstream text.

## Decision

Add `GET /audit/export`, restricted to the operator (or admin) role:

```
GET /audit/export?since=<ISO time>&until=<ISO time>&limit=500
  -> AuditExport {
       generated_at, since, until,
       tasks, approvals, tool_events,
       outbox  // notification metadata only
     }
```

1. **Read-only projection.** Every field is read from the authoritative
   tables: tasks/tool_events/outbox from the orchestrator store and approvals
   from the mall store (they share a file in `shared` layout). The endpoint
   opens no write transaction and holds no `ToolClient`.
2. **Bounded window.** `since` is inclusive, `until` exclusive, both normalized
   to UTC; a missing offset is interpreted as UTC. An inverted window returns
   `400`. `limit` is capped at 5000 and applies to each collection
   independently, so callers page by time rather than assuming one global page.
3. **Deterministic order.** Results are ordered by `created_at` then row id
   (tool events/outbox by their own timestamps), oldest first.
4. **No secrets, no payload bodies.** Task and approval views already contain
   only public workflow fields. Outbox rows expose event id, dedupe key, topic,
   task id, timestamps, and attempts — not `payload_json` and not
   `last_error`, which may contain arbitrary upstream text. Tokens and API keys
   are never part of any view.
5. **Honest snapshot semantics.** In `split` layout there is no cross-store
   transaction; a concurrent writer may land between queries. The response
   model documents this. The export is for operator review, not a
   point-in-time backup.
6. **Console access.** The approval console adds an "导出审计" button that
   downloads the JSON using the configured token. It provides no approve,
   publish, or mapping shortcut.

## Consequences

- Operators can attach one JSON snapshot to an incident review without
  touching the database or replaying anything.
- The export cannot mutate tasks, approvals, staging, inventory, or the mall;
  tests assert row counts before and after are identical.
- No new persistence, no outbox, no background job, and no tool call.
- Retention remains owned by the existing tables (and the outbox retention
  pass); the export itself does not archive anything.
- Tests cover the full trail, operator/agent/auth checks, time windows and
  limits, inverted ranges, credential absence, and split-store reads.
