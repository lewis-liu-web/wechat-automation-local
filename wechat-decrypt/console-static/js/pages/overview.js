import {
  overview_today,
  overview_history,
  overview_topics,
  refresh_topics,
} from "../api.js";
import {
  h,
  clear,
  tableEl,
  badge,
  toast,
  spinner,
  metricCard,
  pageHeader,
  emptyState,
  fmtTs,
  svgGroupedBars,
} from "../ui.js";

const STATUS_BADGES = {
  ok: ["ok", "已生成"],
  building: ["info", "生成中"],
  empty: ["info", "今日无消息"],
  error: ["err", "生成失败"],
  none: ["muted", "未生成"],
};

function isUnknownTarget(t) {
  return !t.name || t.name === t.target_id;
}

function renderName(t) {
  if (isUnknownTarget(t)) {
    return badge(t.target_id || "未知", "muted");
  }
  return h("span", { text: t.name });
}

function smallSpinner() {
  return h("span", {
    class: "spinner",
    style: "width:14px;height:14px;display:inline-block;vertical-align:middle;margin-left:6px;",
  });
}

export async function render(root) {
  let aborted = false;
  const timers = [];
  let topicPollTimer = null;
  let loadingToday = false;
  let loadingTopics = false;
  let loadingHistory = false;
  let historyDays = 7;
  let todaySection, topicsSection, historySection;

  function cleanup() {
    aborted = true;
    for (const t of timers) clearInterval(t);
    timers.length = 0;
    if (topicPollTimer) {
      clearInterval(topicPollTimer);
      topicPollTimer = null;
    }
  }

  function schedule(fn, interval) {
    const id = setInterval(fn, interval);
    timers.push(id);
    return id;
  }

  function showError(msg, retry) {
    if (aborted) return;
    clear(root);
    root.appendChild(
      h(
        "div",
        { class: "error-banner" },
        h("span", { text: msg }),
        h("button", { class: "btn btn-sm", text: "重试", onclick: retry })
      )
    );
  }

  function renderStructure() {
    clear(root);
    const refreshBtn = h("button", {
      class: "btn btn-ghost btn-sm",
      type: "button",
      text: "刷新",
      onclick: refreshAll,
    });
    root.appendChild(pageHeader("总览", "今日收发、话题与趋势", refreshBtn));

    todaySection = h("section", { id: "overview-today" });
    topicsSection = h("section", { id: "overview-topics" });
    historySection = h("section", { id: "overview-history" });
    root.appendChild(todaySection);
    root.appendChild(topicsSection);
    root.appendChild(historySection);
  }

  async function refreshAll() {
    await Promise.all([refreshToday(), refreshTopics()]);
    await refreshHistory(historyDays);
  }

  async function refreshPeriodic() {
    await Promise.all([refreshToday(), refreshTopics()]);
  }

  async function refreshToday() {
    if (loadingToday || aborted) return;
    loadingToday = true;
    try {
      const today = await overview_today();
      if (aborted) return;
      renderToday(today);
    } catch (err) {
      if (!aborted) toast(err.message || "刷新今日数据失败", "err");
    } finally {
      loadingToday = false;
    }
  }

  async function refreshTopics() {
    if (loadingTopics || aborted) return;
    loadingTopics = true;
    try {
      const topics = await overview_topics();
      if (aborted) return;
      renderTopics(topics);
      manageTopicPolling(topics);
    } catch (err) {
      if (!aborted) toast(err.message || "刷新话题失败", "err");
    } finally {
      loadingTopics = false;
    }
  }

  async function refreshTarget(targetId) {
    try {
      const res = await refresh_topics(targetId);
      if (res.ok !== true) throw new Error(res.error || "刷新失败");
      toast("已开始刷新话题", "ok");
      await refreshTopics();
    } catch (err) {
      toast(err.message || "刷新话题失败", "err");
    }
  }

  function manageTopicPolling(topics) {
    const building = (topics.targets || []).some((t) => t.status === "building");
    if (building) {
      if (!topicPollTimer) {
        topicPollTimer = setInterval(() => refreshTopics(), 5000);
      }
    } else if (topicPollTimer) {
      clearInterval(topicPollTimer);
      topicPollTimer = null;
    }
  }

  function renderToday(today) {
    clear(todaySection);
    todaySection.appendChild(h("h2", { class: "card-title" }, "今日概况"));

    const totals = today.totals || {};
    const metrics = h("div", { class: "metric-grid" });
    metrics.appendChild(metricCard("今日收到", totals.received || 0));
    metrics.appendChild(metricCard("今日回复", totals.replied || 0));
    metrics.appendChild(
      metricCard("今日失败", totals.failed || 0, "升级 " + (totals.escalated || 0))
    );
    metrics.appendChild(metricCard("今日死信", totals.dead || 0));
    todaySection.appendChild(metrics);

    todaySection.appendChild(
      h("h2", { class: "section-title" }, "今日各群")
    );
    const targets = today.targets || [];
    if (!targets.length) {
      todaySection.appendChild(emptyState("今日没有数据"));
      return;
    }
    const rows = targets.map((t) => ({
      name: t,
      received: t.received || 0,
      replied: t.replied || 0,
      failed: t.failed || 0,
      escalated: t.escalated || 0,
      dead: t.dead || 0,
    }));
    todaySection.appendChild(
      tableEl(
        [
          { key: "name", label: "群名", render: (row) => renderName(row.name) },
          { key: "received", label: "收到" },
          { key: "replied", label: "回复" },
          { key: "failed", label: "失败" },
          { key: "escalated", label: "升级" },
          { key: "dead", label: "死信" },
        ],
        rows
      )
    );
  }

  function renderTopics(topics) {
    clear(topicsSection);
    topicsSection.appendChild(
      h("h2", { class: "card-title" }, "今日主要话题")
    );
    const targets = topics.targets || [];
    if (!targets.length) {
      topicsSection.appendChild(emptyState("没有启用的监听目标"));
      return;
    }
    const grid = h("div", { class: "grid-2" });
    for (const t of targets) {
      grid.appendChild(renderTopicCard(t));
    }
    topicsSection.appendChild(grid);
  }

  function renderTopicCard(t) {
    const card = h("div", { class: "card topic-card" });
    const header = h("div", {
      class: "form-row",
      style: "justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;",
    });
    const title = h("h3", {
      class: "card-title",
      style: "margin:0;font-size:1rem;",
      text: t.name || t.target_id || "未知目标",
    });
    const statusWrap = h("span", {
      style: "display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;",
    });
    statusWrap.appendChild(renderTopicStatus(t));
    if (t.stale) {
      statusWrap.appendChild(
        h("span", {
          class: "caption", style: "margin:0;font-size:12px;",
          text: "上次生成 " + fmtTs(t.generated_at) + "，正在更新…",
        })
      );
    }
    const refreshBtn = h("button", {
      class: "btn btn-ghost btn-sm",
      text: "刷新话题",
      onclick: () => {
        refreshBtn.disabled = true;
        refreshTarget(t.target_id).finally(() => {
          refreshBtn.disabled = false;
        });
      },
    });
    header.appendChild(title);
    header.appendChild(statusWrap);
    header.appendChild(refreshBtn);
    card.appendChild(header);

    const body = h("div", { style: "margin-top:0.75rem;" });
    if (t.status === "building") {
      body.appendChild(spinner("正在生成话题…"));
    } else if (t.status === "error") {
      body.appendChild(
        h(
          "div",
          { class: "error-banner", text: t.error || "生成失败" }
        )
      );
      body.appendChild(
        h("button", {
          class: "btn btn-sm",
          text: "重试",
          onclick: () => refreshTarget(t.target_id),
        })
      );
    } else if (t.status === "empty" || t.status === "none") {
      body.appendChild(emptyState("今日无消息"));
    } else {
      const topics = t.topics || [];
      if (!topics.length) {
        body.appendChild(emptyState("暂无话题"));
      } else {
        const list = h("div", { style: "display:flex;flex-direction:column;gap:0.75rem;" });
        for (const topic of topics) {
          list.appendChild(renderTopicItem(topic));
        }
        body.appendChild(list);
      }
    }
    card.appendChild(body);
    return card;
  }

  function renderTopicStatus(t) {
    if (t.status === "building") {
      return h(
        "span",
        { style: "display:inline-flex;align-items:center;" },
        badge("生成中", "info"),
        smallSpinner()
      );
    }
    if (t.status === "ok") {
      return badge("已生成 " + fmtTs(t.generated_at), "ok");
    }
    const [kind, text] = STATUS_BADGES[t.status] || STATUS_BADGES.none;
    return badge(text, kind);
  }

  function renderTopicItem(topic) {
    const item = h("div", { class: "topic-card" });
    item.appendChild(h("strong", { text: topic.title || "未命名话题" }));
    if (topic.summary) {
      item.appendChild(
        h("p", {
          class: "caption", style: "margin:0;",
          text: topic.summary,
        })
      );
    }
    const keywords = topic.keywords || [];
    if (keywords.length) {
      const chips = h("div", { class: "chips" });
      for (const kw of keywords) {
        chips.appendChild(h("span", { class: "chip topic-kw", text: kw }));
      }
      item.appendChild(chips);
    }
    return item;
  }

  async function loadHistory(days) {
    historyDays = days;
    if (loadingHistory || aborted) return;
    loadingHistory = true;
    try {
      const history = await overview_history(days);
      if (aborted) return;
      renderHistory(history);
    } catch (err) {
      if (!aborted) {
        clear(historySection);
        historySection.appendChild(
          h(
            "div",
            { class: "error-banner" },
            h("span", { text: err.message || "加载趋势失败" }),
            h("button", {
              class: "btn btn-sm",
              text: "重试",
              onclick: () => loadHistory(days),
            })
          )
        );
      }
    } finally {
      loadingHistory = false;
    }
  }

  function renderHistory(history) {
    clear(historySection);
    historySection.appendChild(h("h2", { class: "card-title" }, "统计趋势"));

    const tabs = h("div", { class: "tabs" });
    tabs.appendChild(
      h(
        "button",
        {
          class: "tab" + (historyDays === 7 ? " active" : ""),
          text: "本周",
          onclick: () => {
            if (historyDays !== 7) loadHistory(7);
          },
        }
      )
    );
    tabs.appendChild(
      h(
        "button",
        {
          class: "tab" + (historyDays === 30 ? " active" : ""),
          text: "当月",
          onclick: () => {
            if (historyDays !== 30) loadHistory(30);
          },
        }
      )
    );
    historySection.appendChild(tabs);

    const chartWrap = h("div", { class: "card chart-card" });
    const series = history.series || [];
    const labels = series.map((s) => s.date.slice(5));
    chartWrap.appendChild(
      svgGroupedBars({
        labels,
        series: [
          {
            name: "收到",
            color: "#9ca3af",
            values: series.map((s) => s.received || 0),
          },
          {
            name: "回复",
            color: "#07C160",
            values: series.map((s) => s.replied || 0),
          },
          {
            name: "失败",
            color: "#ef4444",
            values: series.map((s) => s.failed || 0),
          },
        ],
        height: 220,
      })
    );
    historySection.appendChild(chartWrap);

    historySection.appendChild(
      h("h3", { class: "section-title" }, "按群汇总")
    );
    const perTarget = history.per_target || [];
    if (!perTarget.length) {
      historySection.appendChild(emptyState("没有汇总数据"));
      return;
    }
    const rows = perTarget.map((t) => ({
      name: t,
      received: t.received || 0,
      replied: t.replied || 0,
      failed: t.failed || 0,
      dead: t.dead || 0,
    }));
    historySection.appendChild(
      tableEl(
        [
          { key: "name", label: "群", render: (row) => renderName(row.name) },
          { key: "received", label: "收到" },
          { key: "replied", label: "回复" },
          { key: "failed", label: "失败" },
          { key: "dead", label: "死信" },
        ],
        rows
      )
    );
  }

  async function load() {
    if (aborted) return;
    clear(root);
    root.appendChild(spinner("加载总览…"));
    try {
      const [today, topics] = await Promise.all([
        overview_today(),
        overview_topics(),
      ]);
      if (aborted) return;
      renderStructure();
      renderToday(today);
      renderTopics(topics);
      manageTopicPolling(topics);
      await loadHistory(7);
      schedule(() => refreshPeriodic(), 30000);
    } catch (err) {
      showError(err.message || "加载失败", load);
    }
  }

  await load();
  return cleanup;
}
