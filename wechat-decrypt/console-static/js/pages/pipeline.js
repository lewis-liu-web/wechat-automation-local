import { reliable_pipeline_status, reliable_scheduler_status, reliable_scheduler_start, reliable_scheduler_stop, requeue_reliable_outbox, list_targets } from "../api.js";
import { h, clear, tableEl, badge, toast, spinner, metricCard, detailsEl, pageHeader, emptyState, fmtTs } from "../ui.js";

const TABLE_CN = {
  inbound_events: "收到的消息",
  turn_windows: "聚合窗口",
  turns: "对话回合",
  turn_jobs: "想回复任务",
  send_outbox: "发件箱",
};

const STATUS_CN = {
  pending: "待处理",
  open: "进行中",
  closed: "已关闭",
  materialized: "已生成任务",
  claimed: "处理中",
  done: "完成",
  failed: "失败",
  queued: "排队",
  sending: "发送中",
  sent: "已发送",
  confirmed: "已确认发出",
  dead_letter: "失败待处理",
  cancelled: "已取消",
};

export async function render(root) {
  clear(root);
  let aborted = false;
  let selectedOutboxId = null;
  let requeueReason = "";

  function cleanup() {
    aborted = true;
  }

  function showError(msg, retry) {
    if (aborted) return;
    clear(root);
    root.appendChild(h("div", { class: "error-banner" },
      h("span", { text: msg }),
      h("button", { class: "btn btn-sm", text: "重试", onclick: retry })
    ));
  }

  async function load() {
    if (aborted) return;
    clear(root);
    root.appendChild(spinner("加载管线状态…"));

    try {
      const [rp, sch, targets] = await Promise.all([
        reliable_pipeline_status(),
        reliable_scheduler_status(),
        list_targets("all"),
      ]);
      if (aborted) return;
      renderPage(rp, sch, targets);
    } catch (err) {
      showError(err.message || "加载失败", load);
    }
  }

  async function refresh() {
    await load();
    toast("已刷新", "ok");
  }

  async function startScheduler() {
    try {
      const res = await reliable_scheduler_start();
      if (res.ok !== true) throw new Error(res.error || "启动失败");
      toast("处理已开始", "ok");
      await load();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function stopScheduler() {
    try {
      const res = await reliable_scheduler_stop();
      if (res.ok !== true) throw new Error(res.error || "停止失败");
      toast("处理已停止", "ok");
      await load();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  async function doRequeue() {
    const reason = requeueReason.trim();
    if (!selectedOutboxId) {
      toast("请选择发件 ID", "err");
      return;
    }
    if (!reason) {
      toast("请填写再试原因", "err");
      return;
    }
    try {
      const res = await requeue_reliable_outbox(selectedOutboxId, reason);
      if (res.ok !== true) throw new Error(res.error || "再试失败");
      toast("已重新排队", "ok");
      selectedOutboxId = null;
      requeueReason = "";
      await load();
    } catch (err) {
      toast(err.message, "err");
    }
  }

  function renderPage(rp, sch, targets) {
    clear(root);

    const enabled = Boolean(rp.enabled);
    const testTargetOnly = Boolean(rp.test_target_only);
    const dbStatus = rp.db_status || "?";
    const running = Boolean(sch.running || sch.thread_alive);

    // Header + intro + refresh
    root.appendChild(pageHeader("可靠管线（自动回复后台）"));
    root.appendChild(h("div", { class: "form-row" },
      h("button", { class: "btn btn-ghost btn-sm", text: "刷新状态", onclick: refresh })
    ));
    root.appendChild(h("p", { text: "这里管的是现有自动回复怎么可靠跑，不是第二种机器人。流程：收到消息 → 先记到库 → Agent 想回复 → 排队发微信。进程挂了也能从库接着处理；失败的发送会记成「失败待处理」。" }));

    // 4 metrics
    const metrics = h("div", { class: "metric-grid" });
    metrics.appendChild(metricCard("后台总开关", enabled ? "开" : "关"));
    metrics.appendChild(metricCard("发送范围", testTargetOnly ? "仅白名单群" : "全部开启群"));
    metrics.appendChild(metricCard("任务库", dbStatus));
    metrics.appendChild(metricCard("持续处理", running ? "运行中" : "已停止"));
    root.appendChild(metrics);

    // Whitelist notice
    if (testTargetOnly) {
      let names = rp.test_target_names || ["bot群聊测试", "family"];
      if (typeof names === "string") names = [names];
      const namesText = names && names.length ? names.join("、") : "（见 config）";
      root.appendChild(h("div", { class: "card" },
        h("span", { class: "badge badge-info", text: "白名单" }),
        h("span", { text: ` 当前开了发送白名单：只有名单里的群会真发到微信。常见名单：${namesText}。其它群即使开了自动回复，也只跑后台不投递。` })
      ));
    }

    // Backlog table
    root.appendChild(h("h2", { class: "card-title" }, "积压一览"));
    root.appendChild(h("p", { text: "数字变大说明某一步卡住了：消息入库 / 想回复 / 待发送 / 失败待处理。" }));
    const counts = rp.counts || {};
    const rows = [];
    for (const [table, bag] of Object.entries(counts).sort()) {
      const tLabel = TABLE_CN[table] || table;
      if (bag && typeof bag === "object" && !Array.isArray(bag)) {
        for (const [statusName, n] of Object.entries(bag).sort()) {
          rows.push({ tLabel, statusLabel: STATUS_CN[statusName] || statusName, n });
        }
      } else {
        rows.push({ tLabel, statusLabel: "", n: bag });
      }
    }
    if (rows.length) {
      root.appendChild(tableEl(
        [
          { key: "tLabel", label: "环节" },
          { key: "statusLabel", label: "状态" },
          { key: "n", label: "数量", render: (row) => String(row.n) },
        ],
        rows
      ));
    } else {
      root.appendChild(emptyState("暂无计数（任务库不可用或为空）"));
    }

    // Scheduler section
    root.appendChild(h("h2", { class: "card-title" }, "持续处理（调度）"));
    root.appendChild(h("p", { text: "调度开着，后台才会不断「把排队任务交给 Agent」和「把回复发到微信」。关掉后新消息可能只堆在库里。" }));
    const schedulerActions = h("div", { class: "form-row" });
    schedulerActions.appendChild(h("button", { class: "btn btn-primary", text: "开始处理", onclick: startScheduler }));
    schedulerActions.appendChild(h("button", { class: "btn btn-danger", text: "停止处理", onclick: stopScheduler }));
    root.appendChild(schedulerActions);

    const techBody = h("pre", { class: "kv", text: JSON.stringify({
      running: Boolean(sch.running),
      thread_alive: Boolean(sch.thread_alive),
      iterations: sch.iterations || 0,
      interval: sch.interval || "",
      last_error: sch.last_error || "",
      last_worker: sch.last_worker || {},
      last_sender: sch.last_sender || {},
    }, null, 2) });
    root.appendChild(detailsEl("处理循环技术细节", techBody, false));

    // Dead letter table
    root.appendChild(h("h2", { class: "card-title" }, "发送失败记录"));
    root.appendChild(h("p", { text: "多次发失败会记在这里。从未开始发的可以再试一次；已经点过发送的只能当历史看，再试可能把同一条再发到群里。" }));
    const dls = rp.dead_letters || [];
    if (!dls.length) {
      root.appendChild(h("div", { class: "card" },
        badge("无失败记录", "ok")
      ));
    } else {
      const dlRows = dls.map((d) => ({
        id: d.id,
        jobId: d.job_id,
        target: d.target_id,
        attempts: `${d.attempts || 0}/${d.max_attempts || 0}`,
        requeueLabel: d.requeue_safe ? "可以" : "否（防重复发）",
        created: d.created_at ? fmtTs(d.created_at) : "",
        dead: d.dead_at ? fmtTs(d.dead_at) : "",
      }));
      root.appendChild(tableEl(
        [
          { key: "id", label: "发件ID" },
          { key: "jobId", label: "任务ID" },
          { key: "target", label: "目标" },
          { key: "attempts", label: "尝试" },
          { key: "requeueLabel", label: "可否再试" },
          { key: "created", label: "创建" },
          { key: "dead", label: "失败时间" },
        ],
        dlRows
      ));

      const safeIds = dls
        .filter((d) => d.requeue_safe && d.id != null)
        .map((d) => d.id);
      if (!safeIds.length) {
        root.appendChild(h("p", { text: "本页失败记录都已开过发送，禁止再试，避免重复发到微信。" }));
      } else {
        selectedOutboxId = safeIds[0];
        const select = h("select", {
          class: "select",
          onchange: (e) => { selectedOutboxId = e.target.value; },
        });
        for (const id of safeIds) {
          select.appendChild(h("option", { value: String(id), text: String(id), selected: id === safeIds[0] }));
        }
        const reasonInput = h("input", {
          class: "input",
          type: "text",
          placeholder: "例如：发送前临时故障已恢复",
          value: requeueReason,
          oninput: (e) => { requeueReason = e.target.value; },
        });
        const requeueBtn = h("button", {
          class: "btn btn-primary",
          text: "再试一次发送",
          onclick: doRequeue,
        });
        root.appendChild(h("div", { class: "card form-row" },
          h("label", { text: "选择可再试的发件ID" }),
          select,
          h("label", { text: "再试原因（必填，方便事后查）" }),
          reasonInput,
          requeueBtn
        ));
      }
    }

    // Auto-reply enabled groups
    root.appendChild(h("h2", { class: "card-title" }, "已打开自动回复的群"));
    const enabledTargets = (Array.isArray(targets) ? targets : (targets.targets || [])).filter((t) => t.reliable_pipeline_target === true);
    if (!enabledTargets.length) {
      root.appendChild(emptyState("还没有群打开自动回复。到「监听目标」里给某个群打开即可。"));
    } else {
      const arRows = enabledTargets.map((t) => ({
        name: t.name,
        listening: t.enabled ? "是" : "否",
        shadow: t.reliable_pipeline_shadow ? "是" : "否",
        agent: t.dedicated_agent_instance_id || "通用池",
        lastId: t.last_local_id != null ? String(t.last_local_id) : "",
      }));
      root.appendChild(tableEl(
        [
          { key: "name", label: "群名" },
          { key: "listening", label: "监听中" },
          { key: "shadow", label: "只演练" },
          { key: "agent", label: "专用 Agent" },
          { key: "lastId", label: "已读到ID" },
        ],
        arRows
      ));
    }
  }

  await load();
  return cleanup;
}
