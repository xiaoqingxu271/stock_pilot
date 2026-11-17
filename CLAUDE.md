# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目目标

StockPilot：基于 MCP 的库存驱动电商调价上架 Agent。

跑通一条受约束的跨系统任务链：

```
check_inventory（只读）→ get_listing（只读）→ update_price（写，需审批）→ publish_listing（写，需审批）→ 最终状态核对
```

## 技术栈

- Python 3.10+
- MCP（Model Context Protocol），使用官方 Python SDK 或 FastMCP
- LangGraph（Agent 状态机）
- FastAPI（服务封装）
- SQLite（模拟库存系统 + 商城系统 + 审批队列）

## 核心业务规则（不可违背）

1. 库存 ≤ 0 → 任务终止，**不执行任何写操作**。
2. 商品映射存在歧义（多个同名商品）→ **挂起任务**，输出候选列表等待人工确认，**禁止猜测**。
3. `update_price` 和 `publish_listing` **必须经过审批队列**，审批通过后才实际写入。
4. 任何工具调用超时 → 设置最大重试次数 + 熔断，超时后降级为"状态未知，任务中止"，**禁止继续后续写操作**。
5. 审批拒绝 → 任务回滚为"未执行"，**不得留下"价格已改但未上架"的中间脏状态**。

## MCP 工具契约（最小 4 个）

| 工具 | 类型 | 输入 | 输出 | 关键约束 |
|---|---|---|---|---|
| check_inventory | 只读 | sku_id, spec | quantity, timestamp | 必须返回具体规格，不能只返回"有货" |
| get_listing | 只读 | sku_id | 三态结果：`resolved`（price, status, inventory_record_id, pending）/ `ambiguous`（候选列表，每个候选也含 pending）/ `not_found` | 用于确认商品映射，防止同名不同款；`pending` 是写超时后的只读对账依据 |
| update_price | 写入（审批） | listing_id, new_price, proposal_reason | approval_id | 不直接改价，只写 staging 并创建审批记录（同一事务）；审批通过前商城不可见 |
| publish_listing | 写入（审批） | listing_id, approval_id | final_status | 审批通过后调用；在同一事务内原子提交「改价 + 上架」并消费 approval_id |

split 布局下库存 Server 额外注册内部工具 `reserve_inventory` / `release_inventory` / `list_reservations`（输入 inventory_record_id + approval_id / approval_id / 无参数），只供编排层 saga 与人工清理使用，不交给模型，不改变上述 4 个模型可用工具的契约。

## 已确认的设计决策

1. **最终核对**：复用 `get_listing` 再读一次，不新增工具；核对结果写入任务记录。
2. **审批粒度**：一次审批覆盖「调价 + 上架」组合提案；审批人通过 FastAPI 操作（提交任务 / 审批决策 approve|reject / 映射确认，共三个接口）。
3. **回滚方式**：两阶段提交。`update_price` 只写 staging；审批通过后由 `publish_listing` 在同一事务内原子提交价格与上架。审批拒绝时丢弃 staging，商城侧从未发生写入。
4. **写超时**：允许一次只读对账（复用 `get_listing`，依据其 `pending` 字段判断 staging/审批/提交状态），禁止任何后续写操作。
5. **`get_listing` 契约**：三态 `resolved / ambiguous / not_found`，仍是同一个只读工具。
6. **目标价来源**：调用方提交任务时给定 `sku_id`、`spec`、目标价、理由；Agent 只负责安全执行，不自主定价。
7. **MCP 拓扑**：第一版一个 MCP Server，内部按 module 隔离库存 / 商城 / 审批三块数据；B1 增加拆分部署——库存与商城各一个 Streamable HTTP MCP Server，编排层按工具路由，审批面仍留在编排进程（人机管理面不是模型工具）。默认仍是单库演示；`STOCK_PILOT_DB_LAYOUT=split` 时三库隔离（见决策 14）。
8. **发布前库存复核**：审批通过后、`publish_listing` 前用现有 `check_inventory` 再读一次。库存 ≤ 0 → 不提交商城，审批置 `abandoned` 并在同一事务丢弃 staging，任务 `ROLLED_BACK` / `INSUFFICIENT_STOCK`。复核工具失败同样撤回提案，避免「任务已终止但 staging 仍活跃」。
9. **重启恢复与幂等**：审批状态以数据库为准，进程重启后可从 checkpoint 继续；`GET /recovery` 只读标注异常态；`POST /tasks/{id}/recover` 对 `awaiting_approval` 任务继续图，对 `published` 任务只读补核对；`Idempotency-Key` 与重复审批返回原结果。
10. **提交时兜底与人工清理**：单库模式下 `publish_listing` 在提交事务内再次校验库存（复核读数可能已过期），不足则整个事务回滚、零写入；split 模式由库存预留承担同一职责（`check_stock=False`）。写超时遗留的 `price_staged` 只由人工经 `POST /approvals/{id}/abandon` 清理，接口必须先只读对账，商城已提交则拒绝撤回。`GET /healthz` 暴露所有存储与工具客户端就绪状态。
11. **鉴权与拆分（B1）**：静态 Bearer Token 映射 agent / operator / admin 三角色；任一 Token 配置即强制校验，身份写入 `submitted_by` / `decided_by` / `abandoned_by`。库存与商城各一个 Streamable HTTP MCP Server，只接受 agent Token；审批面仍留在编排进程。只读接口默认开放，`STOCK_PILOT_REQUIRE_READ_AUTH=true` 时纳入鉴权。正常流程靠角色分离保证审批人 ≠ 提交人；admin 自审（break-glass）会写 warning 日志，`submitted_by == decided_by` 可在审计中识别。
12. **并发不变量**：所有写路径使用 `BEGIN IMMEDIATE` 串行化；每个 listing 的活动 staging 由部分唯一索引兜底；审批消费与商城提交在同一事务。并发初始化同一演示库时在写事务内复查行数，只有一次 seed；schema 创建对 `database is locked` 短暂重试。测试覆盖并发 staging / 相反审批 / 并发发布 / 相同幂等键提交 / 并发初始化，要求每次只有一个赢家。
13. **观测与重连（E2）**：指标只从权威状态派生（SQLite 任务与工具事件、执行器熔断快照），工具事件只统计带 `task_id` 的编排侧记录，避免 MCP 双写重复计数；JSON 日志只输出白名单字段，不含 Token。MCP 客户端用 2 秒 ping 探测会话、0.5s→10s 退避重连；重连只恢复传输，禁止重放写操作。
14. **三库与发布 saga（E3）**：split 布局下 `orchestrator.db`（tasks/tool_events）、`inventory.db`（inventory_records + `reserved_by`/`reserved_at`）、`mall.db`（listings/staging/approvals）彼此独立。审批与 mall 写同库，保证 staging + approval 原子；发布链路为「复核 → `reserve_inventory` → 商城事务提交 → 回写任务 → 尽力释放预留」。商城已提交但响应丢失时，只做一次只读 `get_listing` 并采纳已提交事实，绝不重发写；补偿只释放预留、撤回未提交提案，不回滚已提交事实。刻意不引入会重投写操作的 outbox（仅通知的 outbox 见决策 16）。详见 `docs/adr/0005`。
15. **预留 TTL 与清理（E5）**：`STOCK_PILOT_RESERVATION_TTL`（默认 900s）只是观测门槛，不是释放许可。`POST /reservations/gc` 只释放审批已 `consumed` / `rejected` / `abandoned`、审批缺失或任务已终态的预留；审批仍可发布且任务活跃的预留绝不自动释放，超龄只标记 `reservation_stale`。`GET /reservations` 只读列出预留；恢复扫描与指标分别暴露 `reservation_orphaned` / `reservation_stale` 与 `stock_pilot_reservations_open` / `stock_pilot_reservations_stale`。split 布局可设置 `STOCK_PILOT_RESERVATION_GC_INTERVAL`（默认 0 关闭）让 lifespan 周期执行**同一个** `sweep()`；改变的是触发方式，不是释放规则，且没有多实例选主。详见 `docs/adr/0006`。
16. **通知 outbox（P2）**：`outbox_events` 只在编排库；任务终态与事件在同一事务写入，`dedupe_key` 唯一。worker 只持有数据库路径与 `NotificationSender`，不持有 `ToolClient`，因此结构上不能重投写操作；投递至少一次，失败保留 pending 并记录 `attempts` / `last_error`。连续失败达到 `STOCK_PILOT_OUTBOX_MAX_ATTEMPTS`（默认 60，默认轮询下约 2 分钟）后写 `dead_lettered_at` 转死信，不再无限重试；已投递/死信行超过 `STOCK_PILOT_OUTBOX_RETENTION`（默认 7 天）由 worker 清理，pending 行永不清理。发送器默认只写结构化日志，配置 `STOCK_PILOT_WEBHOOK_URL` 后改为带超时与可选 HMAC 签名的 webhook；`GET /outbox` 只读、`POST /outbox/{id}/retry` 由 operator 重投死信，仍然不接触任何工具。详见 `docs/adr/0007`。
17. **自然语言入口（P5）**：`POST /tasks/intake` 是可选层，默认关闭（`STOCK_PILOT_LLM=off`，返回 503）。模型只做参数抽取，输出经 Pydantic 严格校验后返回 `TaskDraft`（`sku_id` / `spec` / `target_price` / `reason` / `warnings`），**不落库、不调任何工具、不授予任何权限**；调用方确认后仍走 `POST /tasks`，库存、映射、审批、超时规则全部重跑。未知 SKU 不在入口处猜测，而是在 `check_inventory` 安全失败。开启 `anthropic` 需要 `STOCK_PILOT_LLM_API_KEY`，密钥只在本地环境，不写日志、不入库。审批台「自然语言草稿」面板只读展示草稿，点「确认提交任务」才把四个任务字段发给 `POST /tasks`，不提供直接审批/发布按钮；草稿只存在页面内存，刷新即丢弃。只读 `GET /capabilities` 返回 `intake_enabled` / `llm_model` / `db_layout` / `auth_enabled`，不含 Key。详见 `docs/adr/0008`。
18. **审计导出（P6）**：`GET /audit/export`（operator/admin）是只读投影，仅从权威表读取任务、审批、工具事件与通知元数据；`since` 左闭、`until` 右开、按 UTC 归一，倒置窗口 400，`limit` 每类集合独立且上限 5000。输出不含 Token / API Key，也不返回通知 `payload` 与 `last_error` 自由文本；不调用工具、不开写事务。split 下编排库与商城库分别读取，没有跨库事务，快照可能被并发写插入。审批台顶栏「导出审计」按钮下载 JSON。详见 `docs/adr/0009`。

## 验收用例（至少 5 条，每次改动后回归）

1. **Happy path**：调用方给定目标价 59 元、库存充足 → 审批通过 → 价格变为 59 元 → 状态上架 → 最终核对一致。
2. **库存不足**：库存 ≤ 0 → 任务终止，写操作零执行。
3. **映射歧义**：get_listing 返回多个同名商品 → 任务挂起，输出候选列表，不执行写操作。
4. **审批拒绝**：update_price 审批被拒 → 任务回滚，无中间脏状态。
5. **审批期间库存归零**：审批通过后、发布前复核发现库存 ≤ 0 → 不提交商城；审批标记 `abandoned`、staging 丢弃、任务 `ROLLED_BACK` / `INSUFFICIENT_STOCK`。

## 代码规范

- 所有 MCP 工具使用 Pydantic 模型定义输入/输出 schema。
- 错误码使用枚举，禁止裸字符串。
- 每个工具调用必须有超时设置。
- 提交前运行验收用例（至少覆盖上述 5 条）。

## 禁止事项

- 不要跳过审批直接执行写操作。
- 不要在工具调用失败后"继续尝试下一步"。
- 不要在 `get_listing` 映射不明确时自行选择商品。

## 常用命令

首次安装（Windows Git Bash；POSIX 用 `.venv/bin/python`）：

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

```bash
# 全量验收回归
./.venv/Scripts/python.exe -m pytest -q

# Lint
./.venv/Scripts/python.exe -m ruff check src tests

# 单条用例
./.venv/Scripts/python.exe -m pytest tests/test_agent_acceptance.py::test_happy_path_verified -q

# 分层运行
./.venv/Scripts/python.exe -m pytest tests/test_tools_acceptance.py -q
./.venv/Scripts/python.exe -m pytest tests/test_agent_acceptance.py -q
./.venv/Scripts/python.exe -m pytest tests/test_api.py -q

# 一条命令启动演示（建库 + API；--split 改用两个 HTTP MCP Server，--fresh 重建库）
./.venv/Scripts/python.exe -m stock_pilot.demo
# 浏览器打开 http://127.0.0.1:8000/ 即审批台

# 只启动 FastAPI（默认 127.0.0.1:8000，工具进程内调用）
./.venv/Scripts/python.exe -m stock_pilot.api

# 启动 FastAPI，四个工具真实走 MCP stdio（客户端自动拉起子进程）
STOCK_PILOT_TOOL_CLIENT=mcp ./.venv/Scripts/python.exe -m stock_pilot.api

# 单独启动 MCP Server（stdio；通常由 MCPToolClient 自动拉起）
./.venv/Scripts/python.exe -m stock_pilot.mcp_server

# B1 拆分：库存 / 商城各一个 Streamable HTTP MCP Server，编排进程按工具路由
export STOCK_PILOT_AGENT_TOKEN=demo-agent-token
export STOCK_PILOT_OPERATOR_TOKEN=demo-operator-token
./.venv/Scripts/python.exe -m stock_pilot.mcp_inventory &  # 127.0.0.1:8101/mcp
./.venv/Scripts/python.exe -m stock_pilot.mcp_mall &       # 127.0.0.1:8102/mcp
STOCK_PILOT_TOOL_CLIENT=mcp-http ./.venv/Scripts/python.exe -m stock_pilot.api
```

配置通过环境变量覆盖：`STOCK_PILOT_DB`、`STOCK_PILOT_DB_LAYOUT`（`shared` 默认 / `split`）、`STOCK_PILOT_INVENTORY_DB`、`STOCK_PILOT_MALL_DB`、`STOCK_PILOT_TOOL_TIMEOUT`、`STOCK_PILOT_READ_RETRIES`、`STOCK_PILOT_CIRCUIT_THRESHOLD`、`STOCK_PILOT_CIRCUIT_COOLDOWN`、`STOCK_PILOT_RESERVATION_TTL`、`STOCK_PILOT_RESERVATION_GC_INTERVAL`、`STOCK_PILOT_OUTBOX_INTERVAL`、`STOCK_PILOT_OUTBOX_MAX_ATTEMPTS`、`STOCK_PILOT_OUTBOX_RETENTION`、`STOCK_PILOT_WEBHOOK_URL`、`STOCK_PILOT_WEBHOOK_SECRET`、`STOCK_PILOT_WEBHOOK_TIMEOUT`、`STOCK_PILOT_LLM`（`off` 默认 / `anthropic`）、`STOCK_PILOT_LLM_MODEL`、`STOCK_PILOT_LLM_API_KEY`、`STOCK_PILOT_LLM_TIMEOUT`、`STOCK_PILOT_TOOL_CLIENT`（`inprocess` 默认 / `mcp` / `mcp-http`）、`STOCK_PILOT_MCP_TRANSPORT`、`STOCK_PILOT_HOST`、`STOCK_PILOT_PORT`、`STOCK_PILOT_INVENTORY_URL`、`STOCK_PILOT_MALL_URL`、`STOCK_PILOT_AGENT_TOKEN`、`STOCK_PILOT_OPERATOR_TOKEN`、`STOCK_PILOT_ADMIN_TOKEN`、`STOCK_PILOT_REQUIRE_READ_AUTH`、`STOCK_PILOT_INVENTORY_PORT`、`STOCK_PILOT_MALL_PORT`、`STOCK_PILOT_BIND_HOST`、`STOCK_PILOT_LOG_FORMAT`（`text` 默认 / `json`）、`STOCK_PILOT_LOG_LEVEL`。

鉴权：任一 Token 配置即启用校验；`POST /tasks` 需 agent，审批/映射/恢复/撤回需 operator，admin 覆盖两者；`decided_by` / `abandoned_by` 取自身份。审批台「管理令牌」输入框把 Token 放进 `Authorization` 头（sessionStorage 保存，输入后自动刷新）；只读接口默认开放，`STOCK_PILOT_REQUIRE_READ_AUTH=true` 时要求任一有效 Token（`/healthz` 除外）。未配置 Token 时是本地无鉴权演示模式。

## 代码架构

代码在 `src/stock_pilot/`，测试在 `tests/`。

**数据层**

- `db.py`：默认单个 SQLite 文件（WAL），三块 module 边界：`inventory_records`（库存）、`listings`/`staging`（商城）、`approvals`（审批），外加 `tasks` 审计记录、`tool_events` 工具时间线与 `outbox_events` 通知事件（仅编排库）。跨模块提交（改价 + 上架 + 消费审批）在同一个 `BEGIN IMMEDIATE` 事务内完成。`INVENTORY_SCHEMA` / `MALL_SCHEMA` / `ORCHESTRATOR_SCHEMA` 可按 split 布局写入三个文件（`initialize_split_stores`）；`listings` 不再有跨库外键，库存预留字段通过增量迁移补齐。
- 金额统一用「分」存 SQLite，边界层用 `Decimal`；`models.to_cents/from_cents` 是唯一转换点。`db.parse_dt` 把不带时区的历史时间戳按 UTC 解释，避免与 `utcnow()` 做差时抛异常。
- `staging` 有部分唯一索引：每个 listing 最多存在一条 `price_staged` 提案。
- 增量迁移在 `initialize_database` 内执行：补 `approvals.decided_by` / `abandoned_at` / `abandon_reason` / `abandoned_by`、`tasks.idempotency_key` 与唯一索引，保证旧 demo 库可直接打开。

**工具层**

- `inventory.py` / `mall.py` / `approval.py`：纯业务函数；业务违规抛 `ToolFailure(ErrorCode)`，由 `tools.py` 转成 `ok=False` 的结构化输出。
- `reservations.py`：预留清理的唯一决策点 `disposition()` 与 `sweep()`。只释放审批已终止 / 缺失或任务已终态的预留；`approved` 且任务活跃的预留即使超过 TTL 也只标记 stale。`sweep()` 通过 `ToolClient` 读预留与释放，审批与任务走本地库，不引入新持久状态。`ReservationSweeper` 是可选进程内定时器（split 布局且 `STOCK_PILOT_RESERVATION_GC_INTERVAL > 0` 时由 lifespan 启动），只重复调用 `sweep()`。
- `notifications.py`：仅通知的 outbox。`enqueue_terminal()` 由 `tasks.update_task()` 在**同一事务**内调用；`OutboxWorker` 只持有数据库路径与 `NotificationSender`，**不持有 `ToolClient`**，因此结构上不可能重投写操作。投递至少一次，发送失败保留 pending 并累计 `attempts`/`last_error`；达到 `max_attempts` 后写 `dead_lettered_at` 转死信，`prune()` 只删除超过保留期的已投递/死信行；`list_events()` / `retry_dead()` 提供运维只读与重投，仍不接触工具。`LogSender` 只写结构化日志；`WebhookSender` 用标准库 HTTP，带超时与可选 HMAC-SHA256 签名，错误信息不包含 URL。
- `intake.py`：可选的自然语言入口。`LLMClient` 协议只返回原始字段，`extract_task()` 用 `ExtractedTask` 严格校验（多余字段拒绝、价格上下界、必填字段不得为空）并生成 `TaskDraft`；`AnthropicLLMClient` 使用官方 SDK 的结构化输出（一次请求、不重试、有超时），缺依赖或未配置 provider 时入口返回 503。该模块不接收 `ToolClient`，草稿不落库。
- `runner.py`：每次调用有硬超时；只读工具可重试，写工具只执行一次；每个工具独立熔断器（连续失败达阈值后打开，冷却后半开）。`ToolFailure` 不计入熔断。每次尝试通过 `event_sink` 上报 `ToolOutcome` 与耗时，并输出一条带 `tool/attempt/outcome/error_code/latency_ms` 的日志；审计写失败不影响业务结果。`circuit_states()` 供指标读取熔断快照。
- `metrics.py`：`GET /metrics` 的 Prometheus collector，每次抓取新建 registry。任务状态、工具事件与耗时从编排库派生，审批积压从 mall 库派生（`mall_db_path`），恢复问题跨库扫描；熔断与客户端可用性由调用方传入。工具事件只统计 `task_id IS NOT NULL` 的编排侧记录，避免 MCP 模式下服务端与客户端双写导致重复计数。预留相关 gauge：`stock_pilot_reservations_open` 直接读库存库，`stock_pilot_reservations_stale` 取自恢复扫描的 `reservation_stale`；`stock_pilot_outbox_pending` 读编排库未投递通知数。
- `logging_config.py`：`STOCK_PILOT_LOG_FORMAT=json` 时每条日志一个 JSON 对象（`ts/level/logger/message` + 白名单 `task_id/tool/attempt/outcome/error_code/latency_ms`），字段与 `tool_events` 对齐；不输出 Token / Authorization 头。`configure_logging()` 在 API 与三个 MCP Server 入口调用。
- `audit.py`：`task_context()` 用 ContextVar 关联任务；`make_event_sink()` 把每次工具尝试写入 `tool_events`（`task_id / tool / attempt / outcome / error_code / latency_ms`），`GET /tasks/{id}/events` 读取；`tool_event_from_row()` 供审计导出复用。
- `audit_export.py`：`GET /audit/export` 的唯一实现。按 UTC 时间窗读取编排库的任务 / 工具事件 / 通知元数据与商城库的审批，按 `created_at` 升序，`limit` 每类集合独立；只读、不持有 `ToolClient`，不返回凭据、通知 payload 或 `last_error`。split 下没有跨库事务，快照是操作员视图而非备份。
- `tools.py`：`StockPilotTools` 门面，把超时/熔断/内部异常统一映射为 `TOOL_TIMEOUT` / `TOOL_UNAVAILABLE` / `CIRCUIT_OPEN` / `INTERNAL_ERROR`。按工具路由存储：库存工具走 `store_inventory`，商城工具走 `store_mall`，审批走 `approval_db`，审计走 `store_orchestrator`。split 模式下实现内部 `reserve_inventory` / `release_inventory` / `list_reservations`，并在 `publish_listing` 关闭单库库存校验；`reserve_inventory` 输出附带 `reserved_at` / `expires_at`（TTL 仅观测）。
- `mcp_server.py`：MCP 2.x 中 `FastMCP` 已改名 `MCPServer`（`mcp.server.mcpserver`），不要再从 `mcp.server.fastmcp` 导入。工具用平铺参数接收，内部立即构造 Pydantic 输入模型，返回 Pydantic 输出模型（含 `outputSchema` / structuredContent）。`register_inventory_tools` / `register_mall_tools` 同时支撑单 stdio Server 与两个拆分 Server；split 模式下额外注册 `register_inventory_saga_tools`（内部预留 / 释放 / 列出工具），默认不暴露。
- `mcp_http.py`：把 `MCPServer.streamable_http_app()` 包装成纯 ASGI 的 Bearer 中间件（不用 `BaseHTTPMiddleware`，避免干扰 MCP 流式响应）；`mcp_inventory.py` / `mcp_mall.py` 是对应的 HTTP 入口，端口默认 8101 / 8102。
- `auth.py`：静态 Token → 角色（agent / operator / admin）。任一 Token 配置即启用校验；admin 覆盖 agent 与 operator。只读 API 保持开放是演示取舍。

**工具客户端边界（`clients.py`）**

- `ToolClient` 协议：编排层依赖四个工具方法、内部 `reserve_inventory` / `release_inventory` / `list_reservations`，以及 `is_available` / `get_approval` / `abandon_approval` / `decide_approval`。`create_tool_client()` 按 `STOCK_PILOT_TOOL_CLIENT` 返回实现，默认 `StockPilotTools`（进程内），`mcp` 返回单 stdio `MCPToolClient`，`mcp-http` 返回 `SplitMCPToolClient`。审批面方法由 `_LocalApprovalPlane` 复用，读写 `settings.approval_db`（split 时即 mall 库）。`initialize_stores()` 建好所有配置的库；stdio 子进程环境注入布局与三个路径。
- `MCPToolClient` 同时支持 stdio（`StdioServerParameters`）与 Streamable HTTP（`url` + `headers`）；`SplitMCPToolClient` 持有两个独立会话，`check_inventory` 路由到库存服务，其余三个工具路由到商城服务。
- `MCPToolClient`：后台线程跑 asyncio 事件循环，用官方 SDK 的 `stdio_client` + `ClientSession` 长期持有会话；子进程命令为 `sys.executable -m stock_pilot.mcp_server`，注入 `STOCK_PILOT_DB`、`PYTHONPATH=src`、`PYTHONUTF8=1`。
- 会话由后台循环监督：每 2 秒 `send_ping` 探测（HTTP transport 自身不报告对端断开），失败后卸载会话并以 0.5s→10s 指数退避重连，成功后重置。重连只恢复传输，绝不重放写操作；断开期间调用快速失败。`close()` 取消监督任务。
- 同步调用经 `asyncio.run_coroutine_threadsafe` 投递，仍复用 `ToolExecutor`，因此客户端侧的超时 / 只读重试 / 写不重试 / 熔断语义与进程内模式一致。传输层失败映射为 `TOOL_UNAVAILABLE`（读会重试直至熔断），业务 `ok=False` 原样透传。
- 审批队列读写与决策留在编排进程本地（审批台是人机管理面，不属于四个 MCP 工具）；`MCPToolClient` 构造时会先 `initialize_database()`，保证任务/审批表存在。
- 服务不可用或中途退出时，任务在第一次失败处落入安全终态（`ABORTED_UNKNOWN_STATE` 等），不会补发写操作；错误码可能是 `TOOL_UNAVAILABLE` 或已打开的 `CIRCUIT_OPEN`。

**编排层**

- `agent.py`：LangGraph 状态机 `check_stock → resolve_listing → stage_price → await_approval → recheck_stock → publish → verify`。
- 两个 interrupt：`mapping_required`（歧义商品，等人工选候选）和 `approval_required`（等审批）。FastAPI 用 `Command(resume=...)` 唤醒同一 `thread_id`；审批状态始终以审批表为准，resume payload 只负责唤醒。
- 写超时后只做一次只读 `get_listing` 对账：改价超时看 `pending` 是否已落库并采纳 `approval_id`；上架超时看价格/状态是否已是目标值。对账失败则 `ABORTED_UNKNOWN_STATE`，绝不重发写操作。
- 最终核对复用 `get_listing`，结果写入 `tasks.verification_json`；核对通过才进入 `VERIFIED`。
- 歧义 SKU 在人工选定后仍会返回 ambiguous，因此候选 `ListingCandidate` 也带 `pending`，核对/对账通过已确认的 `listing_id` 在候选中定位，不靠猜。
- `start_task` / `resume_task` 用 `audit.task_context()` 包裹 `graph.invoke`，为工具事件提供 `task_id`。
- `decide_and_resume` 幂等：相同决策重复提交返回当前任务；「审批已决策但图未恢复」时直接 resume，这正是重启恢复路径。相反决策返回 `APPROVAL_ALREADY_DECIDED`。记录首次决策时若 `decided_by == task.submitted_by`（仅 admin break-glass 可能）打 warning，自审不会静默。
- `recheck_stock`：审批通过后、`publish_listing` 前再调一次只读 `check_inventory`。库存 ≤ 0 → `abandon_approval`（本地事务：审批置 `abandoned` + staging 置 `discarded`）→ 任务 `ROLLED_BACK` / `INSUFFICIENT_STOCK`。复核工具失败同样不发布并撤回提案，任务 `ABORTED_UNKNOWN_STATE`；绝不留下已终止任务 + 活跃 staging。
- split 模式下复核通过后调用内部 `reserve_inventory` 预留库存；预留失败（`INSUFFICIENT_STOCK` / `STOCK_RESERVED`）同样撤回提案并 `ROLLED_BACK`，传输失败则 `ABORTED_UNKNOWN_STATE`。`publish` 用 `try/finally` 尽力 `release_inventory`，补偿失败只记日志，不改任务结论。
- 单库模式下 `publish` 的提交事务内还有最后一道库存校验（见 `mall.publish_listing`）：复核读数可能过期，事务内库存 ≤ 0 时整体回滚；`agent` 收到 `INSUFFICIENT_STOCK` 后同样 `_abandon_proposal` 并落 `ROLLED_BACK`。split 模式由预留代替这一检查（`check_stock=False`）。
- `verify_published_task()`：已提交但未写回核对结果的恢复路径，只读 `get_listing` + 写 `verification_json`，绝不再次调用 `publish_listing`；调用时同样用 `audit.task_context()` 关联工具事件。

**重启与恢复（`recovery.py`）**

- `recovery.scan(db_path, mall_db_path=..., inventory_db_path=..., reservation_ttl_seconds=...)` 只读扫描 `received` / `awaiting_approval` / `published` 任务，在应用层把编排库的任务与 mall 库的审批、staging、listing 关联，分类为 `approved_not_published`、`rejected_not_closed`、`abandoned_not_closed`、`published_verify_pending`、`approval_missing`、`approved_without_staging`、`interrupted_before_approval`；另扫描写超时后遗留的 `price_staged` 行，报告 `staging_orphaned`（只标注，不自动写）。再扫描库存库中存活预留，按 `reservations.disposition()` 报告 `reservation_orphaned`（可清理）或 `reservation_stale`（需人工）。默认两个路径均为同一文件，保持单库行为。
- 启动时 lifespan 打一条 warning 汇总；`GET /recovery` 返回明细，审批台显示「待恢复任务」面板。
- `POST /tasks/{id}/recover` 处理两类任务：`awaiting_approval` 且审批已决策时用 `Command(resume=...)` 继续图（审批通过必须有活跃 `price_staged`，否则拒绝恢复）；`published` 时调用 `verify_published_task()` 只读补做核对。其余状态返回 409。恢复不会新增写操作，只会执行已审批的 `publish_listing`、回写 `rolled_back` 或补核对。
- 人工清理：`POST /approvals/{id}/abandon` 只接受 `approved`（`abandoned` 幂等）提案，先通过 `get_listing` 做只读对账；商城已是目标价且 `published` 时拒绝撤回，防止把已提交事实误判为遗留提案。对账失败也拒绝。

**服务层**

- `api.py`：操作接口 `POST /tasks`（支持 `Idempotency-Key`，冲突返回 `IDEMPOTENCY_CONFLICT`；需 agent）、`POST /tasks/intake`（需 agent，返回草稿、零写入；未配置模型时 503）、`POST /approvals/{approval_id}/decision`（可带 `decided_by`；需 operator）、`POST /approvals/{approval_id}/abandon`（需 operator）、`POST /tasks/{task_id}/mapping`、`POST /tasks/{task_id}/recover`（需 operator）、`POST /reservations/gc`（需 operator，只释放审批不可再发布的预留）、`POST /outbox/{event_id}/retry`（需 operator，只重投死信通知）；只读接口 `GET /capabilities`（模型开关 / 模型名 / 布局 / 鉴权开关，不含 Key）、`GET /tasks`、`GET /tasks/{id}`、`GET /tasks/{id}/events`、`GET /recovery`、`GET /audit/export`（需 operator，只读审计快照，支持 since/until/limit）、`GET /reservations`、`GET /outbox`、`GET /healthz`（任一存储不可读或工具客户端不可用时 503 + `degraded`）、`GET /approvals`、`GET /approvals/{id}`、`GET /approvals/queue`（联表返回商品与任务上下文）；`GET /metrics` 输出 Prometheus 文本（不鉴权，供抓取）。lifespan 启动并停止通知 outbox worker（无 `ToolClient`）。配置 Token 后 `decided_by` / `abandoned_by` 以身份为准，忽略请求体；未配置 Token 为本地演示模式。
- `tasks.py`：任务记录 CRUD、`find_task_by_idempotency_key()` 与 JSON 字段序列化；`submitted_by` 记录提交人（鉴权开启时来自 agent 身份）。`update_task()` 在同一事务内调用 `notifications.enqueue_terminal()`，保证终态与通知事件原子落库。
- `demo.py`：一条命令启动演示（`python -m stock_pilot.demo`，也可用 `stock-pilot-demo`）。预检端口，打印审批台地址、各存储路径与三个演示 SKU；`--split` 以子进程拉起库存/商城 HTTP MCP Server、切到 `mcp-http` 并启用三库布局，退出时回收子进程；`--fresh` 只删除配置的演示库（含三个库的 `-wal`/`-shm`）。
- `static/`：零构建的原生 HTML/CSS/JS 审批台，`GET /` 返回页面。页面每 5 秒自动刷新，审批备注草稿、映射选择、审批人姓名与自然语言草稿在重绘时保留；拒绝必须填写原因；存在异常态任务时显示「待恢复任务」面板，`published_verify_pending` 可补核对，`staging_orphaned` 提供「撤回遗留提案」按钮，`reservation_orphaned` / `reservation_stale` 提供「释放遗留预留」按钮（调用 `POST /reservations/gc`）。「自然语言草稿」面板从 `GET /capabilities` 判断是否显示输入框；关闭时显示说明，开启时草稿只读展示、确认后仅向 `POST /tasks` 发送四个任务字段，不提供审批/发布捷径。顶栏「导出审计」按钮以当前 Token 调用 `GET /audit/export` 并下载 JSON。静态资源通过 `pyproject.toml` 的 `package-data` 打进 wheel。

**演示数据（`db.py:_seed_demo_data`）**

| sku_id | spec | 场景 |
|---|---|---|
| SKU-TSHIRT-001 | 白色-M | Happy path（LST-001，79 元，库存 20） |
| SKU-AMBIG-002 | 白色-M / 白色-L | 映射歧义（LST-002 / LST-003 同名） |
| SKU-HOODIE-003 | 黑色-L | 库存为 0 |

## 测试对应关系

- `tests/test_tools_acceptance.py`：4 条验收用例的工具层断言 + MCP 工具注册。
- `tests/test_agent_acceptance.py`：4 条验收用例的图编排断言，另含映射恢复、写超时对账、拒绝/超时后零后续写、复核读数过期时提交事务内兜底。
- `tests/test_runner.py`：超时重试、熔断、写不重试、业务失败不计入熔断。
- `tests/test_api.py`：FastAPI 操作接口、错误码与 `/healthz` 就绪/降级。
- `tests/test_ui.py`：审批台静态资源、页面入口、审批队列联表数据与状态过滤、`GET /capabilities` 开关/布局/鉴权、自然语言草稿确认前零写入。
- `tests/test_audit_export.py`：审计快照内容（任务 / 审批 / 工具事件 / 通知元数据）、operator/agent/无 Token 鉴权、时间窗与 limit、倒置窗口 400、凭据不出现、split 跨库读取、导出前后表行数不变。
- `tests/test_mcp_client.py`：真实 MCP stdio 客户端——四个工具契约、完整工具链、图与 FastAPI 跑在 MCP 模式、服务不可用 / 中途退出时安全终止且零后续写。
- `tests/test_mcp_http.py`：两个 Streamable HTTP MCP Server（进程内 uvicorn）——工具路由、完整图链路、Bearer 缺失/错误时 401、未授权客户端安全终止。
- `tests/test_auth.py`：角色鉴权——缺失/错误 Token 401、跨角色 403、admin 覆盖与自审告警、身份覆盖请求体、撤回留痕 `abandoned_by`、只读鉴权开关。
- `tests/test_concurrency.py`：并发 staging 单赢家、相反审批只生效一个、并发发布只提交一次、相同 `Idempotency-Key` 并发提交只建一个任务、并发初始化只 seed 一次。
- `tests/test_recovery.py`：进程重启后续审批/映射、恢复扫描与恢复接口（含 `published` 只读补核对）、任务/审批幂等、操作者留痕、工具事件时间线、发布前库存复核与系统撤回、遗留提案人工撤回与拒绝条件。
- `tests/test_observability.py`：`/metrics` 数值（任务、工具事件、耗时桶、熔断、降级）、JSON 日志字段与 `task_id`、字段白名单（Token 不入日志）、`/healthz` 降级。
- `tests/test_reconnect.py`：HTTP MCP Server 停机后任务安全中止、同端口重启后客户端自动重连、新任务完成，且审计中只有一次成功 `publish_listing`（写不重放）。
- `tests/test_split_stores.py`：三库各自只拥有本模块表；库存预留的独占/幂等/释放；split happy path 提交并释放预留；商城已提交但响应丢失时只读采纳一次（`publish_calls == 1`）；提交前失败则 `ABORTED_UNKNOWN_STATE` 并释放预留；split API 跨库读审批与指标；HTTP 两服务路由内部预留工具。
- `tests/test_reservations.py`：`disposition()` 保守规则；预留输出带 TTL；清理释放 consumed / abandoned / 审批缺失 / 任务终态的预留且幂等；活跃审批的预留即使超过 TTL 也只标记 stale，清理后仍能正常发布；恢复扫描与指标；`POST /reservations/gc` 的 operator 鉴权；进程内定时器释放孤立预留、不抢活跃预留，且仅在显式配置时由 lifespan 启动。
- `tests/test_outbox.py`：终态更新与通知在同一事务（入队失败回滚任务）、非终态不入队、重复终态去重、投递一次、发送失败保留待重试、达到重试上限转死信、保留期清理只删已投递/死信行、`stock_pilot_outbox_pending` / `stock_pilot_outbox_dead`、API lifespan worker 端到端投递，以及 worker 零工具写入。
- `tests/test_webhook.py`：webhook 请求体、HMAC 签名、非 2xx 失败且错误不含 URL；`GET /outbox` 状态过滤、死信重投的 operator 鉴权与幂等、已投递/不存在事件的拒绝，以及重投不触碰商城与工具审计。
- `tests/test_intake.py`：草稿返回且零写入、缺少理由的警告、缺字段/越界/多余字段被拒绝、上游失败映射 503/502、默认关闭、agent 角色鉴权、未知 SKU 在提交阶段安全失败、价格按分归一化、Anthropic 适配器的结构化输出与拒答处理、密钥不出现在 `repr(Settings)` 中。
- `tests/helpers.py`：进程内 uvicorn ServerHandle、空闲端口、`wait_until`，供 HTTP MCP、重连与 split 测试共用。
- `tests/test_demo.py`：`stock_pilot.demo` 参数、端口探测，以及真实子进程启动后 `/healthz` 与页面可用。

## 文档

- `docs/interview-notes.md`：3 分钟项目叙述、11 个高频追问、失败矩阵、已知边界。
- `docs/demo.md`：面试演示脚本（含失败路径、恢复、拆分、鉴权与预留清理）。
- `docs/observability.md`：指标清单、Prometheus 告警规则示例、JSON 日志字段、断线重连语义。
- `docs/screenshots/console.png`：审批台截图，README 首屏引用。
- `docs/adr/`：0001 两阶段审批提交、0002 失败即停止写、0003 拆分与静态 Token、0004 恢复与人工清理、0005 三库分离与发布 saga、0006 预留 TTL 与清理、0007 通知 outbox、0008 自然语言入口只做参数抽取、0009 审计导出只读投影。

## 实现状态

工具层、MCP Server、真实 MCP stdio 客户端、LangGraph 状态机、FastAPI、审批台 UI、重启恢复（含已提交任务补核对）、任务/审批幂等、工具调用审计、发布前与提交事务内库存复核、遗留提案人工清理、健康检查均已落地；B1 已完成库存/商城两个 Streamable HTTP MCP Server 与静态 Token 角色鉴权；任务、审批、撤回分别留痕提交人 / 审批人 / 撤回人；E1 并发不变量与 E2 观测性（Prometheus 指标、JSON 日志、断线自动重连）已完成。E4 提供一条命令的演示启动器与审批台截图。E3 完成三库分离与发布 saga：内部 `reserve_inventory` / `release_inventory`、提交后响应丢失的只读采纳、跨库恢复扫描与指标、`demo --split`。E5 完成预留 TTL 观测与保守清理：`list_reservations`、`GET/POST /reservations`、恢复扫描分类、指标与审批台按钮。P2 完成仅通知 outbox：终态同事务入队、至少一次投递、`stock_pilot_outbox_pending` / `stock_pilot_outbox_dead`、死信、保留期清理、lifespan worker，结构上禁止重投写；后续补上 webhook 发送器与死信重投接口。E5 补上可选的预留清理进程内定时调度。P5 完成可选的自然语言入口：模型只做参数抽取、严格校验、草稿不落库不调工具，默认关闭且测试不联网；审批台补上只读草稿面板与 `GET /capabilities`，确认后才提交任务。P6 完成只读审计导出：`GET /audit/export`、时间窗与 limit、跨库读取、通知元数据脱敏，审批台提供下载按钮。`pytest -q` 当前 163 个用例通过（含并发不变量；并发用例连续 20 次全绿），`ruff check` 通过。CI 在 `.github/workflows/ci.yml`（Ubuntu + Windows × Python 3.10/3.12）。尚未实现：OIDC / 真实登录、指标接入告警平台与日志聚合、Postgres。
