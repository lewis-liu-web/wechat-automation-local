import {
  agent_jobs,
  agent_jobs_stats,
  get_agent_job,
  create_agent_test_job,
  retry_job_send,
  recover_job_result,
  dismiss_job,
  dismiss_jobs_batch,
  agent_provider_health,
  run_agent_worker_once,
  run_dispatcher_once,
  run_reconciler_once,
  run_sender_once,
  agent_worker_status,
  start_agent_worker,
  stop_agent_worker,
  list_agent_instances,
  discover_hermes_profiles,
  register_agent_instance,
  start_agent_instance,
  async_loop_status,
  start_async_loop,
  stop_async_loop,
  agent_pool_status,
  list_targets,
} from "../api.js";
import {
  h,
  clear,
  tableEl,
  toast,
  fmtTs,
  spinner,
  confirmDanger,
  emptyState,
  metricCard,
  detailsEl,
  pageHeader,
} from "../ui.js";

function jobStatusForDisplay(job) {
  const status = String(job.status || "");
  if (status === "sent" && job.send_status === "skipped") return "skipped";
  return status;
}

function jobStatusLabel(value) {
  return (
    {
      queued: "排队中",
      dispatching: "派发中",
      submitted: "已派给 Agent",
      agent_running: "Agent 处理中",
      running: "处理中",
      done: "处理完成",
      sending: "准备发送",
      sent: "已发送",
      skipped: "已跳过发送",
      failed: "处理失败",
      timeout: "处理超时",
      expired: "等待过期",
      cancelled: "已取消",
    }[String(value || "")] || String(value || "—")
  );
}

function sendStatusLabel(value) {
  return (
    {
      pending: "待发送",
      sent: "已发送",
      failed: "发送失败",
      skipped: "已跳过",
    }[String(value || "")] || String(value || "—")
  );
}

function providerLabel(value) {
  return (
    {
      echo: "安全测试（不调用真实模型）",
      hermes: "Hermes 本地 Agent",
    }[String(value || "")] || String(value || "—")
  );
}

function jobSummary(job) {
  const status = String(job.status || "");
  if (status === "queued") return "等待后台处理";
  if (status === "dispatching") return "正在派给 Agent";
  if (status === "submitted") return "已派给 Agent，等待首次轮询";
  if (status === "agent_running") return "Agent 正在处理，等待结果";
  if (status === "running") return "任务正在执行";
  if (status === "expired") return "自动等待窗口已过期，可尝试拉取结果";
  if (status === "timeout") return "处理超时，建议重试或检查处理服务";
  if (status === "failed") {
    const err = String(job.error || "未知原因");
    return "处理失败：" + err.slice(0, 80);
  }
  if (status === "sent" && job.send_status === "skipped") return "Agent 已处理，无需回复";
  const txt = String(job.result_text || job.error || "");
  return txt.slice(0, 100);
}

function poolStatusLabel(value) {
  return (
    {
      busy: "处理中",
      idle: "空闲",
      offline: "离线",
      available: "可上岗",
    }[String(value || "")] || String(value || "—")
  );
}

function instanceKindLabel(kind) {
  return { hermes: "Hermes profile/worker", echo: "Echo" }[String(kind)] || String(kind || "—");
}

function errorBanner(msg, onRetry) {
  return h(
    "div",
    { class: "error-banner" },
    h("span", { text: msg }),
    h("button", { class: "btn btn-sm", text: "重试", onclick: onRetry })
  );
}

export async function render(root) {
  clear(root);
  const timers = [];
  let autoRefreshTimer = null;
  const state = {
    targets: [],
    stats: null,
    workerStatus: null,
    loopState: null,
    poolState: null,
    instances: [],
    discovered: [],
    jobs: [],
    jobFilters: { range: "全部任务", limit: 50, group: "", autoRefresh: false },
    healthCache: {},
    detailCache: {},
    lastError: null,
  };

  function trackTimer(t) {
    timers.push(t);
    return t;
  }
  function stopTimers() {
    timers.forEach((t) => clearTimeout(t));
    clearInterval(autoRefreshTimer);
  }

  async function loadAll() {
    try {
      const [stats, ws, loop, pool, inst, disc, targetsRes] = await Promise.all([
        agent_jobs_stats().catch(() => ({})),
        agent_worker_status().catch(() => ({})),
        async_loop_status().catch(() => ({})),
        agent_pool_status().catch(() => ({})),
        list_agent_instances().catch(() => []),
        discover_hermes_profiles().catch(() => []),
        list_targets("all").catch(() => ({ targets: [] })),
      ]);
      state.stats = stats || {};
      state.workerStatus = ws || {};
      state.loopState = loop || {};
      state.poolState = pool || {};
      state.instances = Array.isArray(inst) ? inst : (inst.instances || []);
      state.discovered = Array.isArray(disc) ? disc : (disc.profiles || []);
      state.targets = (Array.isArray(targetsRes) ? targetsRes : (targetsRes.targets || [])).filter((t) => t.enabled);
      state.lastError = null;
      await loadJobs();
    } catch (e) {
      state.lastError = e.message || String(e);
    }
    rebuild();
  }

  async function loadJobs() {
    const map = { 仅排队中: "queued", 仅已完成: "done" };
    const status = map[state.jobFilters.range] || undefined;
    try {
      const res = await agent_jobs(
        Number(state.jobFilters.limit) || 50,
        status,
        state.jobFilters.group || undefined
      );
      state.jobs = Array.isArray(res) ? res : ((res && res.jobs) || []);
    } catch (e) {
      toast("任务列表读取失败：" + (e.message || String(e)), "err");
      state.jobs = [];
    }
  }

  async function refreshJobs() {
    await loadJobs();
    rebuild();
  }

  function setAutoRefresh(enabled) {
    state.jobFilters.autoRefresh = enabled;
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
    if (enabled) {
      autoRefreshTimer = setInterval(() => refreshJobs(), 5000);
      trackTimer(autoRefreshTimer);
    }
  }

  function renderStats() {
    const queued = Number(state.stats.queued || 0);
    const running =
      Number(state.stats.running || 0) +
      Number(state.stats.dispatching || 0) +
      Number(state.stats.submitted || 0) +
      Number(state.stats.agent_running || 0);
    const done = Number(state.stats.done || 0) + Number(state.stats.sent || 0);
    const abnormal =
      Number(state.stats.failed || 0) +
      Number(state.stats.timeout || 0) +
      Number(state.stats.expired || 0);
    const workerRunning = Boolean(state.workerStatus && state.workerStatus.running);
    const loopRunning = Boolean(state.loopState && state.loopState.running);

    let alert;
    if (abnormal) {
      alert = h("div", { class: "error-banner", text: "发现异常任务，请优先查看下方任务检查。" });
    } else if (queued && !(workerRunning || loopRunning)) {
      alert = h(
        "div",
        { class: "error-banner" },
        h("span", { text: "有任务在排队，但自动派发未启动。请先启动自动处理。" })
      );
    } else if (loopRunning || workerRunning) {
      alert = h("div", { class: "badge badge-ok", text: "复杂任务处理服务可用，可以开始测试。" });
    } else {
      alert = h("div", { class: "badge badge-info", text: "自动处理未启动。首次检查建议先用“安全测试”。" });
    }

    return h(
      "div",
      { class: "card" },
      h("h3", { class: "card-title", text: "当前状态" }),
      alert,
      h(
        "div",
        { class: "metric-grid" },
        metricCard("排队中", queued),
        metricCard("处理中", running),
        metricCard("已完成", done),
        metricCard("异常", abnormal),
        metricCard("自动处理", loopRunning ? "运行中" : "未启动")
      )
    );
  }

  function renderPoolTable() {
    const rows = (state.poolState.instances || []).map((row) => {
      const cur = row.current_job || {};
      return {
        实例: row.label || row.id,
        Provider: row.provider,
        状态: poolStatusLabel(row.status),
        上岗: row.on_duty ? "是" : "否",
        当前任务: cur.id ? "#" + cur.id + " · " + cur.target_name : "—",
        会话: cur.external_session_id || "—",
      };
    });
    if (!rows.length) return emptyState("暂无池状态数据");
    const caption = h(
      "p",
      { text: "状态说明：空闲=已上岗可接任务；处理中=正在跑任务；离线=不可用；可上岗=健康但尚未加入自动处理池。" }
    );
    const cols = ["实例", "Provider", "状态", "上岗", "当前任务", "会话"].map((k) => ({
      key: k,
      label: k,
    }));
    return h("div", null, caption, tableEl(cols, rows));
  }

  function renderInstanceCard(inst) {
    const iid = String(inst.id || "");
    const label = String(inst.label || inst.profile || iid || "实例");
    const kind = String(inst.provider || inst.type || "hermes").toLowerCase();
    const kindLabel = instanceKindLabel(kind);
    const bridge = inst.bridge_url || inst.bridge_cwd || inst.hermes_home || "-";
    const extras = [];
    if (inst.profile) extras.push("profile=" + inst.profile);
    if (inst.model) extras.push("model=" + inst.model);
    if (inst.toolsets) extras.push("toolsets=" + inst.toolsets);

    const poolById = {};
    (state.poolState.instances || []).forEach((r) => {
      poolById[String(r.id || "")] = r;
    });
    const poolRow = poolById[iid] || {};
    const cur = poolRow.current_job || {};
    const loopRunning = Boolean(state.loopState && state.loopState.running);
    const loopCfg = (state.loopState && state.loopState.config) || {};
    const loopInst = String(loopCfg.instance_id || "");

    const configuredIds = new Set((state.instances || []).map((i) => String(i.id || "")));

    async function joinLoop() {
      try {
        if (!configuredIds.has(iid)) {
          const regRes = await register_agent_instance(inst);
          if (regRes && regRes.ok) {
            state.instances.push(inst);
          }
        }
        const res = await start_async_loop(iid);
        if (res.running) {
          toast("已加入自动处理池", "ok");
          await loadAll();
        } else {
          toast("启动结果：" + (res.action || "未知"), "warn");
        }
      } catch (e) {
        toast("加入失败：" + (e.message || String(e)), "err");
      }
    }

    async function checkInstance() {
      try {
        const resp = await agent_provider_health(kind, iid);
        const hdata = (resp && resp.health) || resp || {};
        state.healthCache[iid] = hdata;
        if (hdata.ok || hdata.ready) {
          toast("可用 · provider=" + (hdata.provider || kind) + " · workers=" + (hdata.workers || 0), "ok");
        } else {
          toast("未在自动池中或暂未检测：" + (hdata.error || "可点击加入自动处理池进行注册和启动"), "warn");
        }
      } catch (e) {
        state.healthCache[iid] = { ok: false, ready: false, error: e.message || String(e) };
        toast("检查失败：" + (e.message || String(e)), "err");
      }
      rebuild();
    }

    async function stopLoop() {
      try {
        const res = await stop_async_loop();
        toast(!res.running ? "已停止" : "已请求停止", res.running ? "warn" : "ok");
        await loadAll();
      } catch (e) {
        toast("停止自动处理失败：" + (e.message || String(e)), "err");
      }
    }

    async function runDispatch() {
      try {
        const res = await run_dispatcher_once(iid, kind);
        toast("派发完成", "ok");
        state.detailCache[iid + "_dispatch"] = res;
        rebuild();
      } catch (e) {
        toast("派发失败：" + (e.message || String(e)), "err");
      }
    }

    async function runReconcile() {
      try {
        const res = await run_reconciler_once();
        toast("轮询完成", "ok");
        state.detailCache[iid + "_reconcile"] = res;
        rebuild();
      } catch (e) {
        toast("轮询失败：" + (e.message || String(e)), "err");
      }
    }

    async function runSend() {
      try {
        const res = await run_sender_once();
        toast("发送完成", "ok");
        state.detailCache[iid + "_send"] = res;
        rebuild();
      } catch (e) {
        toast("发送失败：" + (e.message || String(e)), "err");
      }
    }

    async function startOldWorker(timeoutVal) {
      try {
        if (!configuredIds.has(iid)) {
          const regRes = await register_agent_instance(inst);
          if (regRes && regRes.ok) {
            state.instances.push(inst);
          }
        }
        const res = await start_agent_instance(iid, Number(timeoutVal), 1);
        toast("旧 worker 已启动 · " + (res.action || "started"), "ok");
        await loadAll();
      } catch (e) {
        toast("启动失败：" + (e.message || String(e)), "err");
      }
    }

    async function stopOldWorker() {
      try {
        const res = await stop_agent_worker();
        toast(!res.running ? "已停止" : "已请求停止", res.running ? "warn" : "ok");
        await loadAll();
      } catch (e) {
        toast("停止失败：" + (e.message || String(e)), "err");
      }
    }

    const timeoutInput = h("input", {
      class: "input",
      type: "number",
      value: 240,
      min: 3,
      max: 600,
      step: 1,
    });

    return h(
      "div",
      { class: "card" },
      h("h4", { class: "card-title", text: label }),
      h(
        "div",
        { class: "kv" },
        h("span", { text: "id=" + iid + " · 类型=" + kindLabel + " · 配置=" + bridge }),
        extras.length ? h("span", { text: " · " + extras.join(" · ") }) : null
      ),
      h("div", { class: "metric-grid" }, metricCard("自动池状态", poolStatusLabel(poolRow.status) || "未知")),
      cur.id
        ? h(
            "div",
            { class: "kv" },
            h("span", {
              text: "当前任务：#" + cur.id + " · " + cur.target_name + " · " + cur.status,
            })
          )
        : null,
      loopRunning
        ? h("div", { class: "badge badge-ok", text: "自动处理池运行中" + (loopInst ? " · 在岗=" + loopInst : "") })
        : h("div", { class: "badge badge-muted", text: "自动处理池未启动" }),
      h(
        "div",
        { class: "form-row", style: "margin-top:0.75rem" },
        h("button", { class: "btn btn-primary btn-sm", text: "加入自动处理池", onclick: joinLoop }),
        h("button", { class: "btn btn-ghost btn-sm", text: "检查这个实例", onclick: checkInstance }),
        h("button", { class: "btn btn-danger btn-sm", text: "停止自动处理池", onclick: stopLoop })
      ),
      detailsEl(
        "调试：单步推进队列",
        h(
          "div",
          { class: "form-row" },
          h("button", { class: "btn btn-ghost btn-sm", text: "派发一次", onclick: runDispatch }),
          h("button", { class: "btn btn-ghost btn-sm", text: "轮询一次", onclick: runReconcile }),
          h("button", { class: "btn btn-ghost btn-sm", text: "发送一次", onclick: runSend })
        )
      ),
      detailsEl(
        "旧同步 worker（调试用，不推荐日常使用）",
        h(
          "div",
          null,
          h(
            "div",
            { class: "form-row" },
            h("label", { text: "最长等待（秒）" }),
            timeoutInput
          ),
          h(
            "div",
            { class: "form-row" },
            h("button", {
              class: "btn btn-primary btn-sm",
              text: "启动旧 worker",
              onclick: () => startOldWorker(timeoutInput.value),
            }),
            h("button", { class: "btn btn-danger btn-sm", text: "停止旧 worker", onclick: stopOldWorker })
          )
        )
      ),
      state.detailCache[iid + "_dispatch"]
        ? detailsEl("派发结果 JSON", h("pre", { text: JSON.stringify(state.detailCache[iid + "_dispatch"], null, 2) }), true)
        : null,
      state.detailCache[iid + "_reconcile"]
        ? detailsEl("轮询结果 JSON", h("pre", { text: JSON.stringify(state.detailCache[iid + "_reconcile"], null, 2) }), true)
        : null,
      state.detailCache[iid + "_send"]
        ? detailsEl("发送结果 JSON", h("pre", { text: JSON.stringify(state.detailCache[iid + "_send"], null, 2) }), true)
        : null
    );
  }

  function renderPool() {
    const configuredIds = new Set((state.instances || []).map((i) => String(i.id || "")));
    const available = (state.instances || []).concat(
      (state.discovered || []).filter((p) => !configuredIds.has(String(p.id || "")))
    );

    return h(
      "div",
      { class: "card" },
      h("h3", { class: "card-title", text: "Agent 自动处理池" }),
      h("p", { text: "推荐只用这一块：把可用 Hermes profile/worker 加入自动处理池，系统会自动派任务、轮询结果、发回微信。同一个群仍然串行，多个群可以并发。" }),
      renderPoolTable(),
      !available.length
        ? h("p", { class: "empty-state", text: "未在 wechat_bot_targets.json 的 agent_provider.instances 找到实例配置，也未发现可用 Hermes profile/worker。" })
        : h("div", null, ...available.map((inst) => renderInstanceCard(inst)))
    );
  }

  function renderTestForm() {
    const providerSel = h(
      "select",
      { class: "select" },
      h("option", { value: "echo", text: "安全测试（不调用真实模型）" }),
      h("option", { value: "hermes", text: "Hermes 本地 Agent" })
    );
    const groupInput = h("input", { class: "input", type: "text", value: "manual-test" });
    const senderInput = h("input", { class: "input", type: "text", value: "tester" });
    const targetOptions = [h("option", { value: 0, text: "不绑定目标" })].concat(
      state.targets.map((t, i) =>
        h("option", { value: i + 1, text: (t.name || "?") + "（" + (t.username || "") + "）" })
      )
    );
    const targetSel = h("select", { class: "select" }, ...targetOptions);
    const promptArea = h("textarea", { class: "textarea", rows: 3, text: "请用一句话说明今天上海天气适合穿什么" });

    async function submitTest(e) {
      e.preventDefault();
      const targetIdx = Number(targetSel.value) || 0;
      const target = targetIdx > 0 ? state.targets[targetIdx - 1] : null;
      try {
        const res = await create_agent_test_job(
          promptArea.value.trim(),
          providerSel.value,
          groupInput.value.trim() || "manual-test",
          senderInput.value.trim() || "tester",
          target
        );
        const job = (res && res.job) || {};
        toast("测试任务已创建", "ok");
        const rows = [
          {
            任务编号: job.id,
            当前状态: jobStatusLabel(jobStatusForDisplay(job)),
            测试分组: job.group_key,
            处理方式: providerLabel(job.provider),
          },
        ];
        state.detailCache["test_form_result"] = { rows, raw: res };
        rebuild();
      } catch (e) {
        toast("创建失败：" + (e.message || String(e)), "err");
      }
    }

    const result = state.detailCache["test_form_result"];

    return h(
      "div",
      { class: "card" },
      h("h3", { class: "card-title", text: "快速测试" }),
      h("p", { text: "用一条测试任务确认：创建任务 → 被处理 → 返回结果，这个流程是否正常。" }),
      h(
        "form",
        { class: "form-grid", onsubmit: submitTest },
        h("div", { class: "form-row" }, h("label", { text: "测试内容" }), promptArea),
        h(
          "div",
          { class: "form-row" },
          h("label", { text: "测试模式" }), providerSel,
          h("label", { text: "测试分组" }), groupInput,
          h("label", { text: "操作人" }), senderInput,
          h("label", { text: "绑定目标" }), targetSel
        ),
        h("button", { class: "btn btn-primary", type: "submit", text: "开始测试" })
      ),
      result
        ? h(
            "div",
            null,
            tableEl(
              ["任务编号", "当前状态", "测试分组", "处理方式"].map((k) => ({ key: k, label: k })),
              result.rows
            ),
            detailsEl("查看创建详情（JSON）", h("pre", { text: JSON.stringify(result.raw, null, 2) }))
          )
        : null
    );
  }

  function renderAdvanced() {
    const providerSel = h(
      "select",
      { class: "select" },
      h("option", { value: "echo", text: "安全测试（不调用真实模型）" }),
      h("option", { value: "hermes", text: "Hermes 本地 Agent" })
    );
    const timeoutInput = h("input", { class: "input", type: "number", value: 240, min: 3, max: 600, step: 1 });

    async function runOnce() {
      try {
        const res = await run_agent_worker_once(providerSel.value, Number(timeoutInput.value) || 240);
        if (res.action === "completed") toast("处理完成：已拿到结果。", "ok");
        else if (res.action === "idle") toast("当前没有可处理任务。", "info");
        else toast("处理结果：" + (res.action || "未知"), "warn");
        state.detailCache["advanced_once"] = res;
        rebuild();
      } catch (e) {
        toast("运行失败：" + (e.message || String(e)), "err");
      }
    }

    return h(
      "div",
      { class: "card" },
      detailsEl(
        "高级操作：立即处理一次",
        h(
          "div",
          null,
          h("p", { text: "用于排查“任务已创建但没有自动处理”的情况。普通使用无需操作。只执行一次处理检查，不会持续后台运行。" }),
          h(
            "div",
            { class: "form-row" },
            h("label", { text: "处理模式" }), providerSel,
            h("label", { text: "最长等待时间" }), timeoutInput,
            h("button", { class: "btn btn-primary", text: "立即处理一次", onclick: runOnce })
          ),
          state.detailCache["advanced_once"]
            ? detailsEl("查看处理详情（JSON）", h("pre", { text: JSON.stringify(state.detailCache["advanced_once"], null, 2) }), true)
            : null
        )
      )
    );
  }

  function renderJobTable() {
    let rows = state.jobs;
    const range = state.jobFilters.range;
    if (range === "仅异常") {
      rows = rows.filter((j) => ["failed", "timeout", "expired"].includes(j.status));
    } else if (range === "仅处理中") {
      rows = rows.filter((j) => ["running", "dispatching", "submitted", "agent_running"].includes(j.status));
    } else if (range === "仅已完成") {
      rows = rows.filter((j) => ["done", "sent"].includes(j.status));
    } else if (range === "仅发送失败") {
      rows = rows.filter((j) => j.send_status === "failed");
    }

    if (!rows.length) return emptyState("暂无任务。");

    const tableRows = rows.map((j) => {
      const sendStatus = j.send_status || "pending";
      let resultSummary = jobSummary(j);
      if (sendStatus === "failed" && j.error) {
        resultSummary = "发送失败：" + String(j.error).slice(0, 60);
      }
      const payload = j.payload || {};
      const created = j.created_at || 0;
      const createdStr = created ? fmtTs(created) : "—";
      const nextPoll = j.next_poll_at || 0;
      const nextPollStr = nextPoll ? fmtTs(nextPoll) : "—";
      const externalSession =
        j.external_session_id ||
        (typeof payload === "object" ? payload.agent_instance_id : "") ||
        "—";
      return {
        任务编号: j.id,
        当前状态: jobStatusLabel(jobStatusForDisplay(j)),
        "群/联系人": j.target_name || j.group_key,
        任务内容: String(payload.prompt || payload.clean_text || "").slice(0, 40),
        创建时间: createdStr,
        "Agent会话/实例": String(externalSession).slice(0, 28),
        下次检查: nextPollStr,
        任务内容类型: j.task_type,
        处理方式: providerLabel(j.provider),
        处理节点: j.worker_id || "—",
        发送结果: sendStatusLabel(sendStatus),
        结果摘要: resultSummary,
      };
    });
    const cols = [
      "任务编号",
      "当前状态",
      "群/联系人",
      "任务内容",
      "创建时间",
      "Agent会话/实例",
      "下次检查",
      "任务内容类型",
      "处理方式",
      "处理节点",
      "发送结果",
      "结果摘要",
    ].map((k) => ({ key: k, label: k }));
    return tableEl(cols, tableRows);
  }

  async function dismissJobs(jobs) {
    const ids = jobs.map((j) => Number(j.id)).filter(Boolean);
    if (!ids.length) return;
    try {
      const res = await dismiss_jobs_batch(ids);
      toast("已忽略 " + res.ok_count + "/" + res.total + " 个任务", "ok");
      if (res.failed_ids && res.failed_ids.length) {
        toast("以下任务忽略失败：" + res.failed_ids.join(", "), "err");
      }
      await loadJobs();
      rebuild();
    } catch (e) {
      toast("批量忽略失败：" + (e.message || String(e)), "err");
    }
  }

  async function viewJob(jobId) {
    try {
      const res = await get_agent_job(jobId);
      state.detailCache["job_" + jobId] = (res && res.job) || res;
      rebuild();
    } catch (e) {
      toast("读取失败：" + (e.message || String(e)), "err");
    }
  }

  async function dismissOne(jobId) {
    try {
      const res = await dismiss_job(jobId);
      if (res.action === "dismissed") {
        toast("已忽略", "ok");
        await loadJobs();
        rebuild();
      } else {
        toast("忽略失败：" + (res.error || "未知"), "warn");
      }
    } catch (e) {
      toast("忽略失败：" + (e.message || String(e)), "err");
    }
  }

  async function recoverOne(jobId, send) {
    try {
      const res = await recover_job_result(jobId, send);
      if (res.action === "recovered") {
        const sendResult = res.send_result || {};
        if (!send) {
          toast("已拉取结果", "ok");
        } else if (sendResult.sent) {
          toast("已拉取并发送", "ok");
        } else {
          toast("已拉取结果，但发送失败：" + (sendResult.reason || "未知"), "warn");
        }
        await loadJobs();
        rebuild();
      } else {
        const detail = res.result || {};
        toast("还没找到可发送结果：" + (detail.error || "未知"), "warn");
      }
    } catch (e) {
      toast((send ? "拉取并发送" : "拉取") + "失败：" + (e.message || String(e)), "err");
    }
  }

  async function retrySend(jobId) {
    try {
      const res = await retry_job_send(jobId);
      if (res.action === "sent") {
        toast("发送成功", "ok");
        await loadJobs();
        rebuild();
      } else {
        toast("发送失败：" + ((res.send_result || {}).reason || "未知"), "warn");
      }
    } catch (e) {
      toast("重试失败：" + (e.message || String(e)), "err");
    }
  }

  function renderAbnormalJobs() {
    const abnormal = state.jobs.filter((j) => ["failed", "timeout", "expired"].includes(j.status));
    if (!abnormal.length) return null;

    return h(
      "div",
      { class: "card" },
      h("h4", { class: "card-title", text: "异常任务（可恢复、忽略或补发）" }),
      h("p", { text: '拉取结果会从 agent 历史会话里找回超时后完成的回复；忽略后任务状态会变为"已取消"。' }),
      h(
        "button",
        {
          class: "btn btn-danger btn-sm",
          text: "一键忽略全部异常任务",
          onclick: () => {
            if (confirmDanger("确定要忽略全部异常任务吗？此操作不可恢复。")) {
              dismissJobs(abnormal);
            }
          },
        }
      ),
      ...abnormal.map((j) => {
        const jobId = j.id;
        const title = j.target_name || j.group_key || "未知";
        const payload = j.payload || {};
        return detailsEl(
          "任务 #" + jobId + " - " + title,
          h(
            "div",
            null,
            h("div", { class: "kv" }, h("span", { text: "任务内容：" + String(payload.prompt || "").slice(0, 120) })),
            h("div", { class: "kv" }, h("span", { text: "错误原因：" + (j.error || "未知") })),
            j.result_text ? h("div", { class: "kv" }, h("span", { text: "结果预览：" + j.result_text.slice(0, 120) })) : null,
            payload.agent_raw_output
              ? h("div", { class: "kv" }, h("span", { text: "Agent 原始输出：" + String(payload.agent_raw_output).slice(0, 300) }))
              : null,
            h(
              "div",
              { class: "form-row" },
              h("button", { class: "btn btn-ghost btn-sm", text: "查看完整任务", onclick: () => viewJob(jobId) }),
              h("button", { class: "btn btn-ghost btn-sm", text: "忽略", onclick: () => dismissOne(jobId) }),
              h("button", { class: "btn btn-primary btn-sm", text: "拉取结果", onclick: () => recoverOne(jobId, false) }),
              h("button", { class: "btn btn-primary btn-sm", text: "拉取并发送", onclick: () => recoverOne(jobId, true) }),
              j.result_text
                ? h("button", { class: "btn btn-primary btn-sm", text: "补发已有", onclick: () => retrySend(jobId) })
                : null
            ),
            state.detailCache["job_" + jobId]
              ? detailsEl(
                  "完整任务 JSON",
                  h("pre", { text: JSON.stringify(state.detailCache["job_" + jobId], null, 2) }),
                  true
                )
              : null
          )
        );
      })
    );
  }

  function renderSendableJobs() {
    const sendable = state.jobs.filter(
      (j) => j.result_text && j.send_status !== "sent" && ["done", "failed", "timeout", "expired"].includes(j.status)
    );
    if (!sendable.length) return null;

    return h(
      "div",
      { class: "card" },
      h("h4", { class: "card-title", text: "已有结果待发送" }),
      h("p", { text: "这些任务已经有结果，但还没有成功发回微信。" }),
      h(
        "button",
        {
          class: "btn btn-danger btn-sm",
          text: "一键忽略全部待发送任务",
          onclick: () => {
            if (confirmDanger("确定要忽略全部待发送任务吗？此操作不可恢复。")) {
              dismissJobs(sendable);
            }
          },
        }
      ),
      ...sendable.map((j) => {
        const jobId = j.id;
        const title = j.target_name || j.group_key || "未知";
        const payload = j.payload || {};
        return detailsEl(
          "任务 #" + jobId + " - " + title,
          h(
            "div",
            null,
            h("div", { class: "kv" }, h("span", { text: "任务内容：" + String(payload.prompt || "").slice(0, 120) })),
            h("div", { class: "kv" }, h("span", { text: "结果预览：" + (j.result_text || "").slice(0, 160) })),
            payload.agent_raw_output
              ? h("div", { class: "kv" }, h("span", { text: "Agent 原始输出：" + String(payload.agent_raw_output).slice(0, 300) }))
              : null,
            h(
              "div",
              { class: "form-row" },
              h("button", { class: "btn btn-ghost btn-sm", text: "查看完整任务", onclick: () => viewJob(jobId) }),
              h("button", { class: "btn btn-primary btn-sm", text: "补发已有结果", onclick: () => retrySend(jobId) }),
              h("button", { class: "btn btn-ghost btn-sm", text: "忽略", onclick: () => dismissOne(jobId) })
            ),
            state.detailCache["job_" + jobId]
              ? detailsEl(
                  "完整任务 JSON",
                  h("pre", { text: JSON.stringify(state.detailCache["job_" + jobId], null, 2) }),
                  true
                )
              : null
          )
        );
      })
    );
  }

  function renderJobsSection() {
    const rangeSel = h(
      "select",
      { class: "select", onchange: (e) => { state.jobFilters.range = e.target.value; refreshJobs(); } },
      ["全部任务", "仅排队中", "仅处理中", "仅异常", "仅已完成", "仅发送失败"].map((v) =>
        h("option", { value: v, text: v, selected: state.jobFilters.range === v })
      )
    );
    const limitInput = h("input", {
      class: "input",
      type: "number",
      value: state.jobFilters.limit,
      min: 10,
      max: 200,
      step: 10,
      onchange: (e) => { state.jobFilters.limit = Number(e.target.value) || 50; refreshJobs(); },
    });
    const groupInput = h("input", {
      class: "input",
      type: "text",
      placeholder: "测试分组筛选（可空）",
      value: state.jobFilters.group,
      onchange: (e) => { state.jobFilters.group = e.target.value; refreshJobs(); },
    });
    const refreshCheck = h("input", {
      type: "checkbox",
      checked: state.jobFilters.autoRefresh,
      onchange: (e) => setAutoRefresh(e.target.checked),
    });

    return h(
      "div",
      { class: "card" },
      h("h3", { class: "card-title", text: "任务检查" }),
      h("p", { text: "优先关注异常任务和长时间未完成任务。" }),
      h(
        "div",
        { class: "form-row" },
        h("label", { text: "查看范围" }), rangeSel,
        h("label", { text: "显示数量" }), limitInput,
        h("label", { text: "分组筛选" }), groupInput,
        h("label", { class: "checkbox-row" }, refreshCheck, h("span", { text: "自动刷新（5秒）" }))
      ),
      renderJobTable(),
      renderAbnormalJobs(),
      renderSendableJobs()
    );
  }

  function renderTechDetails() {
    const lastHealth = Object.values(state.healthCache).pop() || {};
    return h(
      "div",
      { class: "card" },
      h("h3", { class: "card-title", text: "技术详情与排查" }),
      detailsEl("查看处理能力完整信息", h("pre", { text: JSON.stringify(lastHealth, null, 2) })),
      detailsEl(
        "查看后台服务原始状态",
        h("pre", {
          text: JSON.stringify({ worker: state.workerStatus, m5_async_loop: state.loopState }, null, 2),
        })
      ),
      detailsEl("查看任务技术详情（JSON）", h("pre", { text: JSON.stringify(state.jobs, null, 2) }))
    );
  }

  function buildContent() {
    if (state.lastError) {
      return errorBanner("页面加载失败：" + state.lastError, () => {
        state.lastError = null;
        loadAll();
      });
    }
    if (!state.stats) {
      return spinner("加载 Agent 任务数据…");
    }
    return h(
      "div",
      null,
      pageHeader("复杂任务处理", "查看服务是否可用、做一次安全测试、检查是否有任务卡住。当前不会自动接入微信群监听，也不会自动发送微信。"),
      renderStats(),
      renderPool(),
      renderTestForm(),
      renderAdvanced(),
      renderJobsSection(),
      renderTechDetails()
    );
  }

  function rebuild() {
    clear(root);
    root.appendChild(buildContent());
  }

  try {
    await loadAll();
  } catch (e) {
    state.lastError = e.message || String(e);
    rebuild();
  }

  return stopTimers;
}
