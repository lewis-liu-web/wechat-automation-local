#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Streamlit V1 微信机器人控制台。

V1 只与本地 control_api (18590) 通信 — 不直接 import 监听或任何 agent 客户端。
换 agent 不影响 UI。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_UI_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _UI_DIR.parent
sys.path.insert(0, str(_PROJECT_DIR))
sys.path.insert(0, str(_UI_DIR))

import streamlit as st  # noqa: E402
from streamlit import config as _st_config  # noqa: E402
from target_registry import get_kb_info as reg_get_kb_info  # noqa: E402

from api import (  # noqa: E402
    ControlAPIError,
    DEFAULT_BASE_URL,
    add_triggers,
    agent_jobs,
    agent_jobs_stats,
    agent_pool_status,
    async_loop_status,
    agent_worker_status,
    build_leann_kb,
    leann_build_status,
    leann_index_info,
    list_leann_indexes,
    agent_provider_health,
    get_agent_job,
    diagnose_kb,
    run_dispatcher_once,
    run_reconciler_once,
    run_sender_once,
    set_target_mode,
    discover_hermes_profiles,
    list_agent_instances,
    register_agent_instance,
    start_agent_instance,
    clear_triggers,
    create_agent_test_job,
    delete_kb,
    delete_target,
    disable_kb,
    disable_target,
    dismiss_job,
    dismiss_jobs_batch,
    enable_kb,
    enable_target,
    events_recent,
    events_stats,
    get_triggers,
    get_default_triggers,
    health,
    import_kb,
    list_kbs,
    list_targets,
    open_kb,
    open_kb_obsidian,
    replace_default_triggers,
    replace_target_kbs,
    replace_triggers,
    remove_triggers,
    retry_job_send,
    recover_job_result,
    save_kb,
    scan_targets,
    search_kb,
    set_target_category,
    set_target_field,
    set_target_dedicated_agent,
    start_agent_worker,
    start_async_loop,
    start_monitor,
    status,
    stop_agent_worker,
    stop_async_loop,
    stop_monitor,
    restart_monitor,
)

# --- 关闭所有 Streamlit 自带 UI 噪声（Deploy / 菜单 / GitHub / 工具栏） ---
_st_config.set_option("client.toolbarMode", "viewer")
_st_config.set_option("ui.hideTopBar", False)
_st_config.set_option("theme.base", "dark")
_st_config.set_option("global.developmentMode", False)
# 最直接的办法：把 page 链接、footer、deploy 全部不渲染。
# Streamlit 没有官方"去掉 Deploy"开关，只能用 CSS 隐藏。
_NOISE_CSS = """
<style>
#MainMenu { visibility: hidden !important; }
header [data-testid="stToolbar"] { visibility: hidden !important; }
header [data-testid="stDecoration"] { visibility: hidden !important; }
[data-testid="stStatusWidget"] { visibility: hidden !important; }
[data-testid="manage-app-button"] { display: none !important; }
[data-testid="stToolbarActions"] { display: none !important; }
footer { visibility: hidden !important; }
.viewerBadge_link__qRIco { display: none !important; }
.viewerBadge_container__rCe29 { display: none !important; }
.stDeployButton { display: none !important; }
</style>
"""

st.set_page_config(
    page_title="微信机器人控制台",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items=None,
)
st.markdown(_NOISE_CSS, unsafe_allow_html=True)


def _kv(label: str, value) -> str:
    return "%s：%s" % (label, value)


EVENT_KIND_LABELS = {
    "trigger_matched": "触发词命中",
    "decision": "回复决策",
    "send_ok": "发送成功",
    "send_fail": "发送失败",
    "target_delete": "删除监听目标",
    "target_enable": "启用监听目标",
    "target_disable": "停用监听目标",
    "trigger_add": "添加触发词",
    "trigger_remove": "删除触发词",
    "trigger_replace": "替换触发词",
    "trigger_clear": "清空触发词",
}

REASON_LABELS = {
    "trigger": "命中触发词",
    "trigger_matched": "命中触发词",
    "session_active": "连续会话中",
    "followup": "追问/上下文延续",
    "risk_gate": "风险拦截",
    "pre_boundary_high_risk": "高风险内容拦截",
    "pre_boundary_promise": "承诺/授权类内容拦截",
    "raw_agent_provider": "Agent 原始模式回复",
    "raw_agent_empty_or_unavailable": "Agent 未返回可发送内容",
    "knowledge_grounded": "知识库命中",
    "smalltalk": "普通闲聊",
}

KB_TYPE_LABELS = {
    "local": "本地文件夹",
    "getnote": "Get笔记在线知识库",
    "ima": "IMA 在线知识库",
    "hook": "外部适配器",
    "leann": "LEANN 向量索引",
}

KB_SOURCE_LABELS = {
    "obsidian": "Obsidian 本地知识库",
    "local_folder": "本地文件夹知识库",
    "getnote": "Get笔记 在线知识库",
    "ima": "IMA 在线知识库",
    "hook": "外部适配器知识库",
    "leann": "LEANN 向量索引",
}

CATEGORY_LABELS = {
    "user": "普通",
    "admin": "管理员",
}


def _label(mapping, value):
    value = str(value or "").strip()
    if not value:
        return "—"
    return mapping.get(value, value)


def _failure_bucket(err_str):
    """Translate a markitdown failure message into a category + targeted advice."""
    e = (err_str or "").lower()
    # CLI ran but exited non-zero with markitdown internal exception = file
    # format is not what markitdown can convert.
    if "cli_exit=1" in e and (
        "markitdown._except" in e
        or "unsupportedformatexception" in e
        or "no converter" in e
        or "unknown format" in e
        or "not supported" in e
    ):
        return {
            "title": "格式不支持",
            "advice": "MarkItDown 处理 .pdf/.docx/.pptx/.xlsx/.md/.txt/图片/HTML 等。换个文档或把内容另存为通用格式再导。",
        }
    # Only flag "tool not installed" if BOTH Python and CLI fail to find
    # markitdown. If CLI succeeded, the tool exists and the error is
    # something else; the Python ModuleNotFoundError just means
    # control_api's own process didn't import it.
    if "modulenotfounderror" in e and "markitdown" in e and "cli_exit=1" in e:
        return {
            "title": "MarkItDown 没装上",
            "advice": "重启 control_api 让它加载 D:\\programs\\wechat-kb-tools。",
        }
    if "cli_exit=0" in e and ("stdout_head=''" in e or "stdout_head=\"\"\"" in e):
        return {
            "title": "文件没正文",
            "advice": "MarkItDown 跑了但提不到字。可能是扫描件 / 加密 / 损坏，OCR 转一次或换文件。",
        }
    if "pdf" in e and ("encrypted" in e or "password" in e):
        return {"title": "PDF 加密", "advice": "用浏览器或 Adobe 解密后重导。"}
    if "no text" in e or "empty" in e or "no text extracted" in e or "tesseract" in e or "ocr" in e:
        return {"title": "PDF 没文字（扫描件/图片型）", "advice": "OCR 转一次再导，或换 .docx/.txt/.md。"}
    if "notimplementederror" in e or "unsupported" in e or "missingdependency" in e:
        return {"title": "缺少解析器", "advice": "在 D:\\programs\\wechat-kb-tools 重装 `pip install markitdown[all]`。"}
    if "filenotfounderror" in e or "no such file" in e:
        return {"title": "源文件已找不到", "advice": "重新选择文件再导。"}
    if "permission" in e or "access" in e:
        return {"title": "权限不足", "advice": "换个 control_api 可读的目录。"}
    return {"title": "其它原因", "advice": "可先在 D:\\programs\\wechat-kb-tools 用 markitdown 单独跑一次确认。"}


def _group_failures(failed_items):
    """Group failure items by bucket id, preserving first-seen order."""
    grouped = {}
    order = []
    for item in failed_items or []:
        bucket = _failure_bucket(item.get("error") or "")
        bid = bucket.get("title")
        if bid not in grouped:
            grouped[bid] = (bucket, [])
            order.append(bid)
        grouped[bid][1].append(item)
    return [grouped[k] for k in order]


def _failure_basename(err_str):
    """Pull the basename of the failed file out of the error string."""
    import re as _re
    m = _re.search(r"但转换失败:\s*([^\[]+?)\s*\[", err_str or "")
    if m:
        path = m.group(1).strip()
        if "\\" in path:
            return path.rsplit("\\", 1)[-1]
        if "/" in path:
            return path.rsplit("/", 1)[-1]
        return path
    return ""


def _failure_short_reason(err_str):
    """Single-line human-readable reason; falls back to python error if no CLI stderr."""
    import re as _re
    m = _re.search(r"stderr_tail='([^']*)'", err_str or "")
    if m and m.group(1).strip():
        line = m.group(1).strip().splitlines()[0]
        # Drop verbose class prefixes like "markitdown._exceptions.X:"
        if ":" in line and "Exception" in line.split(":", 1)[0]:
            line = line.split(":", 1)[1].strip()
        return line[:160]
    m = _re.search(r"python_markitdown_error:\s*([^|]+?)(?:\s*\||$)", err_str or "")
    if m:
        return m.group(1).strip()[:160]
    return ""


def _load_data():
    """一次性加载所有页面共享的数据到 session_state，避免每次切换页面重复请求。"""
    base = st.session_state.get("base_url", DEFAULT_BASE_URL)
    if "data_loaded" in st.session_state:
        return

    try:
        st.session_state.health_result = health(base)
    except ControlAPIError:
        if "health_result" not in st.session_state:
            st.session_state.health_result = None
    try:
        st.session_state.status_result = status(base)
    except ControlAPIError:
        if "status_result" not in st.session_state:
            st.session_state.status_result = None
    try:
        st.session_state.events_stats_result = events_stats(base_url=base)
    except ControlAPIError:
        if "events_stats_result" not in st.session_state:
            st.session_state.events_stats_result = None
    try:
        st.session_state.events_recent_result = events_recent(limit=50, base_url=base)
    except ControlAPIError:
        if "events_recent_result" not in st.session_state:
            st.session_state.events_recent_result = None
    try:
        st.session_state.targets_all = list_targets(kind="all", base_url=base)
    except ControlAPIError:
        if "targets_all" not in st.session_state:
            st.session_state.targets_all = []
    try:
        st.session_state.kbs_result = list_kbs(base_url=base)
    except ControlAPIError:
        if "kbs_result" not in st.session_state:
            st.session_state.kbs_result = []
    try:
        st.session_state.default_triggers_result = get_default_triggers(base_url=base)
    except ControlAPIError:
        if "default_triggers_result" not in st.session_state:
            st.session_state.default_triggers_result = []
    st.session_state.data_loaded = True


def _clear_data_cache() -> None:
    """Remove the shared data-loaded flag so the next page render refetches."""
    st.session_state.pop("data_loaded", None)


def _poll_monitor_status(base: str, expected_running: bool, max_wait: float = 12.0, interval: float = 0.5) -> Dict[str, Any]:
    """Poll /status until monitor_running matches expected_running or timeout."""
    import time as _time
    deadline = _time.time() + max_wait
    last: Dict[str, Any] = {}
    while _time.time() < deadline:
        try:
            last = status(base_url=base)
        except ControlAPIError:
            last = {}
        if bool(last.get("monitor_running")) == expected_running:
            break
        _time.sleep(interval)
    return last

def _sidebar():
    with st.sidebar:
        st.markdown("### 🤖 微信机器人控制台")
        st.caption("V1 · 通过本地 control_api 通信")
        st.divider()
        st.text_input(
            "control_api 地址",
            value=st.session_state.get("base_url", DEFAULT_BASE_URL),
            key="base_url",
        )
        health_result = st.session_state.get("health_result")
        if health_result is not None:
            st.success("control_api 已就绪", icon="✅")
        else:
            st.error("control_api 不可达", icon="🛑")
            st.stop()
        status_result = st.session_state.get("status_result")
        if status_result:
            base = st.session_state.get("base_url", DEFAULT_BASE_URL)
            running = bool(status_result.get("monitor_running"))
            pids = status_result.get("monitor_pids") or []
            total = status_result.get("event_total")
            st.markdown("**监听状态**")
            if running:
                st.success("运行中 · pid=%s" % ",".join(str(p) for p in pids))
            else:
                st.warning("已停止")
            mon_bc1, mon_bc2 = st.columns(2)
            if running:
                if mon_bc1.button("停止", key="sidebar_monitor_stop", use_container_width=True):
                    try:
                        res = stop_monitor(base_url=base)
                        if res.get("ok"):
                            st.toast("已请求停止监听")
                            st.session_state.status_result = _poll_monitor_status(base, expected_running=False)
                        else:
                            st.error("停止失败：%s" % (res.get("error") or "未知"))
                        _clear_data_cache()
                        st.rerun()
                    except ControlAPIError as e:
                        st.error("停止失败：%s" % (e,))
                if mon_bc2.button("重启", key="sidebar_monitor_restart", use_container_width=True):
                    try:
                        res = restart_monitor(base_url=base)
                        if res.get("ok"):
                            st.toast("已请求重启监听")
                            st.session_state.status_result = _poll_monitor_status(base, expected_running=True)
                        else:
                            st.error("重启失败：%s" % (res.get("error") or "未知"))
                        _clear_data_cache()
                        st.rerun()
                    except ControlAPIError as e:
                        st.error("重启失败：%s" % (e,))
            else:
                if mon_bc1.button("启动", key="sidebar_monitor_start", use_container_width=True):
                    try:
                        res = start_monitor(base_url=base)
                        if res.get("ok"):
                            st.toast("已请求启动监听")
                            st.session_state.status_result = _poll_monitor_status(base, expected_running=True)
                        else:
                            st.error("启动失败：%s" % (res.get("error") or "未知"))
                        _clear_data_cache()
                        st.rerun()
                    except ControlAPIError as e:
                        st.error("启动失败：%s" % (e,))
                mon_bc2.empty()
            st.markdown("**事件总数**")
            st.metric("event_log", total or 0)
            st.markdown("**已启用目标**")
            enabled_n = sum(1 for t in (st.session_state.get("targets_all") or []) if t.get("enabled"))
            if enabled_n:
                st.success("%d 个" % enabled_n)
            else:
                st.warning("0 个 — monitor 运行中但没有任何目标被监听")
        else:
            st.warning("状态读取失败")
        if st.button("🔄 刷新数据", use_container_width=True):
            _clear_data_cache()
            st.rerun()
        st.caption("agent-agnostic：UI 不依赖任何具体 agent 客户端。")


def _kv_table(rows, headers):
    """Render a list of (key, value) tuples as a clean two-column table."""
    return st.table([{headers[0]: k, headers[1]: v} for k, v in rows])


def _page_overview():
    st.header("总览")
    base = st.session_state.base_url
    stats = st.session_state.get("events_stats_result")
    if stats is None:
        st.error("统计加载失败")
        return
    by_kind = stats.get("by_kind") or {}
    by_reason = stats.get("by_reason") or {}
    by_target = stats.get("by_target") or {}

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("事件总数", stats.get("total", 0))
    c2.metric("触发命中", int(by_kind.get("trigger_matched", 0)))
    c3.metric("决策次数", int(by_kind.get("decision", 0)))
    c4.metric("发送成功", int(by_kind.get("send_ok", 0)))

    st.subheader("按事件类型")
    if by_kind:
        st.table([{"类型": _label(EVENT_KIND_LABELS, k), "次数": v} for k, v in sorted(by_kind.items(), key=lambda x: -x[1])])
    else:
        st.caption("暂无事件。")

    st.subheader("按决策原因")
    cleaned = {k: v for k, v in by_reason.items() if k and k != "(none)"}
    if cleaned:
        st.table([{"原因": _label(REASON_LABELS, k), "次数": v} for k, v in sorted(cleaned.items(), key=lambda x: -x[1])])
    else:
        st.caption("暂无决策原因。")

    st.subheader("按目标群")
    if by_target:
        st.table([{"目标": k, "事件数": v} for k, v in sorted(by_target.items(), key=lambda x: -x[1])])
    else:
        st.caption("暂无目标数据。")

    st.subheader("最近事件")
    since_label = st.selectbox("时间范围", ["全部", "最近 1 小时", "最近 24 小时", "最近 7 天"], key="overview_since")
    kind_filter = st.selectbox("事件类型", ["全部"] + sorted(EVENT_KIND_LABELS.keys()), format_func=lambda v: "全部" if v == "全部" else _label(EVENT_KIND_LABELS, v), key="overview_kind")
    all_targets = st.session_state.get("targets_all") or []
    target_options = ["全部"] + sorted({str(t.get("name") or t.get("username") or "") for t in all_targets if t})
    target_filter = st.selectbox("目标", target_options, key="overview_target")
    limit = st.number_input("显示数量", min_value=50, max_value=500, value=100, step=50, key="overview_limit")

    since_epoch = None
    if since_label == "最近 1 小时":
        since_epoch = time.time() - 3600
    elif since_label == "最近 24 小时":
        since_epoch = time.time() - 86400
    elif since_label == "最近 7 天":
        since_epoch = time.time() - 7 * 86400

    try:
        filtered_stats = events_stats(since=since_epoch, base_url=base) if since_epoch else stats
        filtered_events = events_recent(
            limit=int(limit),
            kind=None if kind_filter == "全部" else kind_filter,
            target=None if target_filter == "全部" else target_filter,
            base_url=base,
        )
    except ControlAPIError as e:
        st.error("事件读取失败：%s" % e)
        filtered_stats = stats
        filtered_events = st.session_state.get("events_recent_result") or []

    by_kind = (filtered_stats or {}).get("by_kind") or by_kind
    by_reason = (filtered_stats or {}).get("by_reason") or by_reason
    by_target = (filtered_stats or {}).get("by_target") or by_target
    events = filtered_events

    if not events:
        st.caption("暂无事件。")
        return
    rows = []
    for idx, e in enumerate(events):
        payload = e.get("payload") or {}
        rows.append({
            "时间": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("ts") or 0)),
            "类型": _label(EVENT_KIND_LABELS, e.get("kind")),
            "目标": e.get("target") or "",
            "原因": _label(REASON_LABELS, payload.get("reason")),
            "发送人": e.get("sender") or "",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.markdown("**事件详情**")
    for idx, e in enumerate(events):
        with st.expander("事件详情 #%s" % e.get("id", idx), expanded=False):
            st.json(e)


def _page_targets():
    st.header("监听目标")
    base = st.session_state.base_url
    all_targets = st.session_state.get("targets_all") or []
    all_kbs = st.session_state.get("kbs_result") or []
    kb_id_to_source = {
        kb.get("id"): str(
            kb.get("source")
            or ("local_folder" if (kb.get("type") or "local") == "local" else kb.get("type"))
            or "local_folder"
        ).lower()
        for kb in all_kbs
        if kb.get("id")
    }
    kind_map = {"已启用": "enabled", "待启用": "pending", "已停用": "disabled", "全部": "all"}
    selected_kind = st.session_state.get("targets_kind", "已启用")

    def _filter_targets(targets, kind):
        if kind == "all":
            return targets
        result = []
        for t in targets:
            enabled = t.get("enabled")
            is_candidate = t.get("is_candidate") or t.get("status") == "pending"
            if kind == "enabled" and enabled:
                result.append(t)
            elif kind == "pending" and is_candidate:
                result.append(t)
            elif kind == "disabled" and not enabled and not is_candidate:
                result.append(t)
        return result

    col1, col2, col3, col4, col5, col6 = st.columns([2, 1, 1, 1, 2, 1])
    with col1:
        kind = st.selectbox(
            "筛选",
            list(kind_map.keys()),
            index=list(kind_map.keys()).index(selected_kind),
            key="targets_kind",
        )
    with col2:
        st.write("")
        if st.button("刷新列表", use_container_width=True):
            _clear_data_cache()
            st.rerun()
    with col3:
        st.write("")
        include_contacts = st.checkbox("包含私聊", value=False, key="targets_include_contacts")
        st.caption("仅影响扫描新目标")
    with col4:
        st.write("")
        if st.button("扫描新目标", use_container_width=True):
            try:
                with st.spinner("扫描中..."):
                    res = scan_targets(include_contacts=include_contacts, base_url=st.session_state.base_url)
                added = int(res.get("added") or 0)
                updated = int(res.get("updated") or 0)
                discovered = int(res.get("discovered") or 0)
                st.success("扫描完成：发现 %d 个，更新 %d 个，新增 %d 个" % (discovered, updated, added))
                _clear_data_cache()
                st.rerun()
            except ControlAPIError as e:
                st.error("扫描失败：%s" % (e,))
    with col5:
        st.text_input(
            "搜索 (name / username)",
            value="",
            key="targets_search",
            placeholder="输入关键字过滤",
        )
    with col6:
        st.write("")
        st.toggle("仅看私聊", value=False, key="targets_private_only")

    targets = _filter_targets(all_targets, kind_map[kind])
    private_only = bool(st.session_state.get("targets_private_only", False))
    if private_only:
        targets = [t for t in targets if (t.get("type") or "") != "group"]
    search_q = (st.session_state.get("targets_search") or "").strip().lower()
    if search_q:
        targets = [
            t for t in targets
            if search_q in (t.get("name") or "").lower()
            or search_q in (t.get("username") or "").lower()
        ]
    if not targets:
        st.caption("没有匹配的目标。")
        return

    for idx, t in enumerate(targets):
        action_key = t.get("username") or t.get("name") or ""
        widget_key = (t.get("username") or t.get("name") or "target_%d" % idx)
        status_label = "已启用" if t.get("enabled") else ("待启用" if t.get("is_candidate") or t.get("status") == "pending" else "已停用")
        cat = (t.get("category") or "user").strip().lower()
        cat_label = _label(CATEGORY_LABELS, cat)
        is_admin = (cat == "admin")
        with st.container(border=True):
            main_cols = st.columns([5, 1])
            with main_cols[0]:
                title_line = "**%s**  %s" % (t.get("name") or "(no name)", status_label)
                if is_admin:
                    title_line += "  · 🛡 **管理员**"
                st.markdown(title_line)
                st.caption("标识：%s" % (t.get("username") or ""))
                st.caption("模式：%s · 策略：%s · 类别：%s · 触发词 %d · 知识库 %d" % (
                    t.get("mode") or "?",
                    t.get("reply_policy") or "?",
                    cat_label,
                    len(t.get("triggers") or []),
                    len(t.get("knowledge_bases") or []),
                ))
                bound_kb_ids = [x for x in (t.get("knowledge_bases") or []) if x in kb_id_to_source]
                if not bound_kb_ids:
                    st.caption("当前 KB 来源：未绑定")
                else:
                    bound_sources = {kb_id_to_source[x] for x in bound_kb_ids}
                    if len(bound_sources) == 1:
                        st.caption("当前 KB 来源：%s（%d 个）" % (
                            _label(KB_SOURCE_LABELS, next(iter(bound_sources))),
                            len(bound_kb_ids),
                        ))
                    else:
                        st.caption("当前 KB 来源：混合（%s）" % ", ".join(
                            _label(KB_SOURCE_LABELS, s) for s in sorted(bound_sources)
                        ))
                mc = st.columns(4)
                mc[0].metric("已读到ID", t.get("last_local_id") or 0)
                mc[1].metric("触发词", len(t.get("triggers") or []))
                mc[2].metric("知识库", len(t.get("knowledge_bases") or []))
                mc[3].metric("状态", status_label)
                # Response-mode / session-policy editor; only meaningful once the target is enabled.
                if t.get("enabled"):
                    with st.expander("响应模式 / 会话策略", expanded=False):
                        mode_options = [("平衡", "group_assistant"), ("客服", "customer_service")]
                        valid_modes = {v for _, v in mode_options}
                        current_mode = t.get("mode") or "group_assistant"
                        if current_mode not in valid_modes:
                            current_mode = "group_assistant"
                        selected_label = st.selectbox(
                            "响应模式",
                            options=[label for label, _ in mode_options],
                            index=[v for _, v in mode_options].index(current_mode),
                            key=f"target_mode_{widget_key}",
                        )
                        selected_mode = dict(mode_options)[selected_label]
                        if st.button("保存响应模式", key="save_mode_%s" % widget_key, use_container_width=False):
                            try:
                                set_target_mode(action_key, selected_mode, base_url=base)
                                st.success("已切换响应模式为：%s" % selected_label)
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
                        st.divider()
                        response_mode_options = [("触发词回复", "trigger"), ("自由回复（所有消息都回）", "free")]
                        valid_response_modes = {v for _, v in response_mode_options}
                        current_response_mode = t.get("response_mode") or "trigger"
                        if current_response_mode not in valid_response_modes:
                            current_response_mode = "trigger"
                        selected_response_label = st.selectbox(
                            "回复策略",
                            options=[label for label, _ in response_mode_options],
                            index=[v for _, v in response_mode_options].index(current_response_mode),
                            key=f"target_response_mode_{widget_key}",
                        )
                        selected_response_mode = dict(response_mode_options)[selected_response_label]
                        if st.button("保存回复策略", key="save_response_mode_%s" % widget_key, use_container_width=False):
                            try:
                                set_target_field(action_key, "response_mode", selected_response_mode, base_url=base)
                                st.success("已切换回复策略为：%s" % selected_response_label)
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
                    with st.expander("类别 / 管理员设置", expanded=False):
                        new_cat = st.radio(
                            "类别",
                            options=["user", "admin"],
                            index=1 if is_admin else 0,
                            format_func=lambda v: _label(CATEGORY_LABELS, v),
                            key="cat_radio_%s" % widget_key,
                            horizontal=True,
                        )
                        if st.button("保存类别", key="save_cat_%s" % widget_key, use_container_width=False):
                            try:
                                set_target_category(action_key, new_cat, base_url=base)
                                st.success("已设为 %s" % _label(CATEGORY_LABELS, new_cat))
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
                        st.caption("管理员白名单：留空表示该目标下所有发送者都被视为管理员。可填写 wxid 或昵称/备注，每行一个。")
                        admin_senders_text = st.text_area(
                            "管理员发送者白名单（每行一个 wxid 或昵称，留空=该目标下所有发送者均为管理员）",
                            value='\n'.join(t.get('admin_senders') or []),
                            key="admin_senders_%s" % widget_key,
                            height=100,
                        )
                        if st.button("保存管理员白名单", key="save_admin_senders_%s" % widget_key, use_container_width=False):
                            try:
                                cleaned = [s.strip() for s in admin_senders_text.splitlines() if s.strip()]
                                set_target_field(action_key, "admin_senders", cleaned, base_url=base)
                                st.success("已保存")
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
                    with st.expander("Agent 绑定", expanded=False):
                        try:
                            _instance_list = list_agent_instances(base_url=base)
                        except ControlAPIError as _e:
                            _instance_list = []
                            st.error("读取 agent 实例失败：%s" % (_e,))
                        instance_ids = [str(i.get('id')) for i in _instance_list if i.get('id')]
                        current_dedicated = str(t.get('dedicated_agent_instance_id') or '')
                        options = [""] + instance_ids
                        try:
                            current_index = options.index(current_dedicated) if current_dedicated in options else 0
                        except ValueError:
                            current_index = 0
                        selected_id = st.selectbox(
                            "专用 Agent 实例",
                            options=options,
                            index=current_index,
                            key="dedicated_agent_%s" % widget_key,
                            help="留空=不绑定（任务进入通用池）；选择具体实例后只由该实例处理该目标的任务。",
                        )
                        if st.button("保存 Agent 绑定", key="save_dedicated_agent_%s" % widget_key, use_container_width=False):
                            try:
                                set_target_dedicated_agent(action_key, selected_id or "", base_url=base)
                                st.success("已保存")
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
                    with st.expander("CUA 独立窗口标题覆盖", expanded=False):
                        st.caption("如果该目标的独立聊天窗口标题与目标名不同，可在此覆盖。")
                        current_title = str(t.get("cua_window_title") or "")
                        new_title = st.text_input(
                            "窗口标题",
                            value=current_title,
                            placeholder=t.get("name") or "",
                            key="cua_title_%s" % widget_key,
                        )
                        if st.button("保存标题覆盖", key="save_cua_title_%s" % widget_key, use_container_width=False):
                            try:
                                set_target_field(action_key, "cua_window_title", new_title.strip(), base_url=base)
                                st.success("已保存")
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
                    with st.expander("薄化模式 / Agent 工具模式", expanded=False):
                        st.caption("薄化模式：monitor 只负责监听和发送，知识库检索、视觉、会话管理都交给 agent 侧。需在 Hermes 配置中注册 wechat_auto skill 与 wechat/leann MCP server。")
                        thin_monitor = st.toggle(
                            "启用薄化模式",
                            value=bool(t.get("thin_monitor")),
                            key="thin_monitor_%s" % widget_key,
                        )
                        if st.button("保存薄化模式", key="save_thin_monitor_%s" % widget_key, use_container_width=False):
                            try:
                                set_target_field(action_key, "thin_monitor", thin_monitor, base_url=base)
                                st.success("已%s薄化模式" % ("启用" if thin_monitor else "关闭"))
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
                        st.divider()
                        agent_mode_options = [("标准（Python 侧预检索）", "standard"), ("工具 Agent（Hermes MCP 工具循环）", "tool_agent")]
                        valid_agent_modes = {v for _, v in agent_mode_options}
                        current_agent_mode = t.get("agent_mode") or "standard"
                        if current_agent_mode not in valid_agent_modes:
                            current_agent_mode = "standard"
                        selected_agent_label = st.selectbox(
                            "Agent 工具模式",
                            options=[label for label, _ in agent_mode_options],
                            index=[v for _, v in agent_mode_options].index(current_agent_mode),
                            key="agent_mode_%s" % widget_key,
                        )
                        selected_agent_mode = dict(agent_mode_options)[selected_agent_label]
                        if st.button("保存 Agent 模式", key="save_agent_mode_%s" % widget_key, use_container_width=False):
                            try:
                                set_target_field(action_key, "agent_mode", selected_agent_mode, base_url=base)
                                st.success("已切换 Agent 模式为：%s" % selected_agent_label)
                                _clear_data_cache()
                                st.rerun()
                            except ControlAPIError as e:
                                st.error("保存失败：%s" % (e,))
            with main_cols[1]:
                if not t.get("enabled") and t.get("is_candidate"):
                    # Pre-pick category at enable time so the user can mark an
                    # admin candidate before clicking the button.
                    enable_cat = st.selectbox(
                        "类别",
                        options=["user", "admin"],
                        index=1 if is_admin else 0,
                        format_func=lambda v: _label(CATEGORY_LABELS, v),
                        key="enable_cat_%s" % widget_key,
                        label_visibility="collapsed",
                    )
                    if st.button("启用", key="on_%s" % widget_key, use_container_width=True):
                        try:
                            enable_target(action_key, category=enable_cat, base_url=base)
                            st.success("已启用 %s（%s）" % (action_key, _label(CATEGORY_LABELS, enable_cat)))
                            _clear_data_cache()
                            st.rerun()
                        except ControlAPIError as e:
                            st.error("启用失败：%s" % (e,))
                if not t.get("enabled") and not t.get("is_candidate"):
                    if st.button("恢复监听", key="resume_%s" % widget_key, use_container_width=True):
                        try:
                            enable_target(action_key, base_url=base)
                            st.success("已恢复监听 %s" % action_key)
                            _clear_data_cache()
                            st.rerun()
                        except ControlAPIError as e:
                            st.error("恢复失败：%s" % (e,))
                if t.get("enabled"):
                    if st.button("停用", key="off_%s" % widget_key, use_container_width=True):
                        try:
                            disable_target(action_key, base_url=base)
                            st.success("已停用 %s" % action_key)
                            _clear_data_cache()
                            st.rerun()
                        except ControlAPIError as e:
                            st.error("停用失败：%s" % (e,))
                if st.button("删除", key="del_%s" % widget_key, use_container_width=True):
                    try:
                        delete_target(action_key, base_url=base)
                        st.success("已删除 %s" % action_key)
                        _clear_data_cache()
                        st.rerun()
                    except ControlAPIError as e:
                        st.error("删除失败：%s" % (e,))


def _page_triggers():
    st.header("触发词管理")
    base = st.session_state.base_url
    all_targets = st.session_state.get("targets_all") or []
    targets = [t for t in all_targets if t.get("enabled")]

    st.subheader("全局默认触发词")
    default_triggers = list(st.session_state.get("default_triggers_result") or [])
    st.caption("当前：%s" % (", ".join(default_triggers) if default_triggers else "（空）"))
    default_text = st.text_area("每行一个默认触发词", value="\n".join(default_triggers),
                                height=80, key="default_triggers_editor")
    dc1, dc2 = st.columns([1, 1])
    with dc1:
        if st.button("保存默认触发词", use_container_width=True, key="save_default_triggers"):
            words = [w.strip() for w in (default_text or "").splitlines() if w.strip()]
            try:
                replace_default_triggers(words, base_url=base)
                _clear_data_cache()
                st.success("已保存 %d 个默认触发词" % len(words))
                st.rerun()
            except ControlAPIError as e:
                st.error("保存失败：%s" % (e,))
    with dc2:
        if st.button("清空默认触发词", use_container_width=True, key="clear_default_triggers"):
            try:
                replace_default_triggers([], base_url=base)
                _clear_data_cache()
                st.success("已清空默认触发词")
                st.rerun()
            except ControlAPIError as e:
                st.error("清空失败：%s" % (e,))
    st.divider()

    if not targets:
        st.caption("没有已启用的目标，请先启用一个目标。")
        return
    options = [("%s（%s）" % (t.get("name") or "?", t.get("username") or "")) for t in targets]
    idx = st.selectbox("选择目标", options=list(range(len(options))),
                       format_func=lambda i: options[i])
    target = targets[idx]
    key = target.get("name") or target.get("username") or ""

    st.caption("响应模式请在「监听目标」页统一配置；本页只管理触发词。")

    info = {}
    try:
        info = get_triggers(key, base_url=base)
    except ControlAPIError as e:
        st.error("读取触发词失败：%s" % (e,))
    current = list((info or {}).get("triggers") or [])
    default_triggers = list((info or {}).get("default_triggers") or [])

    st.markdown("**当前目标触发词**：" + (", ".join(current) if current else "（空，使用全局 default）"))
    st.caption("**全局默认触发词**：" + (", ".join(default_triggers) if default_triggers else "（空）"))
    st.divider()

    st.markdown("**新增单个触发词**")
    c1, c2 = st.columns([4, 1])
    with c1:
        new_word = st.text_input("触发词内容", placeholder="例如：@飞扬的跟屁虫")
    with c2:
        st.write("")
        add_btn = st.button("添加", use_container_width=True, key="trigger_add_btn")
    if add_btn:
        if not new_word.strip():
            st.warning("触发词不能为空")
        else:
            try:
                add_triggers(key, [new_word.strip()], base_url=base)
                st.success("已添加")
                st.rerun()
            except ControlAPIError as e:
                st.error("添加失败：%s" % (e,))

    st.markdown("**批量替换**")
    bulk = st.text_area("每行一个触发词", value="\n".join(current), height=120)
    if st.button("替换为以上内容"):
        words = [w.strip() for w in (bulk or "").splitlines() if w.strip()]
        try:
            replace_triggers(key, words, base_url=base)
            st.success("已替换为 %d 个词" % len(words))
            st.rerun()
        except ControlAPIError as e:
            st.error("替换失败：%s" % (e,))

    c3, c4 = st.columns(2)
    with c3:
        if st.button("清空本目标触发词", use_container_width=True):
            try:
                clear_triggers(key, base_url=base)
                st.success("已清空")
                st.rerun()
            except ControlAPIError as e:
                st.error("清空失败：%s" % (e,))
    with c4:
        if st.button("删除全部当前触发词", use_container_width=True):
            if not current:
                st.warning("当前为空")
            else:
                try:
                    remove_triggers(key, current, base_url=base)
                    st.success("已删除")
                    st.rerun()
                except ControlAPIError as e:
                    st.error("删除失败：%s" % (e,))


def _page_knowledge():
    st.header("知识库管理")
    base = st.session_state.base_url
    kbs = st.session_state.get("kbs_result") or []
    targets = [t for t in (st.session_state.get("targets_all") or []) if not t.get("is_candidate")]

    if st.button("刷新"):
        _clear_data_cache()
        st.rerun()

    # ---- 顶部配置区：新增 + 绑定 ----
    st.subheader("新增本地知识库")
    c1, c2, c3, c4 = st.columns([2, 3, 2, 1])
    with c1:
        new_id = st.text_input("别名", placeholder="例如：workdocs", key="kb_new_id")
    with c2:
        new_desc = st.text_input("说明", placeholder="例如：工作资料本地知识库", key="kb_new_desc")
    with c3:
        source_choice = st.radio("来源", ["普通文件夹", "Obsidian vault"], horizontal=True,
                                 key="kb_local_source_radio")
    with c4:
        st.write("")
        if st.button("创建", use_container_width=True, key="kb_create_local"):
            if not new_id.strip():
                st.warning("别名不能为空")
            else:
                try:
                    source = "obsidian" if source_choice == "Obsidian vault" else "local_folder"
                    save_kb({
                        "id": new_id.strip(),
                        "type": "local",
                        "source": source,
                        "description": new_desc.strip(),
                        "replace": False,
                    }, base_url=base)
                    _clear_data_cache()
                    st.success("已创建本地知识库")
                    st.rerun()
                except ControlAPIError as e:
                    st.error("创建失败：%s" % (e,))

    st.divider()
    st.subheader("新增 LEANN 索引")
    leann_create_msg_key = "leann_create_msg"
    if leann_create_msg_key in st.session_state:
        for msg in st.session_state.pop(leann_create_msg_key):
            if msg.get("type") == "success":
                st.success(msg["text"])
            elif msg.get("type") == "info":
                st.info(msg["text"])
            elif msg.get("type") == "warning":
                st.warning(msg["text"])

    # Discover existing LEANN indexes created by `leann` in the runtime directory.
    leann_index_options = ["(新建)"]
    leann_index_error = ""
    try:
        leann_list_res = list_leann_indexes(base_url=base)
        leann_index_names = [i.get("name") for i in (leann_list_res.get("indexes") or []) if i.get("name")]
        leann_index_options.extend([n for n in leann_index_names if n])
        leann_index_error = leann_list_res.get("error") or ""
    except ControlAPIError as e:
        leann_index_error = str(e)
    if leann_index_error:
        st.caption("⚠️ 读取现有 LEANN 索引列表失败：%s" % leann_index_error)

    local_kb_paths = {
        kb.get("id"): (kb.get("path") or "")
        for kb in kbs
        if kb.get("type") == "local" and kb.get("path")
    }

    leann_mode = st.selectbox("选择已有索引或新建", options=leann_index_options, key="kb_leann_mode")
    is_new_leann = leann_mode == "(新建)"
    lc1, lc2 = st.columns([2, 2])
    with lc1:
        leann_new_id = st.text_input("别名", placeholder="例如：work_kb", key="kb_leann_new_id")
    with lc2:
        leann_new_desc = st.text_input("说明", placeholder="例如：工作资料向量索引", key="kb_leann_new_desc")
    if is_new_leann:
        leann_index_name = st.text_input("LEANN 索引名", value=leann_new_id.strip(), placeholder="例如：work_kb", key="kb_leann_index_name")
    else:
        leann_index_name = leann_mode
        st.caption("将绑定到已有索引：**%s**" % leann_mode)

    docs_dir_col, copy_col = st.columns([3, 1])
    with docs_dir_col:
        leann_docs_dir = st.text_input(
            "来源目录（要索引的文件夹）",
            placeholder="例如 wiki/workdocs 或 C:\\docs\\work",
            key="kb_leann_docs_dir",
            help="相对路径会基于 wiki_dir 解析，绝对路径按原样使用。",
        )
    with copy_col:
        st.write("")
        copy_from = st.selectbox(
            "从本地 KB 复制路径",
            options=[""] + list(local_kb_paths.keys()),
            key="kb_leann_copy_from",
            label_visibility="collapsed",
        )
        if copy_from and copy_from in local_kb_paths:
            st.session_state["kb_leann_docs_dir"] = local_kb_paths[copy_from]
            st.rerun()

    leann_build_now = st.checkbox("创建后立即后台构建索引", value=False, key="kb_leann_build_now")
    if st.button("创建", use_container_width=True, key="kb_create_leann"):
        if not leann_new_id.strip() or not leann_index_name.strip():
            st.warning("别名和索引名不能为空")
        else:
            try:
                payload = {
                    "id": leann_new_id.strip(),
                    "type": "leann",
                    "knowledge_base_id": leann_index_name.strip(),
                    "description": leann_new_desc.strip(),
                    "replace": False,
                }
                if leann_docs_dir.strip():
                    payload["docs_dir"] = leann_docs_dir.strip()
                save_kb(payload, base_url=base)
                messages = [{"type": "success", "text": "已创建 LEANN 索引绑定"}]
                if leann_build_now:
                    try:
                        build_result = build_leann_kb(leann_new_id.strip(), force=True, base_url=base)
                        messages.append({"type": "info", "text": "已启动后台构建：%s" % build_result.get("build_id")})
                    except ControlAPIError as e:
                        messages.append({"type": "warning", "text": "索引绑定已创建，但后台构建启动失败：%s" % (e,)})
                st.session_state[leann_create_msg_key] = messages
                _clear_data_cache()
                st.rerun()
            except ControlAPIError as e:
                st.error("创建失败：%s" % (e,))

    st.divider()
    st.subheader("绑定知识库到目标")
    if not targets:
        st.caption("暂无可绑定目标。")
    else:
        target_options = ["%s（%s）" % (t.get("name") or "?", t.get("username") or "") for t in targets]
        target_idx = st.selectbox("选择目标", options=list(range(len(target_options))),
                                  format_func=lambda i: target_options[i], key="kb_bind_target")
        target = targets[target_idx]
        target_key = target.get("name") or target.get("username") or ""
        kb_ids = [kb.get("id") for kb in kbs if kb.get("id")]
        def _kb_option_label(kb_id):
            kb = {k.get("id"): k for k in kbs}.get(kb_id)
            if not kb:
                return kb_id
            src = str(
                kb.get("source")
                or ("local_folder" if (kb.get("type") or "local") == "local" else kb.get("type"))
                or "local_folder"
            ).lower()
            src_label = _label(KB_SOURCE_LABELS, src)
            count = ""
            kb_type = (kb.get("type") or "local").lower()
            if kb_type == "local":
                fc = kb.get("file_count")
                if isinstance(fc, int) and fc:
                    count = "文档 %d" % fc
            elif kb_type in ("getnote", "ima"):
                nc = kb.get("online_note_count")
                fc2 = kb.get("online_file_count")
                bits = []
                if isinstance(nc, int) and nc:
                    bits.append("笔记 %d" % nc)
                if isinstance(fc2, int) and fc2:
                    bits.append("文件 %d" % fc2)
                if bits:
                    count = "，".join(bits)
            elif kb_type == "leann":
                idx = kb.get("index_name") or kb.get("knowledge_base_id") or ""
                exists = bool(kb.get("exists"))
                count = "索引 %s%s" % (idx, " · 存在" if exists else " · 缺失") if idx else "未配置索引"
            enabled = "已启用" if kb.get("enabled") else "已停用"
            return "%s · %s · %s · %s" % (kb_id, src_label, count or "在线", enabled)
        kb_source_by_id = {}
        kb_type_by_id = {}
        for kb in kbs:
            kb_id = kb.get("id")
            if not kb_id:
                continue
            kb_source_by_id[kb_id] = str(
                kb.get("source")
                or ("local_folder" if (kb.get("type") or "local") == "local" else kb.get("type"))
                or "local_folder"
            ).lower()
            kb_type_by_id[kb_id] = str(kb.get("type") or "local").lower()
        current = [x for x in (target.get("knowledge_bases") or []) if x in kb_ids]
        selected = st.multiselect("绑定知识库", options=kb_ids, default=current, key="kb_bind_select_%s" % target_key, format_func=_kb_option_label)
        selected_sources = {kb_source_by_id.get(x, "local_folder") for x in selected}
        if len(selected_sources) > 1:
            st.warning("一个监听目标只能绑定同源知识库。请只选同一个来源，例如只选 Obsidian、只选普通本地文件夹、只选 Get笔记，或只选 LEANN 索引。")
        elif selected_sources:
            only_source = next(iter(selected_sources))
            st.caption("当前绑定来源：%s" % _label(KB_SOURCE_LABELS, only_source))
            if only_source == "leann":
                allowed = [kb.get("index_name") or kb.get("id") or "?" for kb in kbs if kb.get("id") in selected and kb.get("type") == "leann"]
                if allowed:
                    st.info("该目标在工具 Agent 模式下只能检索以下 LEANN 索引：%s。" % ", ".join(allowed))
        if st.button("保存绑定", key="kb_bind_save"):
            if len(selected_sources) > 1:
                st.error("保存失败：不能混用不同来源的知识库。")
                return
            try:
                replace_target_kbs(target_key, selected, base_url=base)
                _clear_data_cache()
                st.success("已保存绑定")
                st.rerun()
            except ControlAPIError as e:
                st.error("保存失败：%s" % (e,))

        with st.expander("按目标模拟检索", expanded=False):
            bound_kb_ids = [x for x in (target.get("knowledge_bases") or []) if x in kb_ids]
            q = st.text_input("查询词（测试当前目标绑定的知识库）", key="target_kb_diag_query_%s" % target_key,
                              placeholder="例如：押金怎么退")
            if len(bound_kb_ids) == 0:
                st.warning("当前目标未绑定知识库")
            else:
                diag_kb = bound_kb_ids[0]
                if len(bound_kb_ids) > 1:
                    diag_kb = st.selectbox(
                        "选择要测试的知识库",
                        options=bound_kb_ids,
                        key="target_kb_diag_select_%s" % target_key,
                        format_func=lambda kb_id: _kb_option_label(kb_id),
                    )
                diag_type = kb_type_by_id.get(diag_kb, "local")
                diag_supported = diag_type in ("local", "leann")
                if not diag_supported:
                    st.info("%s 暂不支持模拟检索/诊断，仅本地知识库（Obsidian/本地文件夹）和 LEANN 索引支持。" % _label(KB_SOURCE_LABELS, kb_source_by_id.get(diag_kb, "local_folder")))
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("模拟检索", key="target_kb_search_%s" % target_key, use_container_width=True, disabled=not diag_supported):
                        if not q.strip():
                            st.warning("请先输入查询词")
                        else:
                            try:
                                res = search_kb(diag_kb, q.strip(), base_url=base, limit=5)
                                st.session_state["target_kb_search_result_%s" % target_key] = res
                            except ControlAPIError as e:
                                st.error("查询失败：%s" % (e,))
                with c2:
                    if st.button("诊断索引", key="target_kb_diag_%s" % target_key, use_container_width=True, disabled=not diag_supported):
                        if not q.strip() and diag_type != "leann":
                            st.warning("请先输入查询词")
                        else:
                            try:
                                res = diagnose_kb(diag_kb, (q or "").strip(), base_url=base)
                                st.session_state["target_kb_diag_result_%s" % target_key] = res
                            except ControlAPIError as e:
                                st.error("诊断失败：%s" % (e,))
            sres = st.session_state.get("target_kb_search_result_%s" % target_key)
            if sres:
                hits = sres.get("hits") or []
                st.caption("命中 %d / %d 个文档" % (sres.get("matched_files", 0), sres.get("total_files", 0)))
                if not hits:
                    st.caption("没有命中。")
                for h in hits:
                    score = h.get("score")
                    score_txt = "（分数 %d）" % score if isinstance(score, int) else ""
                    st.markdown("- **%s**%s" % (h.get("rel_path"), score_txt))
                    snippet = (h.get("snippet") or "").strip()
                    if snippet:
                        st.caption("  " + snippet[:160].replace("\n", " "))
            dres = st.session_state.get("target_kb_diag_result_%s" % target_key)
            if dres:
                with st.expander("诊断结果", expanded=True):
                    st.json(dres)

    st.divider()

    if not kbs:
        st.caption("未配置知识库。")
        return

    st.subheader("已配置知识库")
    for kb in kbs:
        kb_id = kb.get("id") or ""
        enabled = bool(kb.get("enabled"))
        kb_type = (kb.get("type") or "local").lower()
        managed = bool(kb.get("managed", True))
        upload_nonce_key = "kb_upload_nonce_%s" % kb_id
        if upload_nonce_key not in st.session_state:
            st.session_state[upload_nonce_key] = 0
        with st.container(border=True):
            cols = st.columns([5, 1])
            with cols[0]:
                # 标题：别名 · 状态 · 统计
                if kb_type == "local":
                    fc = kb.get("file_count")
                    suffix = " · %d 个文档" % fc if isinstance(fc, int) and fc else ""
                elif kb_type in ("getnote", "ima"):
                    name = (kb.get("online_name") or "").strip()
                    note_count = kb.get("online_note_count")
                    file_count = kb.get("online_file_count")
                    if name:
                        bits = []
                        if isinstance(note_count, int) and note_count:
                            bits.append("笔记 %d" % note_count)
                        if isinstance(file_count, int) and file_count:
                            bits.append("文件 %d" % file_count)
                        detail = " · 知识库/文件夹：%s（ID：%s）" % (name, kb.get("knowledge_base_id") or "")
                        if bits:
                            detail += "（%s）" % "，".join(bits)
                        suffix = detail
                    else:
                        suffix = " · 在线知识库（名称未获取：%s）" % (kb.get("online_error") or "未知")
                elif kb_type == "leann":
                    idx = kb.get("index_name") or kb.get("knowledge_base_id") or ""
                    idx_exists = bool(kb.get("exists"))
                    suffix = " · LEANN 索引：%s %s" % (
                        idx,
                        "（存在）" if idx_exists else "（缺失）" if idx else "",
                    ) if idx else " · LEANN 索引未配置"
                else:
                    suffix = ""
                st.markdown("**%s**  %s%s" % (kb_id, "已启用" if enabled else "已停用", suffix))
                # 来源行：去掉“类型”重复，只保留来源 + 说明
                # 未注册（wiki 扫描得到）的 KB 仅展示，不可启用/停用/删除
                managed_caption = " · 未注册" if not managed else ""
                st.caption("来源：%s · 说明：%s%s" % (
                    _label(KB_SOURCE_LABELS, kb.get("source") or "local_folder"),
                    kb.get("description") or "",
                    managed_caption,
                ))
                if kb.get("path"):
                    st.caption("路径：%s" % kb.get("path"))
                if kb.get("knowledge_base_id"):
                    st.caption("外部ID：%s" % kb.get("knowledge_base_id"))
                if kb.get("index_name"):
                    st.caption("LEANN 索引名：%s" % kb.get("index_name"))
                if kb_type == "leann":
                    docs_dir = kb.get("docs_dir") or ""
                    docs_dir_resolved = kb.get("docs_dir_resolved") or ""
                    if docs_dir:
                        st.caption("来源目录：%s%s" % (docs_dir, " → %s" % docs_dir_resolved if docs_dir_resolved and docs_dir_resolved != docs_dir else ""))
                    else:
                        st.warning("未配置来源目录（docs_dir），无法构建索引。请在这里补充来源目录。")
                    with st.expander("编辑 LEANN 来源目录", expanded=not bool(docs_dir)):
                        new_docs_dir = st.text_input(
                            "来源目录（要索引的文件夹）",
                            value=docs_dir,
                            placeholder="例如 wiki/workdocs 或 C:\\docs\\work",
                            key="leann_docs_dir_edit_%s" % kb_id,
                            help="相对路径会基于 wiki_dir 解析，绝对路径按原样使用。",
                        )
                        if st.button("保存来源目录", key="leann_docs_dir_save_%s" % kb_id, use_container_width=True):
                            if not new_docs_dir.strip():
                                st.warning("来源目录不能为空")
                            else:
                                try:
                                    payload = {
                                        "id": kb_id,
                                        "type": "leann",
                                        "knowledge_base_id": idx,
                                        "description": kb.get("description") or "",
                                        "enabled": enabled,
                                        "replace": True,
                                        "docs_dir": new_docs_dir.strip(),
                                    }
                                    if kb.get("executable"):
                                        payload["executable"] = kb.get("executable")
                                    if kb.get("limit") is not None:
                                        payload["limit"] = kb.get("limit")
                                    if kb.get("timeout") is not None:
                                        payload["timeout"] = kb.get("timeout")
                                    save_kb(payload, base_url=base)
                                    _clear_data_cache()
                                    st.success("已保存来源目录")
                                    st.rerun()
                                except ControlAPIError as e:
                                    st.error("保存失败：%s" % (e,))
                    if kb.get("index_path"):
                        st.caption("物理位置：%s" % kb.get("index_path"))
                    build_state_key = "leann_build_state_%s" % kb_id
                    can_build = bool(docs_dir_resolved or docs_dir)
                    build_col1, build_col2 = st.columns([1, 2])
                    with build_col1:
                        rebuild_disabled = not can_build
                        rebuild_help = "先配置来源目录" if rebuild_disabled else None
                        if st.button("🔄 重建索引", key="leann_rebuild_%s" % kb_id, use_container_width=True, disabled=rebuild_disabled, help=rebuild_help):
                            try:
                                result = build_leann_kb(kb_id, force=True, base_url=base)
                                st.session_state[build_state_key] = result
                                st.success("已启动后台构建：%s" % result.get("build_id"))
                            except ControlAPIError as e:
                                st.error("启动失败：%s" % (e,))
                    with build_col2:
                        current = st.session_state.get(build_state_key)
                        # Also show persisted latest build if no in-session build.
                        if not current and kb.get("last_build_id"):
                            current = {"build_id": kb.get("last_build_id")}
                        if current:
                            try:
                                status_info = leann_build_status(kb_id, current.get("build_id"), base_url=base)
                            except ControlAPIError as e:
                                st.error("读取构建状态失败：%s" % e)
                                status_info = {}
                            status = status_info.get("status", "unknown")
                            error = status_info.get("error") or ""
                            updated = status_info.get("updated_at")
                            status_text = status
                            if updated:
                                status_text += " · %s" % time.strftime("%H:%M:%S", time.localtime(updated))
                            if status == "done":
                                st.success("构建完成 · %s" % status_text)
                            elif status == "failed":
                                extra = ""
                                if error:
                                    extra += "：%s" % error
                                rc = status_info.get("returncode")
                                if rc is not None:
                                    extra += "（返回码 %s）" % rc
                                st.error("构建失败%s" % extra)
                            elif status == "running":
                                st.info("构建中… · %s" % status_text)
                            elif status == "started":
                                st.info("已启动 · %s" % status_text)
                            else:
                                st.caption("状态：%s" % status_text)
                            log_path = status_info.get("log_path")
                            if log_path and Path(log_path).exists():
                                with st.expander("查看日志", expanded=False):
                                    try:
                                        log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
                                        st.text_area("日志", value=log_text[-4000:], height=200, disabled=True, label_visibility="collapsed")
                                    except Exception as e:
                                        st.caption("无法读取日志：%s" % e)
                            elif log_path:
                                st.caption("日志：%s" % log_path)
            with cols[1]:
                # 未注册（wiki 扫描得到）的 KB：禁用启用/停用/删除，仅允许打开与查询
                mutation_disabled = not managed
                if enabled:
                    if st.button("停用", key="kb_off_%s" % kb_id, use_container_width=True, disabled=mutation_disabled, help="未注册的 KB 无法停用" if mutation_disabled else None):
                        try:
                            disable_kb(kb_id, base_url=base)
                            if "data_loaded" in st.session_state:
                                del st.session_state["data_loaded"]
                            st.rerun()
                        except ControlAPIError as e:
                            st.error("停用失败：%s" % (e,))
                else:
                    if st.button("启用", key="kb_on_%s" % kb_id, use_container_width=True, disabled=mutation_disabled, help="未注册的 KB 无法启用" if mutation_disabled else None):
                        try:
                            enable_kb(kb_id, base_url=base)
                            if "data_loaded" in st.session_state:
                                del st.session_state["data_loaded"]
                            st.rerun()
                        except ControlAPIError as e:
                            st.error("启用失败：%s" % (e,))
                if st.button("删除", key="kb_del_%s" % kb_id, use_container_width=True, disabled=mutation_disabled, help="未注册的 KB 无法删除" if mutation_disabled else None):
                    try:
                        delete_kb(kb_id, remove_files=False, base_url=base)
                        if "data_loaded" in st.session_state:
                            del st.session_state["data_loaded"]
                        st.rerun()
                    except ControlAPIError as e:
                        st.error("删除失败：%s" % (e,))
            if kb_type == "local":
                with st.expander("导入文件或目录"):
                    uploads = st.file_uploader(
                        "选择要导入的文件（可多选）",
                        type=None,
                        accept_multiple_files=True,
                        key="kb_uploads_%s_%d" % (kb_id, st.session_state[upload_nonce_key]),
                        help="支持任意可转换格式；.md 直接复制，其余会通过 MarkItDown 转成 Markdown。",
                    )
                    dir_path = st.text_input(
                        "或直接输入本地目录路径",
                        key="kb_dir_%s" % kb_id,
                        placeholder="例如 C:\\docs\\project-x",
                    )
                    if st.button("导入到知识库", key="kb_import_%s" % kb_id):
                        import_path = ""
                        try:
                            if uploads:
                                import tempfile, os as _os
                                tmp = tempfile.mkdtemp(prefix="wechat-kb-upload-")
                                for up in uploads:
                                    data = up.read()
                                    fname = up.name or "upload.bin"
                                    out = _os.path.join(tmp, fname)
                                    with open(out, "wb") as f:
                                        f.write(data)
                                import_path = tmp
                            elif dir_path.strip():
                                import_path = dir_path.strip()
                            if not import_path:
                                st.warning("请先选择文件或输入目录路径")
                            else:
                                with st.spinner("导入/转换中..."):
                                    res = import_kb(kb_id, import_path, base_url=base)
                                copied = res.get("copied") or []
                                failed = res.get("failed") or []
                                n_ok = len(copied)
                                n_fail = len(failed)
                                st.session_state[upload_nonce_key] += 1
                                st.session_state["kb_last_import_%s" % kb_id] = {
                                    "copied": copied,
                                    "failed": failed,
                                    "ts": time.time(),
                                }
                                # 就地显示成功/失败提示：st.success / st.error 停留到下次用户操作，不再 rerun 冲掉
                                if n_fail:
                                    st.error("导入 %d 个文件，%d 个失败。" % (n_ok, n_fail))
                                else:
                                    st.success("已导入 %d 个文件" % n_ok)
                                # 就地更新 KB 标题栏里的 file_count，不需 rerun
                                kbs_cached = st.session_state.get("kbs_result")
                                if kbs_cached:
                                    for row in kbs_cached:
                                        if row and row.get("id") == kb_id:
                                            try:
                                                info = reg_get_kb_info(kb_id)
                                                row["file_count"] = (info or {}).get("file_count", 0) if row.get("type") == "local" else None
                                            except Exception:
                                                pass
                                            break
                        except ControlAPIError as e:
                            st.error("导入失败：%s" % (e,))
                    # 失败明细：仅在有失败时显示，✕ 关闭收起
                    last = st.session_state.get("kb_last_import_%s" % kb_id)
                    if last and last.get("failed"):
                        last_ts = int(last.get("ts") or 0)
                        dismissed_key = "kb_failure_dismissed_%s_%d" % (kb_id, last_ts)
                        if not st.session_state.get(dismissed_key, False):
                            n_fail = len(last.get("failed") or [])
                            with st.expander("失败明细 · %d 个" % n_fail, expanded=True):
                                if st.button("✕ 关闭", key=dismissed_key + "_btn"):
                                    st.session_state[dismissed_key] = True
                                    st.rerun()
                                for bucket, items in _group_failures(last.get("failed") or []):
                                    st.markdown("**%s（%d 个）**" % (bucket["title"], len(items)))
                                    st.caption(bucket["advice"])
                                    for item in items:
                                        err = item.get("error") or ""
                                        name = _failure_basename(err) or item.get("path") or "?"
                                        short = _failure_short_reason(err)
                                        st.markdown("- `%s`" % name)
                                        if short:
                                            st.caption("  %s" % short)
                                        # 完整错误：默认收起，需要时再展开
                                        with st.expander("完整错误", expanded=False):
                                            st.caption(err)
                with st.expander("测试检索 / 打开目录"):
                    q = st.text_input("查询词（测试这个知识库的检索效果）", key="kb_test_q_%s" % kb_id,
                                      placeholder="例如：押金怎么退")
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        if st.button("查询", key="kb_test_search_%s" % kb_id, use_container_width=True):
                            if not q.strip():
                                st.warning("请先输入查询词")
                            else:
                                try:
                                    res = search_kb(kb_id, q.strip(), base_url=base, limit=5)
                                    st.session_state["kb_test_result_%s" % kb_id] = res
                                except ControlAPIError as e:
                                    st.error("查询失败：%s" % (e,))
                    with c2:
                        if st.button("诊断索引", key="kb_diag_%s" % kb_id, use_container_width=True):
                            if not q.strip():
                                st.warning("请先输入查询词")
                            else:
                                try:
                                    res = diagnose_kb(kb_id, q.strip(), base_url=base)
                                    st.session_state["kb_diag_result_%s" % kb_id] = res
                                except ControlAPIError as e:
                                    st.error("诊断失败：%s" % (e,))
                    with c3:
                        if kb.get("source") == "obsidian":
                            if st.button("打开 Obsidian", key="kb_obsidian_%s" % kb_id, use_container_width=True):
                                try:
                                    open_kb_obsidian(kb_id, base_url=base)
                                    st.success("已打开 Obsidian")
                                except ControlAPIError as e:
                                    st.error("打开失败：%s" % (e,))
                        elif st.button("打开目录", key="kb_open_%s" % kb_id, use_container_width=True):
                            try:
                                open_kb(kb_id, base_url=base)
                                st.success("已打开目录")
                            except ControlAPIError as e:
                                st.error("打开失败：%s" % (e,))
                    tres = st.session_state.get("kb_test_result_%s" % kb_id)
                    if tres:
                        hits = tres.get("hits") or []
                        st.caption("命中 %d / %d 个文档" % (tres.get("matched_files", 0), tres.get("total_files", 0)))
                        if not hits:
                            st.caption("没有命中。")
                        for h in hits:
                            st.markdown("- **%s**（分数 %d）" % (h.get("rel_path"), h.get("score")))
                            snippet = (h.get("snippet") or "").strip()
                            if snippet:
                                st.caption("  " + snippet[:160].replace("\n", " "))
                    dres = st.session_state.get("kb_diag_result_%s" % kb_id)
                    if dres:
                        with st.expander("诊断结果", expanded=True):
                            st.json(dres)


def _job_status_label(value):
    return {
        "queued": "排队中",
        "dispatching": "派发中",
        "submitted": "已派给 Agent",
        "agent_running": "Agent 处理中",
        "running": "处理中",
        "done": "处理完成",
        "sending": "准备发送",
        "sent": "已发送",
        "failed": "处理失败",
        "timeout": "处理超时",
        "expired": "等待过期",
        "cancelled": "已取消",
    }.get(str(value or ""), str(value or "—"))


def _send_status_label(value):
    return {
        "pending": "待发送",
        "sent": "已发送",
        "failed": "发送失败",
        "skipped": "已跳过",
    }.get(str(value or ""), str(value or "—"))


def _provider_label(value):
    return {
        "echo": "安全测试（不调用真实模型）",
        "hermes": "Hermes 本地 Agent",
    }.get(str(value or ""), str(value or "—"))


def _job_summary(job):
    status = str(job.get("status") or "")
    if status == "queued":
        return "等待后台处理"
    if status == "dispatching":
        return "正在派给 Agent"
    if status == "submitted":
        return "已派给 Agent，等待首次轮询"
    if status == "agent_running":
        return "Agent 正在处理，等待结果"
    if status == "running":
        return "任务正在执行"
    if status == "expired":
        return "自动等待窗口已过期，可尝试拉取结果"
    if status == "timeout":
        return "处理超时，建议重试或检查处理服务"
    if status == "failed":
        return "处理失败：%s" % ((job.get("error") or "未知原因")[:80])
    return (job.get("result_text") or job.get("error") or "")[:100]


def _page_agent_jobs():
    st.header("复杂任务处理")
    st.caption("查看服务是否可用、做一次安全测试、检查是否有任务卡住。当前不会自动接入微信群监听，也不会自动发送微信。")
    base = st.session_state.get("base_url", DEFAULT_BASE_URL)

    try:
        stats = agent_jobs_stats(base_url=base)
    except ControlAPIError as e:
        st.error("任务统计读取失败：%s" % (e,))
        stats = {}
    try:
        ws = agent_worker_status(base_url=base)
    except ControlAPIError as e:
        st.error("后台处理服务状态读取失败：%s" % (e,))
        ws = {}
    try:
        loop_state = async_loop_status(base_url=base)
    except ControlAPIError as e:
        st.error("自动派发服务状态读取失败：%s" % (e,))
        loop_state = {}
    try:
        pool_state = agent_pool_status(base_url=base)
    except ControlAPIError as e:
        st.error("Agent 池状态读取失败：%s" % (e,))
        pool_state = {}

    queued = int(stats.get("queued", 0))
    running = (
        int(stats.get("running", 0))
        + int(stats.get("dispatching", 0))
        + int(stats.get("submitted", 0))
        + int(stats.get("agent_running", 0))
    )
    done = int(stats.get("done", 0)) + int(stats.get("sent", 0))
    abnormal = int(stats.get("failed", 0)) + int(stats.get("timeout", 0)) + int(stats.get("expired", 0))
    worker_running = bool(ws.get("running"))
    loop_running = bool(loop_state.get("running"))
    state = ws.get("state") or {}

    if abnormal:
        st.warning("发现异常任务，请优先查看下方任务检查。")
    elif queued and not (worker_running or loop_running):
        st.warning("有任务在排队，但自动派发未启动。请先启动自动处理。")
    elif loop_running or worker_running:
        st.success("复杂任务处理服务可用，可以开始测试。")
    else:
        st.info("自动处理未启动。首次检查建议先用“安全测试”。")

    st.subheader("当前状态")
    s1, s2 = st.columns(2)
    with s1.container(border=True):
        st.markdown("**任务情况**")
        c1, c2 = st.columns(2)
        c1.metric("排队中", queued)
        c2.metric("处理中", running)
        c3, c4 = st.columns(2)
        c3.metric("已完成", done)
        c4.metric("异常", abnormal)
        st.metric("自动处理", "运行中" if loop_running else "未启动")
        if abnormal:
            st.caption("发现异常任务，请查看下方列表。")
        elif queued:
            st.caption("有任务等待后台处理。")
        elif running:
            st.caption("有任务正在处理。")
        else:
            st.caption("当前没有积压。")

    st.subheader("Agent 自动处理池")
    st.caption("推荐只用这一块：把可用 Hermes profile/worker 加入自动处理池，系统会自动派任务、轮询结果、发回微信。同一个群仍然串行，多个群可以并发。")
    try:
        instances = list_agent_instances(base_url=base)
    except ControlAPIError as e:
        st.error("读取 agent 实例失败：%s" % (e,))
        instances = []
    try:
        discovered_profiles = discover_hermes_profiles(base_url=base)
    except ControlAPIError as e:
        st.warning("发现 Hermes profile/worker 失败：%s" % (e,))
        discovered_profiles = []
    configured_ids = {str(inst.get("id") or "") for inst in instances}
    available_instances = list(instances) + [p for p in discovered_profiles if str(p.get("id") or "") not in configured_ids]

    with st.container(border=True):
        if not available_instances:
            st.caption("未在 wechat_bot_targets.json 的 agent_provider.instances 找到实例配置，也未发现可用 Hermes profile/worker。")
        else:
            pool_rows = []
            for row in (pool_state.get("instances") or []):
                cur = row.get("current_job") or {}
                pool_rows.append({
                    "实例": row.get("label") or row.get("id"),
                    "Provider": row.get("provider"),
                    "状态": {
                        "busy": "处理中",
                        "idle": "空闲",
                        "offline": "离线",
                        "available": "可上岗",
                    }.get(str(row.get("status") or ""), row.get("status")),
                    "上岗": "是" if row.get("on_duty") else "否",
                    "当前任务": ("#%s · %s" % (cur.get("id"), cur.get("target_name"))) if cur else "—",
                    "会话": cur.get("external_session_id") or "—",
                })
            if pool_rows:
                st.dataframe(pool_rows, hide_index=True, use_container_width=True)
                st.caption("状态说明：空闲=已上岗可接任务；处理中=正在跑任务；离线=不可用；可上岗=健康但尚未加入自动处理池。")

            options = []
            for inst in available_instances:
                iid = str(inst.get("id") or "")
                label = str(inst.get("label") or inst.get("profile") or iid or "实例")
                kind = str(inst.get("provider") or inst.get("type") or "hermes")
                kind_label = {"hermes": "Hermes profile/worker", "echo": "Echo"}.get(kind, kind)
                bridge = inst.get("bridge_url") or inst.get("bridge_cwd") or inst.get("hermes_home") or "-"
                extras = []
                if inst.get("profile"):
                    extras.append("profile=" + str(inst.get("profile")))
                if inst.get("model"):
                    extras.append("model=" + str(inst.get("model")))
                if inst.get("toolsets"):
                    extras.append("toolsets=" + str(inst.get("toolsets")))
                st.caption("id=%s · 类型=%s · 配置=%s%s" % (iid, kind_label, bridge, (" · " + " · ".join(extras)) if extras else ""))

                pool_by_id = {str(row.get("id") or ""): row for row in (pool_state.get("instances") or [])}
                selected_pool = pool_by_id.get(iid) or {}
                st.metric("自动池状态", {
                    "busy": "处理中",
                    "idle": "空闲",
                    "offline": "离线",
                    "available": "可上岗",
                }.get(str(selected_pool.get("status") or ""), selected_pool.get("status") or "未知"))
                if selected_pool.get("current_job"):
                    cur = selected_pool.get("current_job") or {}
                    st.caption("当前任务：#%s · %s · %s" % (cur.get("id"), cur.get("target_name"), cur.get("status")))

                st.markdown("**自动处理池控制**")
                st.caption("已发现但未配置的 Hermes profile/worker 会在点击加入时写入 agent_provider.instances；不会自动改配置。停止按钮会停止整个自动处理池。")
                loop_cfg = (loop_state.get("config") or {}) if isinstance(loop_state, dict) else {}
                loop_inst = str(loop_cfg.get("instance_id") or "")
                if loop_running:
                    st.success("自动处理池运行中%s" % ((" · 在岗=%s" % loop_inst) if loop_inst else ""))
                else:
                    st.info("自动处理池未启动")
                l1, l2, l3 = st.columns(3)
                if l1.button("加入自动处理池", key="m5_loop_start_%s" % iid, use_container_width=True):
                    try:
                        if iid not in configured_ids:
                            register_agent_instance(inst, base_url=base)
                        res = start_async_loop(instance_id=iid, base_url=base)
                        if res.get("running"):
                            st.success("已加入自动处理池")
                            st.rerun()
                        else:
                            st.warning("启动结果：%s" % (res.get("action") or "未知"))
                    except ControlAPIError as e:
                        st.error("加入失败：%s" % (e,))
                if l2.button("检查这个实例", key="pool_hc_%s" % iid, use_container_width=True):
                    try:
                        h = agent_provider_health(provider=kind, instance_id=iid, base_url=base)
                        st.session_state["agent_last_health"] = h
                    except ControlAPIError as e:
                        h = {"ok": False, "ready": False, "error": str(e)}
                        st.session_state["agent_last_health"] = h
                    if h.get("ok") or h.get("ready"):
                        st.success("可用 · provider=%s · workers=%s" % (h.get("provider") or kind, h.get("workers") or 0))
                    else:
                        st.warning("未在自动池中或暂未检测：%s" % (h.get("error") or "可点击加入自动处理池进行注册和启动"))
                if l3.button("停止自动处理池", key="m5_loop_stop_%s" % iid, use_container_width=True):
                    try:
                        res = stop_async_loop(base_url=base)
                        st.success("已停止" if not res.get("running") else "已请求停止")
                        st.rerun()
                    except ControlAPIError as e:
                        st.error("停止自动处理失败：%s" % (e,))

                with st.expander("调试：单步推进队列", expanded=False):
                    st.caption("仅排查任务调度问题。不会自动持续运行。")
                    s1, s2, s3 = st.columns(3)
                    if s1.button("派发一次", key="dbg_dispatch_%s" % iid, use_container_width=True):
                        try:
                            res = run_dispatcher_once(instance_id=iid, provider=kind, base_url=base)
                            st.json(res)
                        except ControlAPIError as e:
                            st.error("派发失败：%s" % (e,))
                    if s2.button("轮询一次", key="dbg_reconcile_%s" % iid, use_container_width=True):
                        try:
                            res = run_reconciler_once(base_url=base)
                            st.json(res)
                        except ControlAPIError as e:
                            st.error("轮询失败：%s" % (e,))
                    if s3.button("发送一次", key="dbg_send_%s" % iid, use_container_width=True):
                        try:
                            res = run_sender_once(base_url=base)
                            st.json(res)
                        except ControlAPIError as e:
                            st.error("发送失败：%s" % (e,))

                with st.expander("旧同步 worker（调试用，不推荐日常使用）", expanded=False):
                    st.caption("这是早期方案：单独启动一个同步 worker。日常请使用上面的“自动处理池”。")
                    pool_timeout = st.number_input(
                        "最长等待（秒）", min_value=3, max_value=600, value=240, step=1,
                        key="pool_to_%s" % iid,
                    )
                    c_old1, c_old2 = st.columns(2)
                    if c_old1.button("启动旧 worker", key="pool_up_%s" % iid, use_container_width=True):
                        try:
                            if iid not in configured_ids:
                                register_agent_instance(inst, base_url=base)
                            res = start_agent_instance(iid, timeout=float(pool_timeout), worker_count=1, base_url=base)
                            st.success("旧 worker 已启动 · %s" % (res.get("action") or "started"))
                            st.rerun()
                        except ControlAPIError as e:
                            st.error("启动失败：%s" % (e,))
                    if c_old2.button("停止旧 worker", key="pool_dn_%s" % iid, use_container_width=True):
                        try:
                            res = stop_agent_worker(base_url=base)
                            st.success("已停止" if not res.get("running") else "已请求停止")
                            st.rerun()
                        except ControlAPIError as e:
                            st.error("停止失败：%s" % (e,))

    st.subheader("快速测试")
    st.caption("用一条测试任务确认：创建任务 → 被处理 → 返回结果，这个流程是否正常。")
    with st.form("agent_test_job_form"):
        prompt = st.text_area("测试内容", value="请用一句话说明今天上海天气适合穿什么", height=90)
        fc1, fc2, fc3, fc4 = st.columns(4)
        provider = fc1.selectbox("测试模式", ["echo", "hermes"], format_func=_provider_label, key="agent_test_provider")
        group_key = fc2.text_input("测试分组", value="manual-test", help="用于区分不同批次测试，可不改。")
        sender = fc3.text_input("操作人", value="tester")
        enabled_targets = [t for t in (st.session_state.get("targets_all") or []) if t.get("enabled")]
        target_options = ["不绑定目标"] + ["%s（%s）" % (t.get("name") or "?", t.get("username") or "") for t in enabled_targets]
        target_idx = fc4.selectbox("绑定目标", options=list(range(len(target_options))),
                                   format_func=lambda i: target_options[i], key="agent_test_target")
        selected_target = enabled_targets[target_idx - 1] if target_idx > 0 else None
        submitted = st.form_submit_button("开始测试", use_container_width=True)
        if submitted:
            try:
                res = create_agent_test_job(prompt, provider=provider, group_key=group_key, sender=sender,
                                            target=selected_target, base_url=base)
                job = res.get("job") or {}
                st.success("测试任务已创建。下一步：如果后台处理服务已运行，任务会自动开始；也可以用下方“立即处理一次”。")
                st.table([{
                    "任务编号": job.get("id"),
                    "当前状态": _job_status_label(job.get("status")),
                    "测试分组": job.get("group_key"),
                    "处理方式": _provider_label(job.get("provider")),
                }])
                with st.expander("查看创建详情（JSON）", expanded=False):
                    st.json(job or res)
            except ControlAPIError as e:
                st.error("创建失败：%s" % (e,))

    with st.expander("高级操作：立即处理一次", expanded=False):
        st.caption("用于排查“任务已创建但没有自动处理”的情况。普通使用无需操作。只执行一次处理检查，不会持续后台运行。")
        wc1, wc2, wc3 = st.columns([1, 1, 2])
        worker_provider = wc1.selectbox("处理模式", ["echo", "hermes"], format_func=_provider_label, key="agent_worker_provider")
        one_shot_timeout = wc2.number_input("最长等待时间", min_value=3, max_value=600, value=240, step=1, key="agent_worker_once_timeout")
        if wc3.button("立即处理一次", use_container_width=True, key="agent_worker_once"):
            try:
                res = run_agent_worker_once(worker_provider, timeout=float(one_shot_timeout), base_url=base)
                if res.get("action") == "completed":
                    st.success("处理完成：已拿到结果。")
                elif res.get("action") == "idle":
                    st.info("当前没有可处理任务。")
                else:
                    st.warning("处理结果：%s" % (res.get("action") or "未知"))
                with st.expander("查看处理详情（JSON）", expanded=False):
                    st.json(res)
            except ControlAPIError as e:
                st.error("运行失败：%s" % (e,))

    st.subheader("任务检查")
    st.caption("优先关注异常任务和长时间未完成任务。")
    lc1, lc2, lc3, lc4 = st.columns([1, 1, 1, 1])
    range_label = lc1.selectbox("查看范围", ["全部任务", "仅排队中", "仅处理中", "仅异常", "仅已完成", "仅发送失败"], key="agent_job_range")
    limit = lc2.number_input("显示数量", min_value=10, max_value=200, value=50, step=10, key="agent_job_limit")
    group_filter = lc3.text_input("测试分组筛选（可空）", value="", key="agent_job_group")
    auto_refresh = lc4.checkbox("自动刷新", value=False, key="agent_job_auto_refresh", help="每 5 秒自动刷新任务列表")
    status_map = {
        "仅排队中": "queued",
        "仅已完成": "done",
    }
    status_filter = status_map.get(range_label)
    try:
        rows = agent_jobs(limit=int(limit), status=status_filter, group_key=group_filter or None, base_url=base)
    except ControlAPIError as e:
        st.error("任务列表读取失败：%s" % (e,))
        rows = []
    if range_label == "仅异常":
        rows = [j for j in rows if j.get("status") in ("failed", "timeout", "expired")]
    elif range_label == "仅处理中":
        rows = [j for j in rows if j.get("status") in ("running", "dispatching", "submitted", "agent_running")]
    elif range_label == "仅已完成":
        rows = [j for j in rows if j.get("status") in ("done", "sent")]
    elif range_label == "仅发送失败":
        rows = [j for j in rows if j.get("send_status") == "failed"]
    if not rows:
        st.caption("暂无任务。")
    else:
        table = []
        for j in rows:
            send_status = j.get("send_status") or "pending"
            error = j.get("error") or ""
            result_summary = _job_summary(j)
            if send_status == "failed" and error:
                result_summary = "发送失败：%s" % error[:60]
            payload = j.get("payload") or {}
            created = j.get("created_at") or 0
            created_str = time.strftime("%m-%d %H:%M", time.localtime(created)) if created else "—"
            next_poll = j.get("next_poll_at") or 0
            next_poll_str = time.strftime("%H:%M:%S", time.localtime(next_poll)) if next_poll else "—"
            external_session = j.get("external_session_id") or (payload.get("agent_instance_id") if isinstance(payload, dict) else "") or "—"
            table.append({
                "任务编号": j.get("id"),
                "当前状态": _job_status_label(j.get("status")),
                "群/联系人": j.get("target_name") or j.get("group_key"),
                "任务内容": (payload.get("prompt") or payload.get("clean_text") or "")[:40],
                "创建时间": created_str,
                "Agent会话/实例": str(external_session)[:28],
                "下次检查": next_poll_str,
                "任务内容类型": j.get("task_type"),
                "处理方式": _provider_label(j.get("provider")),
                "处理节点": j.get("worker_id") or "—",
                "发送结果": _send_status_label(send_status),
                "结果摘要": result_summary,
            })
        st.dataframe(table, use_container_width=True, hide_index=True)

        # Show abnormal jobs with dismiss / retry buttons
        abnormal_jobs = [j for j in rows if j.get("status") in ("failed", "timeout", "expired")]
        if abnormal_jobs:
            st.markdown("---")
            st.markdown("**异常任务（可恢复、忽略或补发）**")
            st.caption('拉取结果会从 agent 历史会话里找回超时后完成的回复；忽略后任务状态会变为"已取消"。')
            if st.button("一键忽略全部异常任务", key="dismiss_all_abnormal", type="secondary"):
                job_ids = [int(j["id"]) for j in abnormal_jobs if j.get("id")]
                res = dismiss_jobs_batch(job_ids, base_url=base)
                st.success("已忽略 %d/%d 个异常任务" % (res["ok_count"], res["total"]))
                if res["failed_ids"]:
                    st.warning("以下任务忽略失败：%s" % res["failed_ids"])
                st.rerun()
            for j in abnormal_jobs:
                job_id = j.get("id")
                if job_id is None:
                    continue
                title = j.get("target_name") or j.get("group_key") or "未知"
                with st.expander("任务 #%s - %s" % (job_id, title), expanded=False):
                    st.caption("任务内容：%s" % ((j.get("payload") or {}).get("prompt") or "")[:120])
                    st.caption("错误原因：%s" % (j.get("error") or "未知"))
                    if j.get("result_text"):
                        st.caption("结果预览：%s" % j["result_text"][:120])
                    raw_out = (j.get("payload") or {}).get("agent_raw_output")
                    if raw_out:
                        st.caption("Agent 原始输出：%s" % str(raw_out)[:300])
                    c1, c2, c3, c4, c5 = st.columns(5)
                    if c1.button("查看完整任务", key="view_job_%s" % job_id, use_container_width=True):
                        try:
                            detail = get_agent_job(int(job_id), base_url=base)
                            st.session_state["agent_job_detail_%s" % job_id] = detail
                        except ControlAPIError as e:
                            st.error("读取失败：%s" % (e,))
                    if c2.button("忽略", key="dismiss_%s" % job_id, use_container_width=True):
                        try:
                            res = dismiss_job(int(job_id), base_url=base)
                            if res.get("action") == "dismissed":
                                st.success("已忽略")
                                st.rerun()
                            else:
                                st.warning("忽略失败：%s" % (res.get("error") or "未知"))
                        except ControlAPIError as e:
                            st.error("忽略失败：%s" % (e,))
                    if c3.button("拉取结果", key="recover_%s" % job_id, use_container_width=True):
                        try:
                            res = recover_job_result(int(job_id), send=False, base_url=base)
                            if res.get("action") == "recovered":
                                st.success("已拉取结果")
                                st.rerun()
                            else:
                                detail = res.get("result") or {}
                                st.warning("还没找到可发送结果：%s" % (detail.get("error") or "未知"))
                        except ControlAPIError as e:
                            st.error("拉取失败：%s" % (e,))
                    if c4.button("拉取并发送", key="recover_send_%s" % job_id, use_container_width=True):
                        try:
                            res = recover_job_result(int(job_id), send=True, base_url=base)
                            if res.get("action") == "recovered" and (res.get("send_result") or {}).get("sent"):
                                st.success("已拉取并发送")
                                st.rerun()
                            elif res.get("action") == "recovered":
                                st.warning("已拉取结果，但发送失败：%s" % ((res.get("send_result") or {}).get("reason") or "未知"))
                            else:
                                detail = res.get("result") or {}
                                st.warning("还没找到可发送结果：%s" % (detail.get("error") or "未知"))
                        except ControlAPIError as e:
                            st.error("拉取并发送失败：%s" % (e,))
                    if j.get("result_text") and c5.button("补发已有", key="retry_send_%s" % job_id, use_container_width=True):
                        try:
                            res = retry_job_send(int(job_id), base_url=base)
                            if res.get("action") == "sent":
                                st.success("发送成功")
                                st.rerun()
                            else:
                                st.warning("发送失败：%s" % (res.get("send_result", {}).get("reason") or "未知"))
                        except ControlAPIError as e:
                            st.error("重试失败：%s" % (e,))
                    detail = st.session_state.get("agent_job_detail_%s" % job_id)
                    if detail:
                        with st.expander("完整任务 JSON", expanded=False):
                            st.json(detail)

        sendable_jobs = [
            j for j in rows
            if j.get("result_text") and j.get("send_status") != "sent" and j.get("status") in ("done", "failed", "timeout", "expired")
        ]
        if sendable_jobs:
            st.markdown("---")
            st.markdown("**已有结果待发送**")
            st.caption("这些任务已经有结果，但还没有成功发回微信。")
            if st.button("一键忽略全部待发送任务", key="dismiss_all_sendable", type="secondary"):
                job_ids = [int(j["id"]) for j in sendable_jobs if j.get("id")]
                res = dismiss_jobs_batch(job_ids, base_url=base)
                st.success("已忽略 %d/%d 个待发送任务" % (res["ok_count"], res["total"]))
                if res["failed_ids"]:
                    st.warning("以下任务忽略失败：%s" % res["failed_ids"])
                st.rerun()
            for j in sendable_jobs:
                job_id = j.get("id")
                if job_id is None:
                    continue
                title = j.get("target_name") or j.get("group_key") or "未知"
                with st.expander("任务 #%s - %s" % (job_id, title), expanded=False):
                    st.caption("任务内容：%s" % ((j.get("payload") or {}).get("prompt") or "")[:120])
                    st.caption("结果预览：%s" % (j.get("result_text") or "")[:160])
                    raw_out = (j.get("payload") or {}).get("agent_raw_output")
                    if raw_out:
                        st.caption("Agent 原始输出：%s" % str(raw_out)[:300])
                    s1, s2, s3 = st.columns(3)
                    if s1.button("查看完整任务", key="view_done_%s" % job_id, use_container_width=True):
                        try:
                            detail = get_agent_job(int(job_id), base_url=base)
                            st.session_state["agent_job_detail_%s" % job_id] = detail
                        except ControlAPIError as e:
                            st.error("读取失败：%s" % (e,))
                    if s2.button("补发已有结果", key="send_done_%s" % job_id, use_container_width=True):
                        try:
                            res = retry_job_send(int(job_id), base_url=base)
                            if res.get("action") == "sent":
                                st.success("发送成功")
                                st.rerun()
                            else:
                                st.warning("发送失败：%s" % (res.get("send_result", {}).get("reason") or "未知"))
                        except ControlAPIError as e:
                            st.error("发送失败：%s" % (e,))
                    if s3.button("忽略", key="dismiss_done_%s" % job_id, use_container_width=True):
                        try:
                            res = dismiss_job(int(job_id), base_url=base)
                            if res.get("action") == "dismissed":
                                st.success("已忽略")
                                st.rerun()
                            else:
                                st.warning("忽略失败：%s" % (res.get("error") or "未知"))
                        except ControlAPIError as e:
                            st.error("忽略失败：%s" % (e,))
                    detail = st.session_state.get("agent_job_detail_%s" % job_id)
                    if detail:
                        with st.expander("完整任务 JSON", expanded=False):
                            st.json(detail)

    # Auto-refresh logic
    if auto_refresh:
        time.sleep(5)
        st.rerun()

    st.subheader("技术详情与排查")
    with st.expander("查看处理能力完整信息", expanded=False):
        st.caption("仅排查问题时使用，普通使用可忽略。")
        st.json(st.session_state.get("agent_last_health") or {})
    with st.expander("查看后台服务原始状态", expanded=False):
        st.caption("仅排查问题时使用，普通使用可忽略。")
        st.json({"worker": ws, "m5_async_loop": loop_state})
    with st.expander("查看任务技术详情（JSON）", expanded=False):
        st.caption("仅排查问题时使用，普通使用可忽略。")
        st.json(rows)


PAGES = [
    ("总览", _page_overview),
    ("监听目标", _page_targets),
    ("触发词", _page_triggers),
    ("知识库", _page_knowledge),
    ("Agent 任务", _page_agent_jobs),
]


def main():
    # 检查 base_url 是否变化
    current_url = st.session_state.get("base_url", DEFAULT_BASE_URL)
    last_url = st.session_state.get("last_base_url", current_url)
    if current_url != last_url:
        st.session_state.last_base_url = current_url
        _clear_data_cache()

    _load_data()
    _sidebar()
    st.sidebar.divider()
    choice = st.sidebar.radio("页面", [p[0] for p in PAGES], index=0)
    for name, fn in PAGES:
        if name == choice:
            fn()
            break


if __name__ == "__main__":
    main()
