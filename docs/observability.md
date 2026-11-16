# 观测性：指标、日志、告警与重连

StockPilot 的观测面全部从权威状态派生：任务与工具时间线来自 SQLite，
熔断与传输可用性来自内存中的执行器；不维护第二套计数，避免指标与审计漂移。

## 指标

`GET /metrics` 输出 Prometheus 文本格式（`text/plain; version=0.0.4`），
每次抓取构建独立 registry，可以直接挂到多个进程实例上。

| 指标 | 类型 | 标签 | 含义 |
|---|---|---|---|
| `stock_pilot_tasks_total` | counter | `status` | 按当前状态统计的任务数 |
| `stock_pilot_tool_events_total` | counter | `tool` / `outcome` / `error_code` | 工具尝试次数；仅统计带任务的编排侧调用 |
| `stock_pilot_tool_latency_ms` | histogram | `tool` | 工具尝试耗时（毫秒），桶 10ms → 10s |
| `stock_pilot_approvals_pending` | gauge | — | 等待人工决策的审批数 |
| `stock_pilot_recovery_issues` | gauge | — | 需要人工关注的中断态任务 + 遗留 staging |
| `stock_pilot_circuit_open` | gauge | `tool` | 熔断器当前是否拒绝调用（1/0） |
| `stock_pilot_tool_client_available` | gauge | — | 工具客户端是否持有可用会话（1/0） |

MCP 模式下，工具服务端也会把尝试写入共享演示库；服务端行没有 `task_id`，
因此指标只统计 `task_id IS NOT NULL` 的编排侧记录，避免双倍计数。

`split` 布局下任务与工具事件在 `orchestrator.db`，审批在 `mall.db`；
collector 分别读取两个库再组装，不需要跨库 SQL。

`GET /healthz` 与指标同源：数据库不可读或工具客户端不可用时返回 `503` 与
`status=degraded`，同时暴露 `pending_approvals`、`recovery_issues`。

## 告警规则示例

```yaml
groups:
  - name: stock-pilot
    rules:
      - alert: StockPilotToolClientDown
        expr: stock_pilot_tool_client_available == 0
        for: 1m
        labels: { severity: critical }
        annotations:
          summary: 工具客户端不可用，写任务会被安全终止

      - alert: StockPilotCircuitOpen
        expr: stock_pilot_circuit_open == 1
        for: 2m
        labels: { severity: critical }
        annotations:
          summary: "工具 {{ $labels.tool }} 熔断器持续打开"

      - alert: StockPilotToolTimeoutRate
        expr: |
          sum(rate(stock_pilot_tool_events_total{outcome="timeout"}[5m]))
            /
          sum(rate(stock_pilot_tool_events_total[5m])) > 0.05
        for: 10m
        labels: { severity: warning }
        annotations:
          summary: 工具超时比例超过 5%

      - alert: StockPilotToolLatencyP95
        expr: |
          histogram_quantile(
            0.95,
            sum by (le) (rate(stock_pilot_tool_latency_ms_bucket[5m]))
          ) > 2000
        for: 10m
        labels: { severity: warning }

      - alert: StockPilotApprovalBacklog
        expr: stock_pilot_approvals_pending > 0
        for: 30m
        labels: { severity: warning }
        annotations:
          summary: 审批积压超过 30 分钟

      - alert: StockPilotRecoveryIssues
        expr: stock_pilot_recovery_issues > 0
        for: 5m
        labels: { severity: warning }
        annotations:
          summary: 存在中断态任务或遗留提案，检查 GET /recovery
```

超时率分母为 0 时比值不产生样本，规则自然不会触发。熔断告警只说明调用被
拒绝，真正的处置是查 `/tasks/{id}/events` 与恢复面板，而不是重发写操作。

## 结构化日志

默认输出面向本地开发的可读文本；设置 `STOCK_PILOT_LOG_FORMAT=json` 后每条日志
是一个 JSON 对象：

```json
{"ts":"2026-11-16T10:17:18.126179+00:00","level":"INFO","logger":"stock_pilot.runner","message":"tool attempt","task_id":"task_18744f5983854f11","tool":"publish_listing","attempt":1,"outcome":"unavailable","error_code":"TOOL_UNAVAILABLE","latency_ms":4}
```

字段说明：

- `task_id`：由 `audit.task_context()` 注入；工具尝试日志自动携带。
- `tool` / `attempt` / `outcome` / `error_code` / `latency_ms`：与 `tool_events` 一致。
- 只输出白名单字段。Authorization 头、Token、请求体不会进入日志。
- `STOCK_PILOT_LOG_LEVEL` 控制级别（默认 `INFO`）。

`configure_logging()` 在 API 与三个 MCP Server 入口启动时安装单一 stderr handler。

## 重连

`MCPToolClient`（stdio 与 Streamable HTTP 共用）在后台线程持有会话：

- 每 2 秒发送一次 MCP ping；ping 失败说明对端已不可用，立即卸载会话。
- 重连使用 0.5s 起的指数退避，上限 10s；成功后重置退避。
- 重连只恢复传输，**从不重放写操作**。断开期间的调用快速失败为
  `TOOL_UNAVAILABLE`（或已打开的 `CIRCUIT_OPEN`），由编排层按「状态未知、
  任务中止」处理。
- `close()` 取消会话、ping 与退避等待，不阻塞进程退出。

`/healthz` 与 `stock_pilot_tool_client_available` 在会话断开瞬间转为不健康，
重连成功后自动恢复；期间提交的新任务会在第一次工具失败处安全终止。

集成测试 `tests/test_reconnect.py` 覆盖：商城 Server 停机 → 任务安全中止 →
同端口重启 → 客户端自动恢复 → 新任务完成，且审计中只有一次成功的
`publish_listing`。
