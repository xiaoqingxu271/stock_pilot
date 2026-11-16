# ADR 0004: Restart recovery is read-only by default

## Context

A process can die between a human decision and graph resume, between a mall
commit and verification write-back, or after a write timeout with live
staging. Automatically guessing the intended next step would violate the
"no writes after an unknown outcome" rule.

## Decision

- `GET /recovery` performs a read-only scan over non-terminal tasks and live
  `price_staged` rows, reporting explicit kinds such as
  `approved_not_published`, `rejected_not_closed`, `published_verify_pending`,
  and `staging_orphaned`.
- `POST /tasks/{id}/recover` handles only two cases:
  - `awaiting_approval` with a decided approval: resume the graph with
    `Command(resume=...)`. An approved proposal must still have live staging.
  - `published`: run a read-only verification pass and write back
    `verification_json`; never call `publish_listing` again.
- `POST /approvals/{id}/abandon` is the only way to clear an orphaned
  proposal. It requires an approved (or already abandoned) approval, performs
  a mandatory read-only reconciliation, and refuses when the mall already
  shows the proposed price and `published` status.
- Approval state in the database is authoritative; the resume payload only
  wakes the graph.

## Consequences

- After a restart, the operator sees exactly what needs attention and decides
  whether to continue or clean up.
- No recovery path introduces a second write decision.
- A mismatch observed during verification ends in `failed_verification` and
  preserves the evidence instead of silently repairing it.
