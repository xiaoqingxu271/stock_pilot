# ADR 0005: Split stores and a publish saga with reservation compensation

## Context

ADR 0003 split the inventory and mall MCP servers but let both share one
SQLite file. That shared file was the only reason `publish_listing` could
re-read inventory inside the mall commit transaction and atomically consume
the approval. With independent stores, the publish path crosses three
ownership boundaries:

1. inventory (the commit-time stock guarantee),
2. mall (listing price/status, staging, approval consumption),
3. orchestrator (task state and tool audit).

A distributed transaction is not available. The business rules still forbid
guessing, skipping approval, and retrying a write after an uncertain outcome.

## Decision

`STOCK_PILOT_DB_LAYOUT=split` (or `stock_pilot.demo --split`) uses three
independent SQLite files:

| Store | Tables | Owner |
|---|---|---|
| `orchestrator.db` | `tasks`, `tool_events` | API / graph |
| `inventory.db` | `inventory_records` + reservation columns | inventory MCP server |
| `mall.db` | `listings`, `staging`, `approvals` | mall MCP server |

`STOCK_PILOT_DB` names the orchestrator store; `STOCK_PILOT_INVENTORY_DB` and
`STOCK_PILOT_MALL_DB` override the derived `inventory.db` / `mall.db` siblings.
The default `shared` layout is unchanged, so existing deployments and tests
keep one file and the in-transaction stock gate.

Approvals stay in the mall store. An approval authorizes a mall write, and
`update_price` must keep "staging + approval in one transaction"; moving the
approval to the orchestrator would turn that invariant into a distributed
write without a compensating need. The human approval plane remains in the
orchestrator process but opens the mall store.

### Internal reservation tools

In split mode the inventory server also registers `reserve_inventory` and
`release_inventory`. They are **not** part of the four model-facing tools and
are never exposed to the model; they are saga steps used by the graph:

- reserve: `BEGIN IMMEDIATE`; idempotent for the same `approval_id`; fails
  with `STOCK_RESERVED` for a different approval and `INSUFFICIENT_STOCK` at
  zero; writes `reserved_by` / `reserved_at`.
- release: clears only rows reserved by that approval; idempotent.

Because the mall cannot read `inventory.db`, `publish_listing` accepts
`check_stock=False` and the reservation replaces the in-transaction check.

### Publish saga

```
recheck_stock (inventory read)
  → reserve_inventory (inventory tx)
  → publish_listing (mall tx: listing commit + consume approval + staging)
  → task status update (orchestrator tx)
  → release_inventory (compensation / cleanup, best effort)
```

Failure handling:

| Failure point | Result |
|---|---|
| recheck fails or quantity ≤ 0 | abandon approval (mall tx), no publish, `ROLLED_BACK` |
| reserve fails (zero / reserved / error) | abandon approval, release (no-op), stop before mall |
| mall rejects (approval/staging/listing guard) | abandon approval, stop; no retry |
| mall commit uncertain (timeout / transport) | one read-only `get_listing`; committed at target price → adopt, mark published, verify; otherwise `ABORTED_UNKNOWN_STATE`, never re-publish |
| crash after reserve, before release | reservation leaks (documented limitation; TTL cleanup is future work) |

Compensation never reverts a committed mall fact. If the mall committed, the
orchestrator adopts it; if it did not, the proposal is abandoned and staging
is left for the existing recovery / manual-cleanup path. A lost publish
response is therefore harmless: `publish_calls == 1` and the listing is
verified from the mall.

### No outbox write re-driver

An outbox that re-sends `publish_listing` would violate "never continue writes
after an uncertain outcome". Each cross-store write has exactly one durable
point (the mall commit, which consumes the approval) and at most one read-only
reconciliation. Notifications/audit export can use an outbox later, but it
must not re-drive writes.

## Consequences

- Recovery and metrics join task state with mall state in application code
  instead of SQL; `scan(..., mall_db_path=...)` and `/metrics` do this.
- There are no cross-store foreign keys. `listings.inventory_record_id` is a
  reference checked by the graph's mapping guard, not by SQLite.
- In-process and HTTP split tests cover happy path, reservation semantics,
  reservation routes over MCP, a lost commit response, and a failure before
  commit.
- Remaining limitations: no reservation TTL/GC, no notification outbox,
  SQLite rather than Postgres. The reservation tool set is internal API, not a
  fifth model capability.
