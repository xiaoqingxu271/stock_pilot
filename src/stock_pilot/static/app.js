(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);

  const TASK_STATUS = {
    received: { label: "已接收", tone: "neutral" },
    awaiting_approval: { label: "待审批", tone: "warn" },
    suspended_ambiguous: { label: "待确认映射", tone: "info" },
    published: { label: "已上架", tone: "info" },
    verified: { label: "已核对", tone: "success" },
    rolled_back: { label: "已回滚", tone: "warn" },
    aborted_no_stock: { label: "库存不足终止", tone: "danger" },
    aborted_not_found: { label: "未找到商品终止", tone: "danger" },
    aborted_unknown_state: { label: "状态未知终止", tone: "danger" },
    aborted_failed: { label: "执行失败终止", tone: "danger" },
    failed_verification: { label: "核对失败", tone: "danger" },
  };

  const LISTING_STATUS = {
    draft: "草稿",
    published: "已上架",
    off_shelf: "已下架",
  };

  const APPROVAL_STATUS = {
    pending: { label: "待审批", tone: "warn" },
    approved: { label: "已通过", tone: "info" },
    rejected: { label: "已拒绝", tone: "danger" },
    consumed: { label: "已提交", tone: "success" },
    abandoned: { label: "系统撤回", tone: "danger" },
  };

  const RECOVERY_KIND = {
    approved_not_published: "已审批未提交",
    rejected_not_closed: "已拒绝未关闭",
    abandoned_not_closed: "系统撤回未关闭",
    published_verify_pending: "已提交待核对",
    approval_missing: "审批记录缺失",
    approved_without_staging: "有效提案缺失",
    staging_orphaned: "遗留提案",
    interrupted_before_approval: "执行中断",
  };

  const state = {
    approvals: [],
    tasks: [],
    recovery: [],
    query: "",
    loading: false,
    mutating: 0,
    pendingRender: false,
    lastSignature: "",
    lastUpdated: null,
  };

  const draftNotes = new Map();
  const draftChoices = new Map();

  const refs = {
    live: $("#live-indicator"),
    liveText: $("#live-text"),
    refresh: $("#refresh-btn"),
    recoveryPanel: $("#recovery-panel"),
    recoveryList: $("#recovery-list"),
    recoveryCount: $("#recovery-count"),
    adminToken: $("#admin-token"),
    operator: $("#operator-name"),
    approvalList: $("#approval-list"),
    approvalCount: $("#approval-count"),
    mappingList: $("#mapping-list"),
    mappingCount: $("#mapping-count"),
    taskList: $("#task-list"),
    taskCount: $("#task-count"),
    taskFilter: $("#task-filter"),
    form: $("#task-form"),
    sku: $("#sku-input"),
    spec: $("#spec-input"),
    price: $("#price-input"),
    reason: $("#reason-input"),
    submit: $("#submit-btn"),
    formHint: $("#form-hint"),
    toast: $("#toast"),
    statPending: $("#stat-pending"),
    statMapping: $("#stat-mapping"),
    statActive: $("#stat-active"),
    statFinished: $("#stat-finished"),
  };

  function h(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function api(path, options = {}) {
    const headers = {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    };
    const token = refs.adminToken ? refs.adminToken.value.trim() : "";
    if (token) headers.Authorization = `Bearer ${token}`;
    return fetch(path, { ...options, headers }).then(async (response) => {
      const raw = await response.text();
      let body = null;
      if (raw) {
        try {
          body = JSON.parse(raw);
        } catch {
          body = raw;
        }
      }
      if (!response.ok) {
        const detail =
          body && typeof body === "object" && body.detail
            ? body.detail
            : `${response.status} ${response.statusText}`;
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return body;
    });
  }

  function money(value) {
    if (value === null || value === undefined || value === "") return "—";
    const number = Number(value);
    return Number.isFinite(number) ? `¥${number.toFixed(2)}` : String(value);
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function shortId(value) {
    if (!value) return "—";
    return value.length > 20 ? `${value.slice(0, 20)}…` : value;
  }

  function statusMeta(status) {
    return TASK_STATUS[status] || { label: status || "未知", tone: "neutral" };
  }

  function chip(status) {
    const meta = statusMeta(status);
    return h("span", `chip chip-${meta.tone}`, meta.label);
  }

  function approvalChip(status) {
    const meta = APPROVAL_STATUS[status] || { label: status || "未知", tone: "neutral" };
    return h("span", `chip chip-${meta.tone}`, meta.label);
  }

  function listingChip(status) {
    const tone =
      status === "published" ? "success" : status === "off_shelf" ? "neutral" : "info";
    const label = LISTING_STATUS[status] || status;
    return h("span", `chip chip-${tone}`, label);
  }

  function listingStatusLabel(status) {
    return status ? LISTING_STATUS[status] || status : "—";
  }

  function outcomeKind(status) {
    if (status === "verified") return "success";
    if (status === "awaiting_approval" || status === "published") return "success";
    if (status === "rolled_back") return "info";
    if (
      typeof status === "string" &&
      (status.startsWith("aborted") || status === "failed_verification")
    ) {
      return "error";
    }
    return "info";
  }

  let toastTimer = null;
  function toast(message, kind = "info") {
    const node = refs.toast;
    node.textContent = message;
    node.className = `toast is-visible toast-${kind}`;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(
      () => node.classList.remove("is-visible"),
      kind === "error" ? 5200 : 3600
    );
  }

  function emptyState(title, hint) {
    const node = h("div", "empty");
    node.append(h("strong", null, title), h("p", null, hint));
    return node;
  }

  function hasActiveInput() {
    const active = document.activeElement;
    if (!active) return false;
    if (active.tagName !== "INPUT" && active.tagName !== "TEXTAREA") return false;
    return Boolean(active.closest("#approval-list, #mapping-list"));
  }

  function addKv(list, key, value, mono = false) {
    list.append(h("dt", null, key), h("dd", mono ? "mono" : null, value));
  }

  function verificationBlock(verification) {
    const result = verification.result;
    const ok = Boolean(result.matched);
    const box = h("div", `verification ${ok ? "ok" : "bad"}`);
    box.append(h("span", "verification-icon", ok ? "✓" : "✗"));
    const text = ok
      ? `核对一致：${money(result.actual_price)} · ${listingStatusLabel(result.actual_status)}`
      : `核对不一致：期望 ${money(result.expected_price)} / ${listingStatusLabel(result.expected_status)}，实际 ${money(result.actual_price)} / ${listingStatusLabel(result.actual_status)}`;
    box.append(h("span", null, text));
    return box;
  }

  // ------------------------------------------------------------------
  // Restart recovery
  // ------------------------------------------------------------------

  function recoveryCard(issue) {
    const label = RECOVERY_KIND[issue.kind] || issue.kind;
    const card = h("article", "recovery-card");

    const title = issue.sku_id
      ? `${issue.sku_id} · ${issue.spec}`
      : `listing ${issue.listing_id || "—"}`;
    const head = h("div", "card-head");
    const titleWrap = h("div");
    titleWrap.append(
      h("h3", "card-title", title),
      h(
        "p",
        "card-meta",
        `${label} · ${issue.task_id ? `task ${shortId(issue.task_id)}` : "无关联任务"}`
      )
    );
    head.append(titleWrap, h("span", "chip chip-warn", label));
    card.append(head);

    card.append(h("p", "recovery-message", issue.message));
    card.append(
      h(
        "p",
        "card-meta",
        `当前 ${issue.task_status ? statusMeta(issue.task_status).label : "未知"} · 目标 ${money(
          issue.target_price
        )} · listing ${issue.listing_id || "—"}`
      )
    );

    const actions = h("div", "card-actions");
    const recoverable =
      (issue.task_status === "awaiting_approval" &&
        [
          "approved_not_published",
          "rejected_not_closed",
          "abandoned_not_closed",
        ].includes(issue.kind)) ||
      (issue.task_status === "published" &&
        issue.kind === "published_verify_pending");

    if (issue.task_id) {
      const recover = h("button", "btn btn-primary btn-small", "恢复执行");
      recover.type = "button";
      recover.disabled = !recoverable;
      if (!recoverable) recover.title = "该状态需要人工核查，不能自动继续";
      actions.append(recover);

      let busy = false;
      recover.addEventListener("click", async () => {
        if (busy || !recoverable) return;
        busy = true;
        recover.disabled = true;
        recover.textContent = "恢复中…";
        state.mutating += 1;
        try {
          const updated = await api(
            `/tasks/${encodeURIComponent(issue.task_id)}/recover`,
            { method: "POST", body: "{}" }
          );
          toast(
            `恢复完成：${statusMeta(updated.status).label}`,
            outcomeKind(updated.status)
          );
          await refresh({ force: true });
        } catch (error) {
          toast(error.message, "error");
          recover.disabled = false;
          recover.textContent = "恢复执行";
        } finally {
          state.mutating -= 1;
          busy = false;
        }
      });
    }

    if (issue.kind === "staging_orphaned" && issue.approval_id) {
      const abandon = h("button", "btn btn-danger-outline btn-small", "撤回遗留提案");
      abandon.type = "button";
      abandon.title = "先只读对账；若商城已提交，接口会拒绝撤回";
      actions.append(abandon);

      let busy = false;
      abandon.addEventListener("click", async () => {
        if (busy) return;
        busy = true;
        abandon.disabled = true;
        abandon.textContent = "撤回中…";
        state.mutating += 1;
        try {
          await api(
            `/approvals/${encodeURIComponent(issue.approval_id)}/abandon`,
            {
              method: "POST",
              body: JSON.stringify({ reason: "审批台清理遗留提案" }),
            }
          );
          toast("遗留提案已撤回，staging 已丢弃", "success");
          await refresh({ force: true });
        } catch (error) {
          toast(error.message, "error");
          abandon.disabled = false;
          abandon.textContent = "撤回遗留提案";
        } finally {
          state.mutating -= 1;
          busy = false;
        }
      });
    }

    if (actions.childElementCount) card.append(actions);

    return card;
  }

  function renderRecovery() {
    const issues = state.recovery;
    refs.recoveryPanel.hidden = issues.length === 0;
    refs.recoveryCount.textContent = String(issues.length);
    refs.recoveryList.replaceChildren();
    issues.forEach((issue) =>
      refs.recoveryList.append(recoveryCard(issue))
    );
  }

  // ------------------------------------------------------------------
  // Approval cards
  // ------------------------------------------------------------------

  function approvalCard(item) {
    const card = h("article", "approval-card");

    const head = h("div", "card-head");
    const titleWrap = h("div");
    titleWrap.append(
      h("h3", "card-title", item.title || "未命名商品"),
      h(
        "p",
        "card-meta",
        `SKU ${item.sku_id} · ${item.spec} · listing ${item.listing_id} · 提交人 ${
          item.submitted_by || "—"
        }`
      )
    );
    head.append(titleWrap, approvalChip(item.status));
    card.append(head);

    const priceRow = h("div", "price-row");
    priceRow.append(
      h("span", "price-old", money(item.original_price)),
      h("span", "price-arrow", "→"),
      h("span", "price-new", money(item.new_price))
    );
    const delta = Number(item.new_price) - Number(item.original_price);
    if (Number.isFinite(delta) && Number(item.original_price) > 0) {
      const percent = Math.abs((delta / Number(item.original_price)) * 100);
      priceRow.append(
        h(
          "span",
          `price-delta ${delta < 0 ? "down" : "up"}`,
          `${delta < 0 ? "↓" : "↑"} ${percent.toFixed(1)}%`
        )
      );
    }
    card.append(priceRow);

    if (Number(item.current_price) !== Number(item.original_price)) {
      card.append(
        h(
          "p",
          "warning-line",
          `商城现价已变为 ${money(item.current_price)}，与提案原价不一致，提交将被拒绝。`
        )
      );
    }

    const reason = h("div", "reason");
    reason.append(
      h("span", "reason-label", "提案理由"),
      h("p", "reason-text", item.proposal_reason)
    );
    card.append(reason);

    const foot = h("div", "card-foot");
    const taskLabel = h(
      "span",
      "mono",
      item.task_id ? shortId(item.task_id) : "无关联任务"
    );
    if (item.task_id) taskLabel.title = item.task_id;
    foot.append(taskLabel, h("span", null, formatDate(item.created_at)));
    card.append(foot);

    const note = h("textarea", "approval-note");
    note.rows = 2;
    note.placeholder = "审批备注（拒绝时必填）";
    note.setAttribute("aria-label", "审批备注");
    note.value = draftNotes.get(item.approval_id) || "";
    note.addEventListener("input", () => draftNotes.set(item.approval_id, note.value));
    card.append(note);

    const actions = h("div", "card-actions");
    const reject = h("button", "btn btn-danger-outline", "拒绝");
    reject.type = "button";
    const approve = h("button", "btn btn-success", "通过");
    approve.type = "button";
    actions.append(reject, approve);
    card.append(actions);

    let busy = false;
    function setBusy(value) {
      busy = value;
      reject.disabled = value;
      approve.disabled = value;
      note.disabled = value;
      reject.textContent = value ? "处理中…" : "拒绝";
      approve.textContent = value ? "处理中…" : "通过";
    }

    async function decide(decision) {
      if (busy) return;
      const text = note.value.trim();
      if (decision === "reject" && !text) {
        toast("拒绝时请填写原因", "error");
        note.focus();
        return;
      }
      setBusy(true);
      state.mutating += 1;
      try {
        const updated = await api(
          `/approvals/${encodeURIComponent(item.approval_id)}/decision`,
          {
            method: "POST",
            body: JSON.stringify({
              decision,
              note: text || null,
              decided_by: refs.operator.value.trim() || null,
            }),
          }
        );
        draftNotes.delete(item.approval_id);
        toast(
          `${decision === "approve" ? "已通过" : "已拒绝"}：${statusMeta(updated.status).label}`,
          outcomeKind(updated.status)
        );
        await refresh({ force: true });
      } catch (error) {
        toast(error.message, "error");
        setBusy(false);
      } finally {
        state.mutating -= 1;
      }
    }

    reject.addEventListener("click", () => decide("reject"));
    approve.addEventListener("click", () => decide("approve"));

    return card;
  }

  function renderApprovals() {
    refs.approvalList.replaceChildren();
    refs.approvalCount.textContent = String(state.approvals.length);
    if (!state.approvals.length) {
      refs.approvalList.append(
        emptyState(
          "暂无待审批提案",
          "提交调价任务后，通过库存与映射检查的提案会出现在这里。"
        )
      );
      return;
    }
    state.approvals.forEach((item) =>
      refs.approvalList.append(approvalCard(item))
    );
  }

  // ------------------------------------------------------------------
  // Mapping cards
  // ------------------------------------------------------------------

  function mappingCard(task) {
    const card = h("article", "mapping-card");
    card.dataset.task = task.task_id;

    const head = h("div", "card-head");
    const titleWrap = h("div");
    titleWrap.append(
      h("h3", null, "需要人工确认商品映射"),
      h(
        "p",
        "card-meta",
        `SKU ${task.sku_id} · ${task.spec} · 目标价 ${money(task.target_price)}`
      )
    );
    head.append(titleWrap, chip(task.status));
    card.append(head);

    card.append(
      h(
        "p",
        "mapping-hint",
        "get_listing 返回多个同名商品。系统不会猜测，请从候选中选择正确 listing。"
      )
    );

    const options = h("div", "candidate-list");
    options.setAttribute("role", "radiogroup");
    options.setAttribute("aria-label", "商品候选");
    const groupName = `candidate-${task.task_id}`;

    task.candidates.forEach((candidate) => {
      const mismatch = candidate.spec !== task.spec;
      const label = h(
        "label",
        `candidate${mismatch ? " is-disabled" : ""}`
      );
      const radio = h("input");
      radio.type = "radio";
      radio.name = groupName;
      radio.value = candidate.listing_id;
      radio.disabled = mismatch;
      if (!mismatch && draftChoices.get(task.task_id) === candidate.listing_id) {
        radio.checked = true;
        label.classList.add("is-selected");
      }

      const body = h("div", "candidate-body");
      const top = h("div", "candidate-top");
      top.append(
        h("strong", "candidate-title", candidate.title),
        listingChip(candidate.status)
      );
      body.append(
        top,
        h(
          "p",
          "candidate-meta",
          `${candidate.spec} · ${money(candidate.price)} · listing ${candidate.listing_id} · 库存记录 ${candidate.inventory_record_id}`
        )
      );
      if (mismatch) {
        body.append(
          h(
            "p",
            "candidate-warning",
            `规格与任务不一致（任务为 ${task.spec}），不能选择`
          )
        );
      }
      label.append(radio, body);
      options.append(label);
    });

    const confirm = h("button", "btn btn-primary btn-small", "确认映射并继续");
    confirm.type = "button";
    confirm.disabled = !options.querySelector("input:checked");

    options.addEventListener("change", () => {
      options.querySelectorAll(".candidate").forEach((label) => {
        const radio = label.querySelector("input");
        label.classList.toggle("is-selected", Boolean(radio && radio.checked));
      });
      confirm.disabled = !options.querySelector("input:checked");
      const checked = options.querySelector("input:checked");
      if (checked) draftChoices.set(task.task_id, checked.value);
    });

    const actions = h("div", "mapping-actions");
    actions.append(confirm, h("span", "mapping-note", "确认后进入审批队列"));
    card.append(options, actions);

    const allDisabled = task.candidates.every((c) => c.spec !== task.spec);
    if (allDisabled) {
      card.append(
        h(
          "p",
          "warning-line",
          "没有规格匹配的候选，请人工核查商品数据后再处理。"
        )
      );
    }

    confirm.addEventListener("click", async () => {
      const checked = options.querySelector("input:checked");
      if (!checked) return;
      confirm.disabled = true;
      confirm.textContent = "确认中…";
      state.mutating += 1;
      try {
        const updated = await api(
          `/tasks/${encodeURIComponent(task.task_id)}/mapping`,
          {
            method: "POST",
            body: JSON.stringify({ listing_id: checked.value }),
          }
        );
        draftChoices.delete(task.task_id);
        toast(
          `映射已确认：${statusMeta(updated.status).label}`,
          outcomeKind(updated.status)
        );
        await refresh({ force: true });
      } catch (error) {
        toast(error.message, "error");
        confirm.disabled = false;
        confirm.textContent = "确认映射并继续";
      } finally {
        state.mutating -= 1;
      }
    });

    return card;
  }

  function renderMappings() {
    const mappings = state.tasks.filter(
      (task) => task.status === "suspended_ambiguous"
    );
    refs.mappingList.replaceChildren();
    refs.mappingCount.textContent = String(mappings.length);
    if (!mappings.length) {
      refs.mappingList.append(
        emptyState("没有待确认的映射", "出现同名商品时，任务会安全挂起并出现在这里。")
      );
      return;
    }
    mappings.forEach((task) => refs.mappingList.append(mappingCard(task)));
  }

  // ------------------------------------------------------------------
  // Task list
  // ------------------------------------------------------------------

  function taskMatches(task) {
    if (!state.query) return true;
    const haystack = [
      task.sku_id,
      task.spec,
      task.status,
      statusMeta(task.status).label,
      task.error_code,
      task.listing_id,
      task.task_id,
      task.approval_id,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(state.query);
  }

  function taskRow(task) {
    const meta = statusMeta(task.status);
    const row = h("article", `task-row tone-${meta.tone}`);

    const head = h("div", "task-row-head");
    const main = h("div", "task-main");
    main.append(
      h("p", "task-title", `${task.sku_id} · ${task.spec}`),
      h(
        "p",
        "task-sub",
        `目标 ${money(task.target_price)} · ${
          task.listing_id ? `listing ${task.listing_id}` : "listing 未确定"
        }`
      )
    );

    const right = h("div", "task-right");
    right.append(chip(task.status), h("span", "muted", formatDate(task.updated_at)));

    if (task.status === "suspended_ambiguous") {
      const jump = h("button", "btn btn-small btn-ghost", "去确认映射");
      jump.type = "button";
      jump.addEventListener("click", () => {
        const panel = document.getElementById("mapping-panel");
        panel.scrollIntoView({ behavior: "smooth", block: "center" });
        window.setTimeout(() => {
          const radio = panel.querySelector(
            `[data-task="${task.task_id}"] input:not(:disabled)`
          );
          if (radio) radio.focus();
        }, 320);
      });
      right.append(jump);
    }

    head.append(main, right);
    row.append(head);

    const details = h("details", "task-details");
    details.append(h("summary", null, "详情"));

    const body = h("div", "details-body");
    const kv = h("dl", "kv");
    addKv(kv, "任务 ID", task.task_id, true);
    addKv(kv, "审批 ID", task.approval_id || "—", true);
    addKv(kv, "提交人", task.submitted_by || "—");
    addKv(
      kv,
      "原价 / 最终价",
      `${task.original_price ? money(task.original_price) : "—"} / ${
        task.final_price ? money(task.final_price) : "—"
      }`
    );
    if (task.error_code) addKv(kv, "错误码", task.error_code, true);
    body.append(kv);

    if (task.verification) {
      body.append(verificationBlock(task.verification));
      body.append(
        h("p", "candidates-summary", `核对时间：${formatDate(task.verification.verified_at)}`)
      );
    }

    if (task.candidates && task.candidates.length) {
      body.append(
        h(
          "p",
          "candidates-summary",
          `候选：${task.candidates
            .map(
              (candidate) =>
                `${candidate.spec} ${money(candidate.price)} (${candidate.listing_id})`
            )
            .join(" · ")}`
        )
      );
    }

    details.append(body);
    row.append(details);
    return row;
  }

  function renderTasks() {
    const filtered = state.tasks.filter(taskMatches);
    refs.taskCount.textContent = state.query
      ? `${filtered.length}/${state.tasks.length}`
      : String(state.tasks.length);
    refs.taskList.replaceChildren();
    if (!filtered.length) {
      refs.taskList.append(
        emptyState(
          state.query ? "没有匹配的任务" : "还没有任务记录",
          state.query
            ? "换个关键词，或清空过滤条件。"
            : "提交一个任务后，完整执行轨迹会出现在这里。"
        )
      );
      return;
    }
    filtered.forEach((task) => refs.taskList.append(taskRow(task)));
  }

  // ------------------------------------------------------------------
  // Stats & render
  // ------------------------------------------------------------------

  function renderStats() {
    const active = state.tasks.filter((task) =>
      ["received", "awaiting_approval", "published"].includes(task.status)
    ).length;
    const finished = state.tasks.filter((task) =>
      [
        "verified",
        "rolled_back",
        "aborted_no_stock",
        "aborted_not_found",
        "aborted_unknown_state",
        "aborted_failed",
        "failed_verification",
      ].includes(task.status)
    ).length;
    const mapping = state.tasks.filter(
      (task) => task.status === "suspended_ambiguous"
    ).length;

    refs.statPending.textContent = String(state.approvals.length);
    refs.statMapping.textContent = String(mapping);
    refs.statActive.textContent = String(active);
    refs.statFinished.textContent = String(finished);
  }

  function render(force = false) {
    if (hasActiveInput()) {
      state.pendingRender = true;
      return;
    }
    const signature = JSON.stringify([
      state.approvals,
      state.tasks,
      state.recovery,
    ]);
    if (!force && signature === state.lastSignature) return;
    state.lastSignature = signature;
    renderStats();
    renderRecovery();
    renderApprovals();
    renderMappings();
    renderTasks();
  }

  async function refresh({ manual = false, force = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    try {
      const [approvals, tasks, recovery] = await Promise.all([
        api("/approvals/queue"),
        api("/tasks?limit=100"),
        api("/recovery"),
      ]);
      state.approvals = Array.isArray(approvals) ? approvals : [];
      state.tasks = Array.isArray(tasks) ? tasks : [];
      state.recovery = Array.isArray(recovery) ? recovery : [];
      state.lastUpdated = new Date();
      render(force);
      updateLive(true);
    } catch (error) {
      updateLive(false);
      if (manual) toast(error.message || "刷新失败", "error");
      console.error(error);
    } finally {
      state.loading = false;
    }
  }

  function updateLive(ok) {
    refs.live.classList.toggle("is-offline", !ok);
    refs.liveText.textContent = ok ? "实时同步" : "连接中断";
    refs.live.title = state.lastUpdated
      ? `最近同步：${state.lastUpdated.toLocaleTimeString("zh-CN")}`
      : ok
        ? "每 5 秒自动刷新"
        : "刷新失败";
  }

  // ------------------------------------------------------------------
  // Events & boot
  // ------------------------------------------------------------------

  function bindForm() {
    refs.form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!refs.form.reportValidity()) return;

      const payload = {
        sku_id: refs.sku.value.trim(),
        spec: refs.spec.value.trim(),
        target_price: refs.price.value.trim(),
        reason: refs.reason.value.trim(),
      };

      refs.submit.disabled = true;
      refs.submit.textContent = "提交中…";
      state.mutating += 1;
      try {
        const task = await api("/tasks", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        if (task.status === "awaiting_approval") {
          refs.formHint.textContent = "提案已进入审批队列，等待人工审批。";
        } else if (task.status === "suspended_ambiguous") {
          refs.formHint.textContent = `任务已挂起，请在左侧候选列表中确认（${task.candidates.length} 个候选）。`;
        } else if (task.status === "aborted_no_stock") {
          refs.formHint.textContent = "库存 ≤ 0，任务在写操作前终止。";
        } else {
          refs.formHint.textContent = `任务状态：${statusMeta(task.status).label}`;
        }
        toast(
          `任务已创建：${statusMeta(task.status).label}`,
          outcomeKind(task.status)
        );
        await refresh({ force: true });
      } catch (error) {
        refs.formHint.textContent = error.message;
        toast(error.message, "error");
      } finally {
        refs.submit.disabled = false;
        refs.submit.textContent = "提交任务";
        state.mutating -= 1;
      }
    });
  }

  function bindScenarios() {
    const scenarios = Array.from(document.querySelectorAll(".scenario"));
    scenarios.forEach((button) => {
      button.addEventListener("click", () => {
        scenarios.forEach((item) =>
          item.classList.toggle("is-active", item === button)
        );
        refs.sku.value = button.dataset.sku || "";
        refs.spec.value = button.dataset.spec || "";
        refs.price.value = button.dataset.price || "";
        refs.reason.value = button.dataset.reason || "";
      });
    });
  }

  function bindFilter() {
    refs.taskFilter.addEventListener("input", () => {
      state.query = refs.taskFilter.value.trim().toLowerCase();
      renderTasks();
    });
  }

  function bindOperator() {
    const key = "stockPilotOperator";
    const tokenKey = "stockPilotAdminToken";
    try {
      refs.operator.value = window.localStorage.getItem(key) || "";
      refs.adminToken.value = window.sessionStorage.getItem(tokenKey) || "";
    } catch {
      // Storage may be unavailable; the fields still work for this page.
    }
    refs.operator.addEventListener("input", () => {
      try {
        window.localStorage.setItem(key, refs.operator.value.trim());
      } catch {
        // Ignore storage failures.
      }
    });
    // Keep the bearer token in sessionStorage: it dies with the tab.
    let tokenTimer = null;
    refs.adminToken.addEventListener("input", () => {
      try {
        window.sessionStorage.setItem(tokenKey, refs.adminToken.value.trim());
      } catch {
        // Ignore storage failures.
      }
      // When read auth is on, loading the page before entering a token gets
      // 401s; retry shortly after the operator types one.
      window.clearTimeout(tokenTimer);
      tokenTimer = window.setTimeout(() => refresh({ force: true }), 400);
    });
  }

  function bindGlobal() {
    refs.refresh.addEventListener("click", () => refresh({ manual: true, force: true }));

    document.addEventListener("focusout", () => {
      window.setTimeout(() => {
        if (state.pendingRender) {
          state.pendingRender = false;
          render();
        }
      }, 0);
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });

    window.setInterval(() => {
      if (!document.hidden && state.mutating === 0) refresh();
    }, 5000);
  }

  bindForm();
  bindScenarios();
  bindFilter();
  bindOperator();
  bindGlobal();
  refresh({ manual: true });
})();
