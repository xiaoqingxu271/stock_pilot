# ADR 0001: Approval-gated two-phase commit

## Context

Repricing is a write that must never happen without human approval, and a
rejected proposal must not leave the mall in a half-applied state (for
example, price changed but listing not published).

## Decision

`update_price` only writes a `staging` row plus an `approvals` row inside one
SQLite transaction. The public listing is untouched. After approval,
`publish_listing` commits the staged price, flips the listing to `published`,
marks staging `committed`, and consumes the approval — all in a single
`BEGIN IMMEDIATE` transaction. Rejection or system withdrawal discards the
staging row; the mall never sees the proposal.

## Consequences

- There is no window where price and publish state disagree.
- A write timeout before commit can leave live staging; recovery reports it as
  `staging_orphaned` instead of guessing.
- `publish_listing` cannot be replayed: a consumed approval is rejected.
- Splitting into independent services later requires a saga; see ADR 0003.

## Alternatives considered

- Direct price write after approval, then a separate publish call: rejected
  because it creates the exact dirty state the business rules forbid.
- Approval stored outside the staging transaction: rejected because a timeout
  could leave one without the other.
