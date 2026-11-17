# 面试演示脚本（约 8 分钟）

目标：让面试官看到「受约束 Agent」的证据，而不只是 happy path。

## 0. 启动（30 秒）

```bash
./.venv/Scripts/python.exe -m pytest -q               # 165 passed
./.venv/Scripts/python.exe -m ruff check src tests
./.venv/Scripts/python.exe -m stock_pilot.demo        # http://127.0.0.1:8000/
# --split 同时拉起两个 HTTP MCP Server 并拆成三个库；--fresh 只重建配置的演示库
```

先指出页面右上角「管理令牌」：未配置 Token 时留空即可；配置 Token 后它就是
操作身份入口。再看底部安全承诺与执行链路的 7 个步骤。

## 1. Happy path（1 分钟）

1. 点「正常调价」，提交 `SKU-TSHIRT-001 / 白色-M / 59.00`。
2. 指向审批卡：商城现价仍是 ¥79.00，提案只进了审批队列与 staging。
3. 填审批人，点「通过」。
4. 任务变为「已核对」，详情里能看到最终价、上架状态与核对时间。

要讲的三点：

- `update_price` 只写 staging + approval；`publish_listing` 才在**同一事务**
  里改价、上架、消费审批。
- 最终核对复用只读 `get_listing`，结果写进任务记录。
- `GET /tasks/{id}/events` 是每次工具尝试的时间线，不是一条「成功」日志。

```bash
curl -s localhost:8000/tasks/<task_id>/events
```

## 2. 三种失败路径（2 分钟）

### 库存不足

点「库存不足」提交 `SKU-HOODIE-003`：任务直接 `aborted_no_stock`，审批队列
为空。强调「零写操作」。

### 映射歧义

点「映射歧义」提交 `SKU-AMBIG-002`：任务挂起并列出两个候选，规格不匹配的
候选被禁用。人工选定后才进入审批。强调「系统不猜」。

### 审批拒绝

任意提案点「拒绝」，必须填原因。任务 `rolled_back`，staging 丢弃，商城价格
不变。

### 审批期间库存归零（可选，30 秒）

提交提案后，在另一个终端执行：

```bash
./.venv/Scripts/python.exe -c "import sqlite3; c=sqlite3.connect('data/stock_pilot.db'); c.execute(\"UPDATE inventory_records SET quantity=0 WHERE id='INV-001'\"); c.commit()"
```

再点「通过」：任务 `rolled_back / INSUFFICIENT_STOCK`，审批状态 `abandoned`，
staging 已丢弃，商城不变。强调 `abandoned` 是系统撤回，不是人工拒绝，审计上
区分开；提交事务内还有第二道库存校验。

## 3. 恢复与幂等（1.5 分钟）

1. 提交一个提案，重启 API。
2. 审批队列仍在，继续审批即可完成。
3. 用 SQL 模拟「审批已通过但进程在恢复前崩溃」：

```bash
./.venv/Scripts/python.exe -c "import sqlite3,datetime; c=sqlite3.connect('data/stock_pilot.db'); c.execute(\"UPDATE approvals SET status='approved', decided_at=? WHERE status='pending'\",(datetime.datetime.utcnow().isoformat(),)); c.commit()"
```

刷新页面：出现「待恢复任务」面板，点「恢复执行」，只执行已批准的
`publish_listing`，不新增写决策。

4. 带同一个 `Idempotency-Key` 连续提交两次，只有一个任务、一条审批。

## 4. 生产化拆分 + 鉴权（2 分钟）

```bash
export STOCK_PILOT_AGENT_TOKEN=demo-agent-token
export STOCK_PILOT_OPERATOR_TOKEN=demo-operator-token
./.venv/Scripts/python.exe -m stock_pilot.mcp_inventory &
./.venv/Scripts/python.exe -m stock_pilot.mcp_mall &
STOCK_PILOT_TOOL_CLIENT=mcp-http STOCK_PILOT_ADMIN_TOKEN=demo-admin-token \
  ./.venv/Scripts/python.exe -m stock_pilot.api
```

- 不加 Token 调 `POST /tasks` 得到 `401`；用 operator Token 提交得到 `403`；
  admin Token 可以走完整流程。
- 在页面「管理令牌」填 admin Token，完成一次调价。`decided_by` 与
  `abandoned_by` 来自 Token 身份，请求体里的值会被忽略。
- 库存与商城是两个独立 HTTP MCP Server，编排层按工具路由；审批面留在编排
  进程，因为它是人机管理面。

更省事的三库演示：`./.venv/Scripts/python.exe -m stock_pilot.demo --split`，
启动日志会列出 `orchestrator.db` / `inventory.db` / `mall.db` 三个文件。

主动说明边界：split 布局下三个库彼此独立，发布链路是「复核 → 内部预留 →
商城事务提交 → 释放预留」的 saga；提交后响应丢失时只做一次只读对账并采纳
已提交事实，绝不重发写。进程在预留后崩溃会留下占用，恢复面板会显示
`reservation_orphaned` 并提供「释放遗留预留」（`POST /reservations/gc`）；
该接口只释放审批已终止或缺失的预留，审批仍可发布时即使超过 TTL 也只标记
`reservation_stale`，不会抢走库存。长驻部署可设置
`STOCK_PILOT_RESERVATION_GC_INTERVAL` 让进程周期执行同一清理（默认 0 关闭）。静态 Token 只是演示级身份，生产应换 OIDC
并强制审批人与提交人分离。

### 预留清理（可选，30 秒）

`--split` 运行时，模拟「预留后进程崩溃」留下的孤立预留：

```bash
./.venv/Scripts/python.exe -c "import datetime,sqlite3; c=sqlite3.connect('data/inventory.db'); c.execute(\"UPDATE inventory_records SET reserved_by='apr-ghost', reserved_at=? WHERE id='INV-001'\",(datetime.datetime.now(datetime.timezone.utc).isoformat(),)); c.commit()"
```

刷新页面：恢复面板出现「遗留库存预留」，点「释放遗留预留」（或
`POST /reservations/gc`）。说明清理只按审批/任务状态判断，不看 TTL；如果审批仍可
发布，即使预留超龄也只标记 `reservation_stale`。

## 5. 自然语言入口（可选，30 秒）

主演示不需要任何模型 Key：入口默认关闭，审批台的「自然语言草稿」面板会显示
一行说明，`POST /tasks/intake` 返回 `503`。先说明「模型不在这条执行链上」，
手工表单仍然可用。

若本地配置了 `STOCK_PILOT_LLM=anthropic` 与 `STOCK_PILOT_LLM_API_KEY`：

1. 刷新审批台，面板出现输入框，右上角显示模型名；`GET /capabilities` 只返回
   `intake_enabled`、`llm_model`、`db_layout`、`auth_enabled`，不含 Key。
2. 输入「把 SKU-TSHIRT-001 白色-M 调到 59 元，清库存」，点「生成草稿」。
   面板展示只读的 SKU / 规格 / 目标价 / 理由与 warnings；此时任务流水仍为空。
3. 点「确认提交任务」，前端只把四个任务字段发给 `POST /tasks`，随后照常进入
   审批队列。强调三点：模型没有工具；多余字段、越界价格、缺失字段会被拒绝；
   草稿只存在页面内存，5 秒自动刷新不重建，刷新页面即丢弃。

也可以直接用命令行复现草稿：

```bash
curl -s -X POST localhost:8000/tasks/intake \
  -H 'Content-Type: application/json' \
  -d '{"text":"把 SKU-TSHIRT-001 白色-M 调到 59 元，清库存"}'
```

## 6. 审计导出（可选，20 秒）

在审批台顶栏点「导出审计」，浏览器下载一份 JSON（或
`GET /audit/export`，需 operator Token）。指出三点：它只读任务、审批、工具
时间线与通知元数据；不包含 Token / API Key，也不返回通知 payload 或投递错误
文本；导出前后任务与审批数据不变。`split` 布局下分别读取编排库与商城库，
是操作员快照，不是备份。

## 7. 通知投递面板（可选，20 秒）

审批台下方「通知投递」显示 pending / delivered / dead 三个计数。正常情况下
死信为 0；说明 `POST /outbox/{id}/retry` 只重发通知，worker 不持有工具客户端，
所以即使人工重投也不可能重放 `update_price` / `publish_listing`。死信事件会
列出 attempts 与 `last_error`，operator 可逐条重投。

## 8. 收尾（30 秒）

`GET /healthz` 返回数据库与工具客户端就绪状态（不可用时 503），部署层据此
摘流量。整个设计文档见 `README.md`，关键取舍见 `docs/adr/`。
