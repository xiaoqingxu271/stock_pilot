# 0010 — Intake rate limiting is a process-local cost guard

## Context

`POST /tasks/intake` is the only endpoint that can spend money on an upstream
model. It already returns a draft with no authority, but a caller (or a stuck
retry loop) could still drive unlimited provider requests. The rest of the
system is deterministic and local; a shared quota store would be a large new
dependency for a demo.

## Decision

Add a sliding-window limiter in front of the model call:

```
STOCK_PILOT_INTAKE_RATE_LIMIT=10      # attempts per identity
STOCK_PILOT_INTAKE_RATE_WINDOW=60     # rolling window in seconds
```

1. **Scope.** Only `POST /tasks/intake` is limited. `POST /tasks`, approval,
   mapping, recovery, audit, and outbox endpoints are untouched.
2. **Identity.** With auth enabled the bucket key is the principal name
   (`agent` / `admin`); without tokens all callers share the `local` bucket.
   The key is not an IP address, because a reverse proxy makes that unreliable.
3. **No cost on rejection.** The limiter is checked after the disabled check
   and before `LLMClient.extract()`. A rejected request makes no provider
   call, creates no task, writes nothing, and returns
   `429` with `Retry-After` plus a `RATE_LIMITED` error detail.
4. **Only accepted attempts count.** Rejected attempts are not recorded;
   otherwise a client that keeps retrying would extend its own lockout. The
   window slides, so capacity returns as the oldest accepted attempt expires.
5. **Thread safety.** A `threading.Lock` guards the per-key deque. The API may
   serve concurrent requests from a thread pool.
6. **Disable.** `STOCK_PILOT_INTAKE_RATE_LIMIT=0` turns limiting off. A
   non-positive window is rejected at startup.
7. **Honest topology.** This is not a distributed quota. Each process has its
   own counters, so a multi-instance deployment must enforce the real limit in
   a gateway or shared store. That limitation is documented rather than
   hidden behind a false guarantee.

## Consequences

- The demo can show a real cost guard without Redis or another service.
- A burst over the limit still leaves zero task/staging/approval rows; tests
  assert this and assert the model client was called only for accepted
  requests.
- The limiter is orthogonal to write safety: it never approves, publishes, or
  calls a tool. It only decides whether the model extraction step starts.
- When the limit is reached, the console surfaces the 429 detail as an error
  toast; the manual task form remains available.
- Tests use an injected clock, cover window recovery, identity isolation,
  thread safety, startup validation, API 429 semantics, and per-identity
  buckets with auth enabled.
