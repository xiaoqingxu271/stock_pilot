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

## 已确认的设计决策

1. **最终核对**：复用 `get_listing` 再读一次，不新增工具；核对结果写入任务记录。
2. **审批粒度**：一次审批覆盖「调价 + 上架」组合提案；审批人通过 FastAPI 操作（提交任务 / 审批决策 approve|reject / 映射确认，共三个接口）。
3. **回滚方式**：两阶段提交。`update_price` 只写 staging；审批通过后由 `publish_listing` 在同一事务内原子提交价格与上架。审批拒绝时丢弃 staging，商城侧从未发生写入。
4. **写超时**：允许一次只读对账（复用 `get_listing`，依据其 `pending` 字段判断 staging/审批/提交状态），禁止任何后续写操作。
5. **`get_listing` 契约**：三态 `resolved / ambiguous / not_found`，仍是同一个只读工具。
6. **目标价来源**：调用方提交任务时给定 `sku_id`、`spec`、目标价、理由；Agent 只负责安全执行，不自主定价。
7. **MCP 拓扑**：第一版一个 MCP Server，内部按 module 隔离库存 / 商城 / 审批三块数据；B1 增加拆分部署——库存与商城各一个 Streamable HTTP MCP Server，编排层按工具路由，审批面仍留在编排进程（人机管理面不是模型工具），两个工具 Server 共用演示数据库。
8. **发布前库存复核**：审批通过后、`publish_listing` 前用现有 `check_inventory` 再读一次。库存 ≤ 0 → 不提交商城，审批置 `abandoned` 并在同一事务丢弃 staging，任务 `ROLLED_BACK` / `INSUFFICIENT_STOCK`。复核工具失败同样撤回提案，避免「任务已终止但 staging 仍活跃」。
9. **重启恢复与幂等**：审批状态以数据库为准，进程重启后可从 checkpoint 继续；`GET /recovery` 只读标注异常态；`POST /tasks/{id}/recover` 对 `awaiting_approval` 任务继续图，对 `published` 任务只读补核对；`Idempotency-Key` 与重复审批返回原结果。
10. **提交时兜底与人工清理**：`publish_listing` 在提交事务内再次校验库存（复核读数可能已过期），不足则整个事务回滚、零写入；写超时遗留的 `price_staged` 只由人工经 `POST /approvals/{id}/abandon` 清理，接口必须先只读对账，商城已提交则拒绝撤回。`GET /healthz` 暴露数据库与工具客户端就绪状态。
11. **鉴权与拆分（B1）**：静态 Bearer Token 映射 agent / operator / admin 三角色；任一 Token 配置即强制校验，身份写入 `decided_by` / `abandoned_by`。库存与商城各一个 Streamable HTTP MCP Server，只接受 agent Token；审批面仍留在编排进程。只读接口保持开放仅用于本地演示，生产必须置于统一鉴权网关之后。

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

# 启动 FastAPI（默认 127.0.0.1:8000，工具进程内调用）
./.venv/Scripts/python.exe -m stock_pilot.api
# 浏览器打开 http://127.0.0.1:8000/ 即审批台

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

配置通过环境变量覆盖：`STOCK_PILOT_DB`、`STOCK_PILOT_TOOL_TIMEOUT`、`STOCK_PILOT_READ_RETRIES`、`STOCK_PILOT_CIRCUIT_THRESHOLD`、`STOCK_PILOT_CIRCUIT_COOLDOWN`、`STOCK_PILOT_TOOL_CLIENT`（`inprocess` 默认 / `mcp` / `mcp-http`）、`STOCK_PILOT_MCP_TRANSPORT`、`STOCK_PILOT_HOST`、`STOCK_PILOT_PORT`、`STOCK_PILOT_INVENTORY_URL`、`STOCK_PILOT_MALL_URL`、`STOCK_PILOT_AGENT_TOKEN`、`STOCK_PILOT_OPERATOR_TOKEN`、`STOCK_PILOT_ADMIN_TOKEN`、`STOCK_PILOT_INVENTORY_PORT`、`STOCK_PILOT_MALL_PORT`、`STOCK_PILOT_BIND_HOST`。

鉴权：任一 Token 配置即启用校验；`POST /tasks` 需 agent，审批/映射/恢复/撤回需 operator，admin 覆盖两者；`decided_by` / `abandoned_by` 取自身份。审批台「管理令牌」输入框把 Token 放进 `Authorization` 头（sessionStorage 保存）；只读接口保持开放（演示用），生产置于网关后。未配置 Token 时是本地无鉴权演示模式。

## 代码架构

代码在 `src/stock_pilot/`，测试在 `tests/`。

**数据层**

- `db.py`：单个 SQLite 文件（WAL），三块 module 边界：`inventory_records`（库存）、`listings`/`staging`（商城）、`approvals`（审批），外加 `tasks` 审计记录和 `tool_events` 工具时间线。跨模块提交（改价 + 上架 + 消费审批）在同一个 `BEGIN IMMEDIATE` 事务内完成。
- 金额统一用「分」存 SQLite，边界层用 `Decimal`；`models.to_cents/from_cents` 是唯一转换点。
- `staging` 有部分唯一索引：每个 listing 最多存在一条 `price_staged` 提案。
- 增量迁移在 `initialize_database` 内执行：补 `approvals.decided_by` / `abandoned_at` / `abandon_reason` / `abandoned_by`、`tasks.idempotency_key` 与唯一索引，保证旧 demo 库可直接打开。

**工具层**

- `inventory.py` / `mall.py` / `approval.py`：纯业务函数；业务违规抛 `ToolFailure(ErrorCode)`，由 `tools.py` 转成 `ok=False` 的结构化输出。
- `runner.py`：每次调用有硬超时；只读工具可重试，写工具只执行一次；每个工具独立熔断器（连续失败达阈值后打开，冷却后半开）。`ToolFailure` 不计入熔断。每次尝试通过 `event_sink` 上报 `ToolOutcome` 与耗时；审计写失败不影响业务结果。
- `audit.py`：`task_context()` 用 ContextVar 关联任务；`make_event_sink()` 把每次工具尝试写入 `tool_events`（`task_id / tool / attempt / outcome / error_code / latency_ms`），`GET /tasks/{id}/events` 读取。
- `tools.py`：`StockPilotTools` 门面，把超时/熔断/内部异常统一映射为 `TOOL_TIMEOUT` / `TOOL_UNAVAILABLE` / `CIRCUIT_OPEN` / `INTERNAL_ERROR`。
- `mcp_server.py`：MCP 2.x 中 `FastMCP` 已改名 `MCPServer`（`mcp.server.mcpserver`），不要再从 `mcp.server.fastmcp` 导入。工具用平铺参数接收，内部立即构造 Pydantic 输入模型，返回 Pydantic 输出模型（含 `outputSchema` / structuredContent）。`register_inventory_tools` / `register_mall_tools` 同时支撑单 stdio Server 与两个拆分 Server。
- `mcp_http.py`：把 `MCPServer.streamable_http_app()` 包装成纯 ASGI 的 Bearer 中间件（不用 `BaseHTTPMiddleware`，避免干扰 MCP 流式响应）；`mcp_inventory.py` / `mcp_mall.py` 是对应的 HTTP 入口，端口默认 8101 / 8102。
- `auth.py`：静态 Token → 角色（agent / operator / admin）。任一 Token 配置即启用校验；admin 覆盖 agent 与 operator。只读 API 保持开放是演示取舍。

**工具客户端边界（`clients.py`）**

- `ToolClient` 协议：编排层只依赖四个工具方法 + `is_available` / `get_approval` / `abandon_approval` / `decide_approval`。`create_tool_client()` 按 `STOCK_PILOT_TOOL_CLIENT` 返回实现，默认 `StockPilotTools`（进程内），`mcp` 返回单 stdio `MCPToolClient`，`mcp-http` 返回 `SplitMCPToolClient`。审批面方法由 `_LocalApprovalPlane` 复用。
- `MCPToolClient` 同时支持 stdio（`StdioServerParameters`）与 Streamable HTTP（`url` + `headers`）；`SplitMCPToolClient` 持有两个独立会话，`check_inventory` 路由到库存服务，其余三个工具路由到商城服务。
- `MCPToolClient`：后台线程跑 asyncio 事件循环，用官方 SDK 的 `stdio_client` + `ClientSession` 长期持有会话；子进程命令为 `sys.executable -m stock_pilot.mcp_server`，注入 `STOCK_PILOT_DB`、`PYTHONPATH=src`、`PYTHONUTF8=1`。
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
- `decide_and_resume` 幂等：相同决策重复提交返回当前任务；「审批已决策但图未恢复」时直接 resume，这正是重启恢复路径。相反决策返回 `APPROVAL_ALREADY_DECIDED`。
- `recheck_stock`：审批通过后、`publish_listing` 前再调一次只读 `check_inventory`。库存 ≤ 0 → `abandon_approval`（本地事务：审批置 `abandoned` + staging 置 `discarded`）→ 任务 `ROLLED_BACK` / `INSUFFICIENT_STOCK`。复核工具失败同样不发布并撤回提案，任务 `ABORTED_UNKNOWN_STATE`；绝不留下已终止任务 + 活跃 staging。
- `publish` 的提交事务内还有最后一道库存校验（见 `mall.publish_listing`）：复核读数可能过期，事务内库存 ≤ 0 时整体回滚；`agent` 收到 `INSUFFICIENT_STOCK` 后同样 `_abandon_proposal` 并落 `ROLLED_BACK`。
- `verify_published_task()`：已提交但未写回核对结果的恢复路径，只读 `get_listing` + 写 `verification_json`，绝不再次调用 `publish_listing`；调用时同样用 `audit.task_context()` 关联工具事件。

**重启与恢复（`recovery.py`）**

- `recovery.scan()` 只读扫描 `received` / `awaiting_approval` / `published` 任务，联表审批、staging、listing，分类为 `approved_not_published`、`rejected_not_closed`、`abandoned_not_closed`、`published_verify_pending`、`approval_missing`、`approved_without_staging`、`interrupted_before_approval`；另扫描写超时后遗留的 `price_staged` 行，报告 `staging_orphaned`（只标注，不自动写）。
- 启动时 lifespan 打一条 warning 汇总；`GET /recovery` 返回明细，审批台显示「待恢复任务」面板。
- `POST /tasks/{id}/recover` 处理两类任务：`awaiting_approval` 且审批已决策时用 `Command(resume=...)` 继续图（审批通过必须有活跃 `price_staged`，否则拒绝恢复）；`published` 时调用 `verify_published_task()` 只读补做核对。其余状态返回 409。恢复不会新增写操作，只会执行已审批的 `publish_listing`、回写 `rolled_back` 或补核对。
- 人工清理：`POST /approvals/{id}/abandon` 只接受 `approved`（`abandoned` 幂等）提案，先通过 `get_listing` 做只读对账；商城已是目标价且 `published` 时拒绝撤回，防止把已提交事实误判为遗留提案。对账失败也拒绝。

**服务层**

- `api.py`：操作接口 `POST /tasks`（支持 `Idempotency-Key`，冲突返回 `IDEMPOTENCY_CONFLICT`；需 agent）、`POST /approvals/{approval_id}/decision`（可带 `decided_by`；需 operator）、`POST /approvals/{approval_id}/abandon`（需 operator）、`POST /tasks/{task_id}/mapping`、`POST /tasks/{task_id}/recover`（需 operator）；只读接口 `GET /tasks`、`GET /tasks/{id}`、`GET /tasks/{id}/events`、`GET /recovery`、`GET /healthz`（数据库/工具客户端不可用时 503 + `degraded`）、`GET /approvals`、`GET /approvals/{id}`、`GET /approvals/queue`（联表返回商品与任务上下文）。配置 Token 后 `decided_by` / `abandoned_by` 以身份为准，忽略请求体；未配置 Token 为本地演示模式。
- `tasks.py`：任务记录 CRUD、`find_task_by_idempotency_key()` 与 JSON 字段序列化。
- `static/`：零构建的原生 HTML/CSS/JS 审批台，`GET /` 返回页面。页面每 5 秒自动刷新，审批备注草稿、映射选择与审批人姓名在重绘时保留；拒绝必须填写原因；存在异常态任务时显示「待恢复任务」面板，`published_verify_pending` 可补核对，`staging_orphaned` 提供「撤回遗留提案」按钮。静态资源通过 `pyproject.toml` 的 `package-data` 打进 wheel。

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
- `tests/test_ui.py`：审批台静态资源、页面入口、审批队列联表数据与状态过滤。
- `tests/test_mcp_client.py`：真实 MCP stdio 客户端——四个工具契约、完整工具链、图与 FastAPI 跑在 MCP 模式、服务不可用 / 中途退出时安全终止且零后续写。
- `tests/test_mcp_http.py`：两个 Streamable HTTP MCP Server（进程内 uvicorn）——工具路由、完整图链路、Bearer 缺失/错误时 401、未授权客户端安全终止。
- `tests/test_auth.py`：角色鉴权——缺失/错误 Token 401、跨角色 403、admin 覆盖、身份覆盖请求体、撤回留痕 `abandoned_by`。
- `tests/test_recovery.py`：进程重启后续审批/映射、恢复扫描与恢复接口（含 `published` 只读补核对）、任务/审批幂等、操作者留痕、工具事件时间线、发布前库存复核与系统撤回、遗留提案人工撤回与拒绝条件。

## 实现状态

工具层、MCP Server、真实 MCP stdio 客户端、LangGraph 状态机、FastAPI、审批台 UI、重启恢复（含已提交任务补核对）、任务/审批幂等、工具调用审计、发布前与提交事务内库存复核、遗留提案人工清理、健康检查均已落地；B1 已完成库存/商城两个 Streamable HTTP MCP Server 与静态 Token 角色鉴权。`pytest -q` 当前 76 个用例通过，`ruff check` 通过。CI 在 `.github/workflows/ci.yml`（Ubuntu + Windows）。尚未实现：各服务独立数据库与 saga 补偿、OIDC / 真实登录、只读接口鉴权、Postgres。
