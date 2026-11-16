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
7. **MCP 拓扑**：第一版一个 MCP Server，内部按 module 隔离库存 / 商城 / 审批三块数据；生产环境拆分方案留作面试说明。

## 验收用例（至少 4 条，每次改动后回归）

1. **Happy path**：调用方给定目标价 59 元、库存充足 → 审批通过 → 价格变为 59 元 → 状态上架 → 最终核对一致。
2. **库存不足**：库存 ≤ 0 → 任务终止，写操作零执行。
3. **映射歧义**：get_listing 返回多个同名商品 → 任务挂起，输出候选列表，不执行写操作。
4. **审批拒绝**：update_price 审批被拒 → 任务回滚，无中间脏状态。

## 代码规范

- 所有 MCP 工具使用 Pydantic 模型定义输入/输出 schema。
- 错误码使用枚举，禁止裸字符串。
- 每个工具调用必须有超时设置。
- 提交前运行验收用例（至少覆盖上述 4 条）。

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

# 单条用例
./.venv/Scripts/python.exe -m pytest tests/test_agent_acceptance.py::test_happy_path_verified -q

# 分层运行
./.venv/Scripts/python.exe -m pytest tests/test_tools_acceptance.py -q
./.venv/Scripts/python.exe -m pytest tests/test_agent_acceptance.py -q
./.venv/Scripts/python.exe -m pytest tests/test_api.py -q

# 启动 FastAPI（默认 127.0.0.1:8000）
./.venv/Scripts/python.exe -m stock_pilot.api
# 浏览器打开 http://127.0.0.1:8000/ 即审批台

# 启动 MCP Server（stdio）
./.venv/Scripts/python.exe -m stock_pilot.mcp_server
```

配置通过环境变量覆盖：`STOCK_PILOT_DB`、`STOCK_PILOT_TOOL_TIMEOUT`、`STOCK_PILOT_READ_RETRIES`、`STOCK_PILOT_CIRCUIT_THRESHOLD`、`STOCK_PILOT_CIRCUIT_COOLDOWN`、`STOCK_PILOT_MCP_TRANSPORT`、`STOCK_PILOT_HOST`、`STOCK_PILOT_PORT`。

## 代码架构

代码在 `src/stock_pilot/`，测试在 `tests/`。

**数据层**

- `db.py`：单个 SQLite 文件（WAL），三块 module 边界：`inventory_records`（库存）、`listings`/`staging`（商城）、`approvals`（审批），外加 `tasks` 审计记录。跨模块提交（改价 + 上架 + 消费审批）在同一个 `BEGIN IMMEDIATE` 事务内完成。
- 金额统一用「分」存 SQLite，边界层用 `Decimal`；`models.to_cents/from_cents` 是唯一转换点。
- `staging` 有部分唯一索引：每个 listing 最多存在一条 `price_staged` 提案。

**工具层**

- `inventory.py` / `mall.py` / `approval.py`：纯业务函数；业务违规抛 `ToolFailure(ErrorCode)`，由 `tools.py` 转成 `ok=False` 的结构化输出。
- `runner.py`：每次调用有硬超时；只读工具可重试，写工具只执行一次；每个工具独立熔断器（连续失败达阈值后打开，冷却后半开）。`ToolFailure` 不计入熔断。
- `tools.py`：`StockPilotTools` 门面，把超时/熔断/内部异常统一映射为 `TOOL_TIMEOUT` / `CIRCUIT_OPEN` / `INTERNAL_ERROR`。
- `mcp_server.py`：MCP 2.x 中 `FastMCP` 已改名 `MCPServer`（`mcp.server.mcpserver`），不要再从 `mcp.server.fastmcp` 导入。工具用平铺参数接收，内部立即构造 Pydantic 输入模型，返回 Pydantic 输出模型（含 `outputSchema` / structuredContent）。

**编排层**

- `agent.py`：LangGraph 状态机 `check_stock → resolve_listing → stage_price → await_approval → publish → verify`。
- 两个 interrupt：`mapping_required`（歧义商品，等人工选候选）和 `approval_required`（等审批）。FastAPI 用 `Command(resume=...)` 唤醒同一 `thread_id`；审批状态始终以审批表为准，resume payload 只负责唤醒。
- 写超时后只做一次只读 `get_listing` 对账：改价超时看 `pending` 是否已落库并采纳 `approval_id`；上架超时看价格/状态是否已是目标值。对账失败则 `ABORTED_UNKNOWN_STATE`，绝不重发写操作。
- 最终核对复用 `get_listing`，结果写入 `tasks.verification_json`；核对通过才进入 `VERIFIED`。
- 歧义 SKU 在人工选定后仍会返回 ambiguous，因此候选 `ListingCandidate` 也带 `pending`，核对/对账通过已确认的 `listing_id` 在候选中定位，不靠猜。

**服务层**

- `api.py`：三个操作接口 `POST /tasks`、`POST /approvals/{approval_id}/decision`、`POST /tasks/{task_id}/mapping`；另有只读的 `GET /tasks`、`GET /tasks/{id}`、`GET /approvals`、`GET /approvals/{id}`、`GET /approvals/queue`（联表返回商品与任务上下文的待审批项）。
- `tasks.py`：任务记录 CRUD 与 JSON 字段序列化。
- `static/`：零构建的原生 HTML/CSS/JS 审批台，`GET /` 返回页面。页面每 5 秒自动刷新，审批备注草稿与映射选择在重绘时保留；拒绝必须填写原因。静态资源通过 `pyproject.toml` 的 `package-data` 打进 wheel。

**演示数据（`db.py:_seed_demo_data`）**

| sku_id | spec | 场景 |
|---|---|---|
| SKU-TSHIRT-001 | 白色-M | Happy path（LST-001，79 元，库存 20） |
| SKU-AMBIG-002 | 白色-M / 白色-L | 映射歧义（LST-002 / LST-003 同名） |
| SKU-HOODIE-003 | 黑色-L | 库存为 0 |

## 测试对应关系

- `tests/test_tools_acceptance.py`：4 条验收用例的工具层断言 + MCP 工具注册。
- `tests/test_agent_acceptance.py`：4 条验收用例的图编排断言，另含映射恢复、写超时对账、拒绝/超时后零后续写。
- `tests/test_runner.py`：超时重试、熔断、写不重试、业务失败不计入熔断。
- `tests/test_api.py`：三个 FastAPI 操作接口及错误码。
- `tests/test_ui.py`：审批台静态资源、页面入口、审批队列联表数据与状态过滤。

## 实现状态

工具层、MCP Server、LangGraph 状态机、FastAPI、审批台 UI、验收回归均已落地；`pytest -q` 当前 35 个用例通过。生产化拆分（库存/商城/审批各自独立 MCP Server、鉴权、审计日志、真实登录与操作者身份）尚未实现。
