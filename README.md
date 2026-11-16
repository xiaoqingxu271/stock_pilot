# StockPilot

基于 MCP 的库存驱动电商调价上架 Agent。调用方给定 `sku_id`、规格、目标价和理由，Agent 负责在受约束的链路中安全执行：

```
check_inventory → get_listing → update_price（审批）→ publish_listing（审批）→ get_listing 核对
```

这不是「让模型自己定价」的 Demo，而是一个把**审批门禁、两阶段提交、超时熔断、映射歧义、重启恢复**都写进代码和测试的受约束执行器。

## 安全保证

| 规则 | 实现 |
|---|---|
| 库存 ≤ 0 | 在 `check_stock` 终止，审批、staging、商城写入均为零 |
| 商品映射歧义 | `get_listing` 返回候选列表，LangGraph `interrupt` 挂起，必须人工选定，禁止猜测 |
| 写操作必须审批 | `update_price` 只写 staging + 审批记录；商城公开价格在审批通过前不变 |
| 审批拒绝零残留 | 拒绝时丢弃 staging；商城侧从未发生写入 |
| 写超时 | 只做一次只读 `get_listing` 对账；对账失败进入 `ABORTED_UNKNOWN_STATE`，绝不重发写操作 |
| 进程中断 | 重启后 `GET /recovery` 显式列出异常态；已审批提案可安全恢复，未审批任务沿用原流程 |
| 重复请求 | `Idempotency-Key` 保证 `POST /tasks` 不重复建任务；重复审批返回当前结果 |

## 架构

```
                    ┌──────────────────────────────┐
                    │  原生 HTML/CSS/JS 审批台      │
                    │  待审批 / 映射 / 恢复 / 流水   │
                    └──────────────┬───────────────┘
                                   │ HTTP
                    ┌──────────────▼───────────────┐
                    │ FastAPI                       │
                    │ 提交 / 审批 / 映射 / 恢复      │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │ LangGraph 状态机              │
                    │ check → map → stage → gate    │
                    │      → publish → verify       │
                    └──────────────┬───────────────┘
                                   │ ToolClient
                    ┌──────────────▼───────────────┐
                    │ MCPToolClient (stdio)         │
                    │ 超时 / 读重试 / 写一次 / 熔断 │
                    └──────────────┬───────────────┘
                                   │ MCP
                    ┌──────────────▼───────────────┐
                    │ MCP Server（4 个工具）         │
                    └──────────────┬───────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │ SQLite（WAL）                 │
                    │ inventory / listings /        │
                    │ staging / approvals / tasks   │
                    └──────────────────────────────┘
```

- `STOCK_PILOT_TOOL_CLIENT=inprocess`（默认）：工具在编排进程内执行，便于测试。
- `STOCK_PILOT_TOOL_CLIENT=mcp`：四个工具真实走 MCP stdio，客户端自动拉起 Server 子进程。
- 审批队列读写与决策留在编排进程本地，因为审批台是人机管理面，不属于四个 MCP 工具。

**两阶段提交**：`update_price` 在同一事务里写 `staging` 和 `approvals`，商城不可见；审批通过后 `publish_listing` 在同一事务内完成「改价 + 上架 + 消费审批」。因此不存在「价格已改但未上架」的中间脏状态。

## 快速开始

Windows Git Bash：

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"

# 默认进程内模式
./.venv/Scripts/python.exe -m stock_pilot.api
# 浏览器打开 http://127.0.0.1:8000/

# 真实 MCP stdio 模式
STOCK_PILOT_TOOL_CLIENT=mcp ./.venv/Scripts/python.exe -m stock_pilot.api

# 全量回归
./.venv/Scripts/python.exe -m pytest -q
```

POSIX 把 `./.venv/Scripts/python.exe` 换成 `.venv/bin/python` 即可。

## 演示脚本

1. **正常调价**：提交 `SKU-TSHIRT-001` / `白色-M` / `59.00` → 审批队列出现提案 → 通过 → 任务 `verified`，商城 59 元且已上架，最终核对一致。
2. **库存不足**：提交 `SKU-HOODIE-003` / `黑色-L` → 任务 `aborted_no_stock`，审批队列为空，商城零写入。
3. **映射歧义**：提交 `SKU-AMBIG-002` / `白色-M` → 任务挂起，显示两个候选 → 人工选 `LST-002` → 进入审批 → 通过 → 核对。
4. **审批拒绝**：任意正常提案 → 拒绝并填写原因 → 任务 `rolled_back`，staging 丢弃，商城价格不变。
5. **重启恢复**：提交提案后重启 API → 原任务仍在队列，审批后正常完成。模拟「已决策未恢复」后，页面出现「待恢复任务」面板，点击「恢复执行」完成后续步骤。
6. **重复提交**：带相同 `Idempotency-Key` 连续提交两次，只产生一个任务和一条审批。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/tasks` | 提交任务；可选 `Idempotency-Key` 请求头 |
| `GET` | `/tasks?limit=100` | 任务流水 |
| `GET` | `/tasks/{id}` | 任务详情 |
| `GET` | `/tasks/{id}/events` | 工具调用时间线（每次尝试的结果与耗时） |
| `POST` | `/tasks/{id}/mapping` | 确认歧义商品的候选 listing |
| `POST` | `/tasks/{id}/recover` | 恢复「已决策但未继续」的任务 |
| `POST` | `/approvals/{id}/decision` | 审批：`approve` / `reject`，可带 `note` 与 `decided_by` |
| `GET` | `/approvals` | 审批记录，可按状态过滤 |
| `GET` | `/approvals/queue` | 审批台联表队列（商品 + 任务上下文） |
| `GET` | `/approvals/{id}` | 单条审批详情 |
| `GET` | `/recovery` | 只读重启对账结果 |

审批决策是幂等的：重复提交相同决策返回当前任务；提交相反决策返回 `409`。

## 失败语义

| 错误码 | 任务终态 | 后续写操作 |
|---|---|---|
| `INSUFFICIENT_STOCK` | `ABORTED_NO_STOCK` | 禁止 |
| `MAPPING_AMBIGUOUS` | `SUSPENDED_AMBIGUOUS` | 等待人工映射 |
| `APPROVAL_REJECTED` | `ROLLED_BACK` | 禁止，staging 已丢弃 |
| `TOOL_TIMEOUT` / `TOOL_UNAVAILABLE` / `CIRCUIT_OPEN`（写） | 一次只读对账；失败则 `ABORTED_UNKNOWN_STATE` | 禁止重发 |
| `PENDING_APPROVAL_EXISTS` | `ABORTED_FAILED` | 禁止抢占其他任务的提案 |
| `FAILED_VERIFICATION` | `FAILED_VERIFICATION` | 保留现场等待人工核查 |

## 测试

```bash
./.venv/Scripts/python.exe -m pytest -q          # 56 个用例
./.venv/Scripts/python.exe -m pytest tests/test_recovery.py -q
./.venv/Scripts/python.exe -m ruff check src tests
```

- `tests/test_tools_acceptance.py`：四条验收用例的工具层断言 + MCP 工具注册。
- `tests/test_agent_acceptance.py`：图编排、映射恢复、写超时对账、拒绝/超时后零后续写。
- `tests/test_runner.py`：超时重试、熔断、写不重试、业务失败不计入熔断。
- `tests/test_api.py`：FastAPI 操作接口与错误码。
- `tests/test_ui.py`：审批台静态资源、队列联表、恢复入口。
- `tests/test_mcp_client.py`：真实 MCP stdio 全链路、API/MCP 模式、服务不可用或中途退出时安全终止。
- `tests/test_recovery.py`：进程重启、幂等提交、审批幂等、操作者身份、工具时间线、恢复接口。

## 目录

```
src/stock_pilot/
  db.py           SQLite schema / 事务 / 演示数据 / 增量迁移
  models.py       Pydantic 契约（工具与 API）
  inventory.py    库存模块
  mall.py         商城模块（get_listing / update_price / publish_listing）
  approval.py     审批模块（人机管理面）
  tools.py        进程内工具门面
  clients.py      ToolClient 边界 + MCPToolClient
  runner.py       超时 / 重试 / 熔断 / 审计事件
  audit.py        工具调用时间线
  recovery.py     只读重启对账
  agent.py        LangGraph 状态机
  api.py          FastAPI + 静态审批台
  mcp_server.py   MCP Server（4 个工具）
  static/         零构建审批台
tests/            验收、单元、API、UI、MCP、恢复
```

## 设计取舍与生产化

第一版刻意保持单 SQLite、单 MCP Server，用 module 边界隔离库存 / 商城 / 审批，便于在本地演示完整闭环和故障路径。

生产化拆分方向：

1. 库存、商城、审批各自独立 MCP Server，走 Streamable HTTP + 服务间鉴权。
2. Postgres 替换 SQLite；跨服务提交从单库事务改为 saga / 补偿，明确每一步的一致性边界。
3. 真实登录与角色：审批人不能是提交人，审批接口不再裸奔。
4. 结构化日志、指标、告警与审批审计导出。

已知限制：当前操作者身份由审批台填写，不是认证；SQLite 适合单实例演示；MCP 模式默认只拉起一个 Server 子进程。
