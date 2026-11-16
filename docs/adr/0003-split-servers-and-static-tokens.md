# ADR 0003: Split inventory/mall HTTP MCP servers; keep the approval plane local

## Context

The first version ran one stdio MCP server with all four tools. A production
topology separates inventory and mall ownership, and write endpoints need
identity. The approval queue is a human admin surface rather than a model
tool, so splitting it would add a service without adding an MCP contract.

## Decision

- `stock_pilot.mcp_inventory` serves `check_inventory` over Streamable HTTP
  (default port 8101).
- `stock_pilot.mcp_mall` serves `get_listing`, `update_price`, and
  `publish_listing` over Streamable HTTP (default port 8102).
- Both servers require `Authorization: Bearer <STOCK_PILOT_AGENT_TOKEN>` when
  the token is configured. The stdio server remains the default demo path.
- The orchestrator uses `SplitMCPToolClient` to route each tool to its server.
- The approval queue, decisions, and manual withdrawal stay in the API
  process. The two tool servers share the demo SQLite file; production needs
  independent stores and a saga.

## Authentication

Static bearer tokens map to three roles:

- `agent`: may submit tasks.
- `operator`: may approve/reject, confirm mappings, recover tasks, withdraw
  proposals.
- `admin`: covers both.

Any configured token turns enforcement on. `submitted_by`, `decided_by`, and
`abandoned_by` come from the token principal, never from the request body.
Read-only endpoints stay open for the local console by default;
`STOCK_PILOT_REQUIRE_READ_AUTH=true` requires a valid token for them too
(`/healthz` stays open for probes). In production they should also sit behind
a gateway.

Role separation means an agent cannot approve what it submitted. Admin covers
both roles as break-glass; self-approval is allowed there but emits a warning
and is visible as `submitted_by == decided_by`.

## Consequences

- The split is a real topology exercise, but the shared database is a demo
  shortcut; commit atomicity would need compensation across services.
- Tokens are coarse and not rotated. OIDC and per-operator identities are the
  next step.
- Tests spin both servers in-process and cover routing, 401, and safe
  termination when the client is unauthorized.
