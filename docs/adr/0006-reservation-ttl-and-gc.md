# ADR 0006: Advisory reservation TTL and a conservative sweep

## Context

ADR 0005 introduced an internal stock reservation to replace the
in-transaction inventory check that the split layout cannot perform. The
publish saga releases that reservation in a `try/finally`, but the release is
best effort: if the process dies between `reserve_inventory` and
`release_inventory`, the row stays reserved and every later proposal for the
same inventory record fails with `STOCK_RESERVED`.

A naive TTL that releases expired reservations would be unsafe. Clock skew, a
slow approval resume, or a paused process can make a *live* approved proposal
look old; releasing its stock in the middle of the recheck-to-commit window
would let another task reserve the same unit. The sweep must therefore decide
from approval and task state, not from age alone.

## Decision

### TTL is advisory

`STOCK_PILOT_RESERVATION_TTL` (default 900s) is used only to *report* an
active reservation as `reservation_stale`. It never authorizes a release.
`reserve_inventory` returns `reserved_at` and `expires_at` so the age is
visible; `list_reservations` returns the same view for each held record.

### Sweep rule

`POST /reservations/gc` (operator; open in local no-token mode) lists live
reservations through the inventory tool boundary, reads the approval from the
local approval plane, and reads the task from the orchestrator store.
`GET /reservations` exposes the read-only list.

| Approval / task state | Action |
|---|---|
| approval `consumed` / `rejected` / `abandoned` | release |
| approval missing | release |
| approval `approved`, no task | release |
| approval `approved`, task terminal | release |
| approval `approved`, task active, age ≤ TTL | retain |
| approval `approved`, task active, age > TTL | retain, report `reservation_stale` |
| approval `pending` (anomalous: reservations are created after approval) | retain, report `reservation_stale` |

Each release goes through `release_inventory`, which is idempotent and only
clears rows reserved by that approval. A failed release is reported as
`failed` and can be retried; a partially successful sweep still returns the
per-item result. The sweep never calls `publish_listing` or any other write
outside the release step.

### Why not a background timer that force-expires

A force-expiring timer would be a second writer that can invalidate the
reservation guarantee the saga depends on. Scheduling the same conservative
sweep periodically is safe; changing its rule is not. Production can run
`POST /reservations/gc` from a cron/ops job without changing semantics.

## Consequences

- Recovery reports `reservation_orphaned` (safe to release) and
  `reservation_stale` (needs a human) alongside task and staging issues.
- `/metrics` exposes `stock_pilot_reservations_open` and
  `stock_pilot_reservations_stale`; both are derived from the inventory store
  and the recovery scan, not from a second counter.
- The approval console offers a one-click sweep on reservation issues.
- Tests cover consumed/abandoned/missing approvals, terminal tasks, an active
  approved reservation that survives the sweep and still publishes, idempotent
  sweeps, operator auth, and stale metrics.
- Remaining limitation: cleanup is operator- or scheduler-triggered, not an
  in-process background task. No notification outbox was added.
