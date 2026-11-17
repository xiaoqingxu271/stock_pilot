# Changelog

All notable changes to StockPilot are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-17

First public, interview-ready release. The core promise: a constrained
inventory-driven repricing chain that stops before any uncertain write and
requires human approval for every price/publish change.

### Added

- MCP tool contract with four model-facing tools: `check_inventory`,
  `get_listing`, `update_price`, and `publish_listing`.
- LangGraph state machine:
  `check_stock → resolve_listing → stage_price → await_approval → recheck_stock
  → publish → verify`.
- Two-phase approval commit: `update_price` writes staging + approval in one
  transaction; `publish_listing` commits price, listing status, and approval
  consumption atomically.
- Ambiguous listing handling: tasks suspend with candidate listings and never
  guess; a human confirms the mapping.
- Read-only reconciliation after write timeouts; no write is ever replayed.
- Publish-time inventory recheck plus a final in-transaction stock gate.
- Restart recovery: `GET /recovery` reports interrupted states,
  `POST /tasks/{id}/recover` continues only approved work or re-verifies a
  committed task.
- Idempotent task submission (`Idempotency-Key`) and idempotent approval
  decisions.
- Real MCP transports: stdio and two split Streamable HTTP servers with
  static Bearer-token roles.
- Split store layout: orchestrator / inventory / mall SQLite files with an
  internal reservation saga and a conservative reservation sweep.
- Notification-only outbox with at-least-once delivery, webhook sender,
  dead-lettering, retention pruning, and an operator retry that cannot re-drive
  a business write.
- Prometheus metrics, JSON structured logging, MCP session ping/reconnect, and
  `/healthz` readiness.
- Optional natural-language intake: model extracts parameters only, the draft
  is never persisted, and confirmation re-runs every guard.
- Approval console: queue, mapping confirmation, task trail, recovery panel,
  intake draft panel, audit export, and notification delivery panel.
- Read-only audit export across orchestrator and mall stores, with no
  credentials or notification payloads.
- Per-identity, process-local rate limiting for the model-backed intake
  endpoint (`429` + `Retry-After`, no model call on rejection).
- One-command demo launcher with seeded data, optional split topology, and a
  fresh-database switch.

### Safety invariants

- Inventory ≤ 0 terminates before any write.
- Ambiguous mappings suspend the task; the system never chooses a listing.
- Every price/publish write requires an approval decision.
- A rejected or abandoned proposal leaves no staged price and no mall write.
- An uncertain write stops the chain and permits at most one read-only
  reconciliation.
- The notification worker holds no tool client.

### Verification

- 176 tests cover the tool contract, agent graph, API, UI assets, MCP stdio and
  HTTP transports, auth, concurrency, recovery, split stores, reservations,
  outbox, webhook, intake, rate limiting, and audit export.
- `ruff check src tests` passes.
- GitHub Actions runs Ubuntu and Windows against Python 3.10 and 3.12.

### Known limitations

- SQLite is a single-instance demo store, not a distributed database.
- Static Bearer tokens are demo-grade identity; OIDC is not included.
- Intake rate limiting is per process, not a shared quota.
- Metrics and JSON logs are implemented but not wired to an alerting or log
  aggregation platform.
- The optional intake provider is Anthropic-only.

[0.1.0]: https://github.com/xiaoqingxu271/stock_pilot/releases/tag/v0.1.0
