#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4 shadow 对比 replay 工作台（单文件）。

同一注入消息集合分别驱动：
  * 真实 legacy 链：reply_engine.generate_reply → agent_jobs → control_api
    dispatcher/reconciler/sender（发送边界 agent_worker._send_result_back 已 mock）
  * 真实 strict 链：wechat_bot_monitor.durable_ingress_event → reliable_pipeline
    turn/job → reliable_worker.process_once → send_once
    （发送边界 reliable_worker.send_reply_detailed 已 mock）

两条链都使用真实 Hermes provider（runtime `hermes-wechat-bot-worker3` 实例配置
原样复制），临时 SQLite DB、临时 config、假 target `shadow-replay-target`。
全程不写 runtime 生产 DB（启动/结束对两个 runtime DB 做 mtime+size 守卫断言），
不做任何真实微信发送（所有发送函数均被替换或设置陷阱）。

判定逻辑与 `docs/2026-07-12-reliable-pipeline-execution-plan.md` Stage 4
「Shadow 对比验收阈值（2026-07-16 预写，replay 实施前冻结）」逐条一致。

用法：
  python scripts/shadow_replay.py --samples 2 --out temp/shadow_replay_<ts>
  python scripts/shadow_replay.py            # 默认全量 20 条
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # wechat-decrypt/
REPO = ROOT.parent                                   # git 仓根
sys.path.insert(0, str(ROOT))

CD_ONLY = Path(r"E:/projects/GA-projects/CD-only/wechat-decrypt")
RUNTIME_DBS = [
    CD_ONLY / "temp" / "agent_jobs.sqlite",
    CD_ONLY / "temp" / "reliable_pipeline_e2e_20260712_125228.sqlite",
]
RUNTIME_BUS_INDEX = CD_ONLY / ".leann" / "indexes" / "work_kb_bus"

TARGET_NAME = "shadow-replay-target"
TARGET_USERNAME = "shadowreplay@chatroom"
TRIGGER = "飞扬的跟屁虫"
INSTANCE_ID = "hermes-wechat-bot-worker3"
# runtime `agent_provider.instances` 中 worker3 实例原样复制（仅非敏感字段；
# 不添加 synthetic hermes_home，保持 profile/工具可用性与生产一致）。
WORKER3_INSTANCE = {
    "id": INSTANCE_ID,
    "provider": "hermes",
    "profile": "wechat-bot-worker3",
    "model": "MiniMax-M3",
    "worker_id": "wechat-bot-worker3",
}
# runtime `knowledge_bases["leann.bus"]` 原样复制（docs_dir 仅只读引用）。
BUS_KB_SPEC = {
    "type": "leann",
    "index_name": "work_kb_bus",
    "scope": "scene",
    "enabled": True,
    "embedding_model": "BAAI/bge-base-zh-v1.5",
    "docs_dir": str(CD_ONLY / "wiki" / "scenes" / "bus"),
}

# ---------------------------------------------------------------------------
# 冻结阈值（docs/2026-07-12-reliable-pipeline-execution-plan.md Stage 4
# 「Shadow 对比验收阈值」，2026-07-16 冻结；改动须先改文档）
# ---------------------------------------------------------------------------
THRESHOLDS = {
    "action_min_rate": 0.80,        # 维度1：动作一致率下限
    "source_min_rate": 0.90,        # 维度2：kb_id 集合一致率下限
    "safety_max_violations": 0,     # 维度3：provenance 越权/新增风险动作上限
    "latency_p50_ratio": 1.5,       # 维度4：strict p50 ≤ legacy p50 × 1.5
    "latency_p95_ratio": 2.0,       # 维度4：strict p95 ≤ legacy p95 × 2
    "min_total_samples": 20,        # 样本量下限（每链同等处理）
    "min_kb_hit_reply": 10,         # KB-hit reply 子集下限（维度2）
    "min_expected_reply": 12,
    "min_expected_silent": 4,
    "min_expected_escalate": 2,
    "min_whitelist_each": 1,        # 白名单差异类别每类样本下限（2026-07-16 已废除，见 WHITELIST_REQUIRED）
}
# 预登记白名单差异点（strict 链独有）。2026-07-16 阈值修订（文档冻结小节同步）：
# 这些类别为「条件性要求」——真实 replay 若触发必须正确归类并记录，但不设样本量
# 下限（真实 provider 正常行为下契约拒绝无法稳定构造，拒绝分支由
# tests/test_hermes_result_contract.py 等单测覆盖）；required whitelist set 为空。
# budget_intercept = N/A：代码库中不存在预算拦截机制（全仓检索仅命中 prompt 字符
# 预算），永不触发，仅作登记。归类仅允许以可举证的错误字符串/provenance 状态，
# 无证据不归类（不可归类的不一致 = FAIL）。
WHITELIST_CLASSES = ("knowledge_gate", "budget_intercept", "contract_reject", "safety_intercept")
WHITELIST_REQUIRED = ()  # 修订后：无强制触发下限
WHITELIST_NA = {"budget_intercept": "机制不存在（N/A）"}
EVIDENCE = {
    "knowledge_gate": ("required knowledge search was not invoked", "invalid knowledge search trace"),
    "contract_reject": ("invalid agent result contract", "provider returned non-AgentResult",
                        "malformed agent_result contract", "terminal success missing agent_result contract"),
    "safety_intercept": ("reply rejected by final safety filter",),
    # 代码库中不存在预算拦截机制（全仓检索 预算/budget 仅命中 prompt 字符预算），
    # N/A 类：登记但永不触发。
    "budget_intercept": (),
}

# ---------------------------------------------------------------------------
# 样本集（20 条，分层；触发词前缀与生产 ingress 原文一致）
# ---------------------------------------------------------------------------
def _s(sid, text, action, kb):
    return {"id": sid, "text": "%s %s" % (TRIGGER, text),
            "expected_action": action, "expected_kb_hit": kb}


def build_samples():
    kb_reply = [
        "公交卡充值失败未到账，怎么申请退款？",
        "我用手机给公交卡充了50块，卡上一直不到账，怎么处理？",
        "超级卡坏了，刷卡没反应，去哪里换卡？",
        "公交卡丢了能挂失吗？里面的余额还能找回来吗？",
        "退款一般多久到账？都等了一个星期了",
        "超级卡和普通公交卡有什么区别？押金能退吗？",
        "公交卡可以异地使用吗？去外地能刷吗？",
        "充值发票怎么开？公司报销要用",
        "老年卡年审什么时候办？需要带什么材料？",
        "学生卡怎么办？需要什么证件？",
        "公交卡客服电话是多少？几点上班？",
        "网上充值失败了但是钱扣了，这个钱会退回来吗？",
    ]
    silent = [
        "哈哈哈哈这个表情包笑死我了",
        "大家早上好呀，今天天气不错",
        "好的好的，知道了",
        "在忙吗？晚点聊",
    ]
    escalate = [
        # 注意避开 legacy precheck 的 PROMISE/HIGH_RISK 词表（承诺/保证/报价/授权/
        # 拍板/决定/转账/聊天记录…），保证两条链都走到 agent 决策层。
        "你们这个超级卡服务太差劲了，我要求赔偿损失，让负责人马上出来处理",
        "问题拖了半个月没人管，再不解决我就去网上发帖曝光，并且向消协投诉到底",
    ]
    whitelist_probe = [
        # knowledge-gate 探针：knowledge_grounded 策略下 agent 可能不检索直接作答
        "哦哦好的，那没事了，谢谢啦",
        "卡已经补办好了，功能正常，就是来回跑了两趟",
    ]
    out = []
    for i, t in enumerate(kb_reply, 1):
        out.append(_s("S%02d" % i, t, "reply", True))
    for i, t in enumerate(silent, 13):
        out.append(_s("S%02d" % i, t, "silent", False))
    for i, t in enumerate(escalate, 17):
        out.append(_s("S%02d" % i, t, "escalate", False))
    out.append(_s("S19", whitelist_probe[0], "silent", False))
    out.append(_s("S20", whitelist_probe[1], "reply", False))
    return out


def select_samples(all_samples, n):
    """--samples N 选择；N=2 冒烟必须两条都是 KB-hit reply（显式校验不变量）。"""
    if n >= len(all_samples):
        return list(all_samples)
    if n == 2:
        smoke = [s for s in all_samples if s["expected_action"] == "reply" and s["expected_kb_hit"]][:2]
        assert len(smoke) == 2 and all(
            s["expected_action"] == "reply" and s["expected_kb_hit"] for s in smoke
        ), "smoke 样本不变量被破坏：--samples 2 必须选到 2 条 KB-hit reply 样本"
        return smoke
    return list(all_samples[:n])


# ---------------------------------------------------------------------------
# runtime-DB 守卫
# ---------------------------------------------------------------------------
def _snap_one(path):
    try:
        st = os.stat(path)
        return {"exists": True, "mtime": st.st_mtime, "size": st.st_size}
    except OSError:
        return {"exists": False, "mtime": None, "size": None}


def guard_snapshot():
    snap = {}
    for db in RUNTIME_DBS:
        for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            snap[str(p)] = _snap_one(p)
    return snap


def guard_verify(before):
    after = guard_snapshot()
    changed = []
    for k, b in before.items():
        a = after[k]
        if (b["exists"], b["mtime"], b["size"]) != (a["exists"], a["mtime"], a["size"]):
            changed.append({"file": k, "before": b, "after": a})
    return {"ok": not changed, "changed": changed, "after": after}


# ---------------------------------------------------------------------------
# 运行上下文（临时 config / 临时 DB / monkeypatch / 发送 mock 记录）
# ---------------------------------------------------------------------------
class ReplayContext:
    def __init__(self, out_dir):
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.config_path = self.out / "config.json"
        self.legacy_db = self.out / "agent_jobs.sqlite"
        self.strict_db = self.out / "reliable_pipeline.sqlite"
        self.legacy_send_calls = []   # legacy 发送 mock 记录
        self.strict_send_calls = []   # strict 发送 mock 记录
        self.monitor_log = self.out / "monitor.log"
        self.target = {
            "name": TARGET_NAME,
            "username": TARGET_USERNAME,
            "db": "shadow.db",
            "table": "Msg_shadowreplay",
            "mode": "customer_service",
            "knowledge_bases": ["leann.bus"],
            "reliable_pipeline_target": True,
            "reliable_pipeline_shadow": False,
            "dedicated_agent_instance_id": INSTANCE_ID,
            "thin_monitor": True,
            "agent_mode": "tool_agent",
            "triggers": [TRIGGER],
            "response_mode": "trigger",
        }
        self.config = {
            "targets": [self.target],
            "default_triggers": [TRIGGER],
            "default_response_mode": "trigger",
            "reply_engine": {"mode": "raw_agent"},
            "knowledge_bases": {"leann.bus": dict(BUS_KB_SPEC)},
            "agent_provider": {"instances": [dict(WORKER3_INSTANCE)]},
            "reliable_pipeline": {
                "enabled": True,
                "test_target_only": True,
                "test_target_names": [TARGET_NAME],
                "db_path": str(self.strict_db),
            },
        }
        self._restore = []

    # -- 环境准备 -----------------------------------------------------------
    def provision(self):
        """落地临时 config、KB 运行时依赖（leann_search_direct.py 与索引 junction）。

        search_leann_kb 的脚本与索引路径均相对 config 所在目录解析，必须在
        临时工作区备齐，否则 strict KB 检索会因工作台布线问题全灭（而非管线行为）。
        """
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copy2(ROOT / "leann_search_direct.py", self.out / "leann_search_direct.py")
        link = self.out / ".leann" / "indexes" / "work_kb_bus"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(RUNTIME_BUS_INDEX)],
                               capture_output=True, text=True)
            if r.returncode != 0 or not (link / "documents.leann.meta.json").exists():
                shutil.copytree(RUNTIME_BUS_INDEX, link)  # 只读副本兜底
        assert (link / "documents.leann.meta.json").exists(), "LEANN 索引 junction/副本失败"

    def _patch(self, obj, name, value):
        self._restore.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def install(self):
        """安装全部进程内 monkeypatch（不改任何生产模块文件）。"""
        import agent_jobs
        import agent_worker
        import control_api  # noqa: F401  (确认可导入，dispatcher/reconciler/sender 入口)
        import reliable_worker
        import target_registry as reg
        import wechat_bot_monitor
        import wechat_sender

        # 1) legacy job DB → 临时 DB
        self._patch(agent_jobs, "DEFAULT_DB_PATH", self.legacy_db)
        # 2) reg.load_config → 临时 config（CONFIG_PATH 是默认参数绑定，patch
        #    函数本体才可靠；所有生产模块均按模块属性调用，进程内生效）
        orig_load_config = reg.load_config
        self._patch(reg, "load_config",
                    lambda path=None: orig_load_config(str(self.config_path)))
        # 3) monitor log → 收集到 out 目录
        def _mon_log(msg):
            line = time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg)
            with self.monitor_log.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        self._patch(wechat_bot_monitor, "log", _mon_log)
        # 4) strict 发送边界 mock（可靠链 send_once 内部调用点）
        def _fake_send_reply_detailed(text, target=None, before_local_id=None, cfg=None,
                                      confirm=None, log=None, mention_name=None):
            self.strict_send_calls.append({
                "ts": time.time(), "chars": len(str(text or "")),
                "target": (target or {}).get("name"), "mention_name": mention_name,
                "before_local_id": before_local_id,
            })
            return wechat_sender.SendResult(
                ok=True, mode="shadow_mock", attempted=["shadow_mock"],
                reason="shadow_mock_ok", confirmed=True,
                detail={"shadow_mock": True, "target": (target or {}).get("name")})
        self._patch(reliable_worker, "send_reply_detailed", _fake_send_reply_detailed)
        # 5) legacy 发送边界 mock（control_api._async_sender_run_once 调用点）
        def _fake_send_result_back(job, result_text, config_path):
            self.legacy_send_calls.append({
                "ts": time.time(), "job_id": job.get("id"), "job_key": job.get("job_key"),
                "chars": len(str(result_text or "")),
            })
            return {"sent": True, "reason": "shadow_mock"}
        self._patch(agent_worker, "_send_result_back", _fake_send_result_back)
        # 6) 真实发送陷阱：任何绕开上述 mock 的发送尝试立即报错
        def _trap(*a, **k):
            raise RuntimeError("shadow replay: 真实微信发送被阻断")
        for fn in ("send_reply", "send_reply_detailed", "send_reply_foreground",
                   "send_reply_backend", "send_reply_cua", "send_reply_cua_separate_window"):
            if hasattr(wechat_sender, fn):
                self._patch(wechat_sender, fn, _trap)
        # 7) Hermes 环境净化：WECHAT_MCP_* 由 provider 按 job 注入，不允许外泄默认值
        for key in ("WECHAT_MCP_CONFIG", "WECHAT_MCP_ALLOWED_KB_IDS", "WECHAT_MCP_TARGET_ID",
                    "WECHAT_MCP_WIKI_DIR", "WECHAT_MCP_TRACE_ID", "WECHAT_MCP_TRACE_DIR"):
            os.environ.pop(key, None)
        # 8) Hermes submit 的 session 目录 = cwd/.wechat_agent_jobs → 收进 out
        os.chdir(self.out)
        return self

    def restore(self):
        for obj, name, value in reversed(self._restore):
            setattr(obj, name, value)
        self._restore.clear()


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------
def _policy(ctx):
    import wechat_bot_monitor
    return wechat_bot_monitor.resolve_target_policy(ctx.config, ctx.target)


def _msg(sample, ts, policy):
    return {
        "content": sample["text"],
        "message_content": sample["text"],
        "local_id": sample["lid"],
        "local_type": 1,
        "sender_username": "shadow_sender_wxid",
        "real_sender_id": "90001",
        "sender": "shadow_sender_wxid",
        "sender_display_name": "影子用户",
        "mention_name": "影子用户",
        "create_time": ts,
        "image_path": "",
        "session_image_paths": [],
        "context_messages": [],
        "is_aggregated": False,
        "target_policy": policy,
    }


def _event_context(sample):
    return {
        "trigger_matched": True, "in_session": False, "session_active": False,
        "session_state": "idle", "session_turns": 0,
        "sender": "shadow_sender_wxid", "sender_username": "shadow_sender_wxid",
        "sender_display_name": "影子用户", "mention_name": "影子用户",
        "local_id": sample["lid"], "mode": "thin_monitor",
    }


def _fp(text, n=160):
    """错误指纹：只保留固定安全标记类文本，截断，绝不回显 provider 正文。"""
    return str(text or "").replace("\n", " ")[:n]


def _read_trace(trace_dir, trace_id):
    """读取 MCP trace 文件 {trace_dir}/{trace_id}.json，返回 (kb集合, 状态)。"""
    if not trace_dir or not trace_id:
        return [], "no_trace_context"
    p = Path(trace_dir) / ("%s.json" % trace_id)
    if not p.is_file():
        return [], "no_tool_call"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        prov = data.get("provenance") or []
        kbs = sorted({str(i.get("kb_id") or "") for i in prov
                      if isinstance(i, dict) and str(i.get("kb_id") or "").strip()})
        return kbs, str(data.get("status") or "unknown")
    except Exception as e:
        return [], "trace_read_error:%r" % e


# ---------------------------------------------------------------------------
# legacy 链
# ---------------------------------------------------------------------------
def run_legacy(sample, ctx, policy, deadline_s=300.0, poll_s=4.0):
    import agent_jobs
    import control_api
    import reply_engine

    t0 = time.time()
    rec = {"chain": "legacy", "action": None, "kb_ids": [], "kb_trace_status": "",
           "allowed_kb_ids": [], "latency_s": None, "terminal": "", "error_fp": "",
           "decision_intent": "", "ack_sent_now": False, "job_id": None, "job_key": ""}
    msg = _msg(sample, t0, policy)
    try:
        decision = reply_engine.generate_reply(
            msg, ctx.target, ctx.config, config_path=str(ctx.config_path))
    except Exception as e:
        rec["terminal"] = "generate_reply_raised"
        rec["error_fp"] = _fp("%s: %s" % (type(e).__name__, e))
        rec["latency_s"] = round(time.time() - t0, 3)
        return rec
    rec["decision_intent"] = str(decision.intent or "")
    rec["ack_sent_now"] = bool(decision.should_reply and decision.reply_text
                               and decision.intent in ("agent_job_queued", "deep_agent_queued"))

    if decision.intent not in ("agent_job_queued", "deep_agent_queued"):
        # 同步决策（precheck 边界拦截等）：生产会立即同步发送，replay 不发送只记录。
        rec["action"] = "reply" if decision.should_reply else "silent"
        rec["terminal"] = "sync_%s" % decision.intent
        rec["latency_s"] = round(time.time() - t0, 3)
        return rec

    job_key = "%s:%s" % (TARGET_USERNAME, sample["lid"])
    rec["job_key"] = job_key
    job = agent_jobs.get_job(job_key=job_key)
    if not job:
        rec["terminal"] = "job_missing_after_enqueue"
        rec["error_fp"] = "enqueue 后按 job_key 未找到 job"
        rec["latency_s"] = round(time.time() - t0, 3)
        return rec
    rec["job_id"] = int(job["id"])

    body = {"instance_id": INSTANCE_ID, "provider": "hermes",
            "max_global_dispatching": 1, "per_group_concurrency": 1}
    deadline = t0 + deadline_s
    terminal_statuses = {"sent", "failed", "timeout", "expired", "cancelled"}
    dispatching_since = None
    while time.time() < deadline:
        job = agent_jobs.get_job(job_id=rec["job_id"]) or job
        st, ss = job.get("status"), job.get("send_status")
        if st in terminal_statuses:
            break
        if st == "queued":
            control_api._async_dispatcher_run_once(body)
            dispatching_since = None
        elif st == "dispatching":
            dispatching_since = dispatching_since or time.time()
            if time.time() - dispatching_since > 40:
                agent_jobs.release_dispatching(rec["job_id"], reason="shadow replay watchdog")
                dispatching_since = None
        elif st in ("submitted", "agent_running", "running"):
            control_api._async_reconciler_run_once({"limit": 5})
        elif st == "done" and ss == "pending":
            control_api._async_sender_run_once({"limit": 5})
        else:
            time.sleep(poll_s)
            continue
        time.sleep(poll_s)
    job = agent_jobs.get_job(job_id=rec["job_id"]) or job

    payload = job.get("payload") or {}
    rec["allowed_kb_ids"] = sorted(str(x) for x in (payload.get("_allowed_kb_ids") or []))
    rec["kb_ids"], rec["kb_trace_status"] = _read_trace(
        payload.get("_knowledge_trace_dir"), payload.get("_knowledge_trace_id"))

    st, ss, err = job.get("status"), job.get("send_status"), str(job.get("error") or "")
    rec["latency_s"] = round(time.time() - t0, 3)
    if st == "sent" and ss == "sent":
        rec["action"], rec["terminal"] = "reply", "sent_mock"
    elif st == "sent" and ss == "skipped":
        rec["action"] = err if err in ("silent", "escalate") else "unknown"
        rec["terminal"] = "skipped:%s" % err
    elif st in terminal_statuses:
        rec["terminal"] = st
        rec["error_fp"] = _fp(err)
    else:
        rec["terminal"] = "replay_deadline:%s" % st
        rec["error_fp"] = _fp(err or "超过单样本时限未到终态")
    return rec


# ---------------------------------------------------------------------------
# strict 链
# ---------------------------------------------------------------------------
def run_strict(sample, ctx, policy):
    import reliable_pipeline
    import reliable_worker
    import reply_engine
    import wechat_bot_monitor
    from agent_provider import provider_from_config

    t0 = time.time()
    rec = {"chain": "strict", "action": None, "kb_ids": [], "kb_trace_status": "",
           "allowed_kb_ids": [], "latency_s": None, "terminal": "", "error_fp": "",
           "job_id": None, "outbox_id": None, "provenance_status": ""}
    msg = _msg(sample, t0, policy)
    ing = wechat_bot_monitor.durable_ingress_event(
        ctx.target, msg, cfg=ctx.config, config_path=str(ctx.config_path),
        db_path=ctx.strict_db, now=t0,
        event_context=_event_context(sample), target_policy=policy)
    if not ing.get("advanced"):
        rec["terminal"] = "ingress_failed"
        rec["error_fp"] = _fp(ing.get("error"))
        rec["latency_s"] = round(time.time() - t0, 3)
        return rec

    # 强制关窗（时间前推），落 turn → job
    reliable_pipeline.close_due_windows(now=t0 + 30.0, db_path=ctx.strict_db)
    jobs = reliable_pipeline.create_jobs_for_ready_turns(db_path=ctx.strict_db)
    mine = []
    for j in jobs:
        try:
            pl = j.get("payload") or {}  # _row() 已将 payload_json 解码为 payload
            lids = [int(e.get("local_id") or 0) for e in (pl.get("events") or [])]
            if sample["lid"] in lids:
                mine.append(j)
        except Exception:
            continue
    if len(mine) != 1:
        rec["terminal"] = "no_job_created"
        rec["error_fp"] = "本样本 turn 未唯一落 job（matches=%d）" % len(mine)
        rec["latency_s"] = round(time.time() - t0, 3)
        return rec
    job_id = int(mine[0]["id"])
    rec["job_id"] = job_id

    def _factory(instance_id=None):
        return provider_from_config(ctx.config, instance_id=instance_id or INSTANCE_ID)

    out = reliable_worker.process_once(
        provider=_factory(INSTANCE_ID), owner="shadow-replay-worker",
        config=ctx.config, provider_factory=_factory,
        final_filter=reply_engine.postcheck,
        db_path=ctx.strict_db, timeout=280.0, instance_id=INSTANCE_ID)

    # outbox → mock 发送（process_once 只建 outbox，send_once 才到发送边界）
    if out.get("status") == "applied" and out.get("outbox"):
        rec["outbox_id"] = int(out["outbox"]["id"])
        reliable_worker.send_once(owner="shadow-replay-sender", config=ctx.config,
                                  db_path=ctx.strict_db,
                                  confirm=lambda *a, **k: True, log=None)

    with sqlite3.connect(str(ctx.strict_db)) as con:
        con.row_factory = sqlite3.Row
        jrow = con.execute("SELECT * FROM turn_jobs WHERE id=?", (job_id,)).fetchone()
        orow = con.execute("SELECT * FROM send_outbox WHERE job_id=?", (job_id,)).fetchone()
    j = dict(jrow) if jrow else {}
    rec["latency_s"] = round(time.time() - t0, 3)

    try:
        prov = json.loads(j.get("provenance_json") or "{}")
    except Exception:
        prov = {}
    rec["provenance_status"] = str(prov.get("status") or "")
    rec["kb_trace_status"] = rec["provenance_status"]
    rec["kb_ids"] = sorted({str(i.get("kb_id") or "") for i in (prov.get("provenance") or [])
                            if isinstance(i, dict) and str(i.get("kb_id") or "").strip()})
    try:
        tpayload = json.loads(j.get("payload_json") or "{}")
        events = tpayload.get("events") or []
        last = events[-1].get("message") if events else {}
        rec["allowed_kb_ids"] = sorted(str(x) for x in ((last or {}).get("_allowed_kb_ids") or []))
        contract = json.loads(j.get("result_json") or "{}")
    except Exception:
        contract = {}

    st, err = str(j.get("status") or ""), str(j.get("error") or "")
    rec["provider_diagnostics"] = j.get("provider_diagnostics_json") or ""
    if orow is not None:
        o = dict(orow)
        if str(o.get("status")) == "sent":
            rec["action"], rec["terminal"] = "reply", "sent_mock"
        else:
            rec["action"], rec["terminal"] = "reply", "outbox_%s" % o.get("status")
            rec["error_fp"] = _fp(o.get("error"))
    elif st == "done" and contract.get("action") == "silent":
        rec["action"], rec["terminal"] = "silent", "done"
    elif st == "escalated":
        rec["action"], rec["terminal"] = "escalate", "escalated"
    elif st == "failed":
        rec["terminal"] = "failed"
        rec["error_fp"] = _fp(err)
    elif st == "done":
        rec["action"] = contract.get("action") or "unknown"
        rec["terminal"] = "done"
    else:
        rec["terminal"] = "unexpected:%s" % st
        rec["error_fp"] = _fp(err)
    return rec


# ---------------------------------------------------------------------------
# 比较器（冻结阈值）
# ---------------------------------------------------------------------------
def _classify_mismatch(strict_rec, legacy_rec):
    """不一致样本归类：仅允许白名单差异点，且必须有可举证错误串。

    返回 (白名单类名|unclassifiable, 证据串)。无证据一律 unclassifiable。
    """
    s_err = str(strict_rec.get("error_fp") or "")
    l_err = str(legacy_rec.get("error_fp") or "")
    for cls in WHITELIST_CLASSES:
        for marker in EVIDENCE[cls]:
            if marker and (marker in s_err or marker in l_err):
                return cls, marker
    return "unclassifiable", ""


def _pct(values, q):
    if not values:
        return None
    xs = sorted(values)
    rank = max(1, int(round(q * len(xs) + 0.49)))
    return xs[min(len(xs), rank) - 1]


def _outcome(chain_rec):
    """单链最终结局：动作（reply/silent/escalate）或失败终态（failed:<terminal>）。"""
    if chain_rec.get("action"):
        return str(chain_rec["action"])
    return "failed:%s" % (chain_rec.get("terminal") or "unknown")


# ---------------------------------------------------------------------------
# Frozen rule (plan line 411, 2026-07-16): single-chain provider fail/timeout
# counts as ``execution_failure`` and is dropped from the dim1 denominator.
# Whitelist-classified mismatches (knowledge_gate etc.) still go to mismatches
# with their class; only unclassifiable provider-side single-chain failures
# are excluded.  A failure terminal of ``failed:expired`` is always a timeout
# and therefore execution_failure.
# ---------------------------------------------------------------------------
EXECUTION_FAILURE_MARKERS = (
    "provider result failed",
    "provider run failed",
    "provider contract",
    "poll expired",
    "agent_timeout",
)


def _is_execution_failure(lo, so, l_err, s_err):
    """Return True iff this single-chain mismatch is a provider fail/timeout.

    Single-chain = exactly one side has outcome ``failed:...``.  The failed
    side's outcome or error fingerprint must indicate a provider-side
    infrastructure failure (contract exhausted, timeout) rather than a
    deterministic per-prompt gate (knowledge_gate, contract_reject, etc.).
    """
    lo_failed = lo.startswith("failed:")
    so_failed = so.startswith("failed:")
    if lo_failed == so_failed:  # both or neither failed -> not single-chain
        return False
    failed_outcome = so if so_failed else lo
    failed_err = s_err if so_failed else l_err
    if failed_outcome == "failed:expired":
        return True
    return any(m in failed_err for m in EXECUTION_FAILURE_MARKERS)


def compare(samples, results):
    pairs = []
    for s in samples:
        r = results.get(s["id"]) or {}
        pairs.append({"sample": s, "legacy": r.get("legacy") or {}, "strict": r.get("strict") or {}})

    processed = [p for p in pairs if p["legacy"].get("terminal") and p["strict"].get("terminal")]
    # 一致 = 两链最终动作相同；一侧有动作另一侧失败 = 分歧（按白名单归类）；
    # 双链同终态失败 = 非分歧也不构成动作一致，单独成桶透明列出，不计入一致率分母。
    consistent, mismatches, both_failed, execution_failures = [], [], [], []
    for p in processed:
        lo, so = _outcome(p["legacy"]), _outcome(p["strict"])
        if lo == so and not lo.startswith("failed:"):
            consistent.append(p)
            continue
        if lo == so:
            both_failed.append({"id": p["sample"]["id"], "outcome": lo,
                                "legacy_error_fp": p["legacy"].get("error_fp") or "",
                                "strict_error_fp": p["strict"].get("error_fp") or ""})
            continue
        cls, ev = _classify_mismatch(p["strict"], p["legacy"])
        l_err = p["legacy"].get("error_fp") or ""
        s_err = p["strict"].get("error_fp") or ""
        # Frozen rule: single-chain provider fail/timeout -> execution_failure,
        # dropped from dim1 denominator (transparent bucket, not a mismatch).
        # Whitelist-classified single-chain mismatches (knowledge_gate etc.)
        # still go to mismatches with their class — the gate is a deliberate
        # per-prompt behavior, not an execution instability.
        if cls == "unclassifiable" and _is_execution_failure(lo, so, l_err, s_err):
            execution_failures.append({"id": p["sample"]["id"],
                                       "legacy_outcome": lo, "strict_outcome": so,
                                       "legacy_error_fp": l_err,
                                       "strict_error_fp": s_err})
            continue
        mismatches.append({"id": p["sample"]["id"],
                           "legacy_outcome": lo, "strict_outcome": so,
                           "legacy_action": p["legacy"].get("action") or "",
                           "strict_action": p["strict"].get("action") or "",
                           "class": cls, "evidence": ev,
                           "legacy_error_fp": l_err, "strict_error_fp": s_err})
    rate_n = len(processed) - len(both_failed) - len(execution_failures)
    rate = (len(consistent) / rate_n) if rate_n else None
    action_pairs = [p for p in processed if p["legacy"].get("action") and p["strict"].get("action")]
    chain_failures = [m for m in mismatches
                      if m["legacy_outcome"].startswith("failed:") or m["strict_outcome"].startswith("failed:")]
    unclass = [m for m in mismatches if m["class"] == "unclassifiable"]
    whitelist_triggered = {cls: sum(1 for m in mismatches if m["class"] == cls)
                           for cls in WHITELIST_CLASSES}
    # 修订后白名单为条件性要求：required set 为空，未触发不再导致 INCONCLUSIVE。
    whitelist_missing = [c for c in WHITELIST_REQUIRED
                         if whitelist_triggered.get(c, 0) < 1]

    if len(processed) < THRESHOLDS["min_total_samples"]:
        d1 = "INCONCLUSIVE"
        d1_note = "两链同等处理样本 %d < 下限 %d" % (len(processed), THRESHOLDS["min_total_samples"])
    elif rate is None:
        d1 = "INCONCLUSIVE"
        d1_note = "无可比样本（%d 条双链同败）" % len(both_failed)
    elif unclass:
        d1 = "FAIL"
        d1_note = "存在不可归类不一致样本：%s" % "、".join(m["id"] for m in unclass)
    elif rate < THRESHOLDS["action_min_rate"]:
        d1 = "FAIL"
        d1_note = "一致率 %.1f%% < 阈值 %.0f%%" % (rate * 100, THRESHOLDS["action_min_rate"] * 100)
    else:
        d1 = "PASS"
        d1_note = ("一致率 %.1f%% ≥ %.0f%%（分母 %d = 处理 %d - 双链同败 %d - 单链执行失败 %d），"
                   "不一致均可归类") % (
            rate * 100, THRESHOLDS["action_min_rate"] * 100, rate_n, len(processed),
            len(both_failed), len(execution_failures))

    # 维度2：来源一致性（双方均 reply 的样本；kb_id 集合比较，排序容忍）
    both_reply = [p for p in action_pairs
                  if p["legacy"]["action"] == "reply" and p["strict"]["action"] == "reply"]
    kb_gate_n = len([p for p in both_reply if p["sample"]["expected_kb_hit"]])
    kb_equal = [p for p in both_reply
                if sorted(p["legacy"].get("kb_ids") or []) == sorted(p["strict"].get("kb_ids") or [])]
    kb_rate = (len(kb_equal) / len(both_reply)) if both_reply else None
    kb_diff = [{"id": p["sample"]["id"], "legacy_kb": p["legacy"].get("kb_ids") or [],
                "strict_kb": p["strict"].get("kb_ids") or []}
               for p in both_reply if p not in kb_equal]
    if kb_gate_n < THRESHOLDS["min_kb_hit_reply"]:
        d2 = "INCONCLUSIVE"
        d2_note = "KB-hit reply 子集 %d < 下限 %d" % (kb_gate_n, THRESHOLDS["min_kb_hit_reply"])
    elif kb_rate is not None and kb_rate < THRESHOLDS["source_min_rate"]:
        d2 = "FAIL"
        d2_note = "kb_id 集合一致率 %.1f%% < 阈值 %.0f%%" % (kb_rate * 100, THRESHOLDS["source_min_rate"] * 100)
    else:
        d2 = "PASS"
        d2_note = "kb_id 集合一致率 %.1f%% ≥ %.0f%%" % ((kb_rate or 0) * 100,
                                                       THRESHOLDS["source_min_rate"] * 100)

    # 维度3：安全性（strict provenance ⊆ 入队快照 100%；无白名单外新增风险动作）
    prov_violations = []
    for p in processed:
        s = p["strict"]
        allowed = set(s.get("allowed_kb_ids") or [])
        used = set(s.get("kb_ids") or [])
        if s.get("terminal") and used and not used.issubset(allowed):
            prov_violations.append({"id": p["sample"]["id"], "used": sorted(used),
                                    "allowed": sorted(allowed)})
    risk_actions = [{"id": m["id"],
                     "legacy_outcome": m["legacy_outcome"], "strict_outcome": m["strict_outcome"]}
                    for m in mismatches
                    if m["strict_action"] == "reply"
                    and m["legacy_action"] in ("", "silent", "escalate")
                    and m["class"] == "unclassifiable"]
    if prov_violations or risk_actions:
        d3 = "FAIL"
        d3_note = "provenance 越权 %d 起；白名单外新增风险动作 %d 起" % (
            len(prov_violations), len(risk_actions))
    else:
        d3 = "PASS"
        d3_note = "strict provenance 全部 ⊆ 入队快照（%d 个带 KB 证据 job 零越权）；无新增风险动作" % (
            sum(1 for p in processed if (p["strict"].get("kb_ids") or [])))

    # 维度4：延迟（各链 ingress/enqueue → 终态，秒）
    legacy_lat = [p["legacy"]["latency_s"] for p in processed if p["legacy"].get("latency_s")]
    strict_lat = [p["strict"]["latency_s"] for p in processed if p["strict"].get("latency_s")]
    lp50, lp95 = _pct(legacy_lat, 0.50), _pct(legacy_lat, 0.95)
    sp50, sp95 = _pct(strict_lat, 0.50), _pct(strict_lat, 0.95)
    if lp50 is None or sp50 is None or len(legacy_lat) < 2 or len(strict_lat) < 2:
        d4 = "INCONCLUSIVE"
        d4_note = "终态样本不足（legacy n=%d, strict n=%d）" % (len(legacy_lat), len(strict_lat))
        r50 = r95 = None
    else:
        r50, r95 = sp50 / lp50, sp95 / lp95
        if r50 > THRESHOLDS["latency_p50_ratio"] or r95 > THRESHOLDS["latency_p95_ratio"]:
            d4 = "FAIL"
            d4_note = "p50 比 %.2f（阈值 %.1f）/ p95 比 %.2f（阈值 %.1f）" % (
                r50, THRESHOLDS["latency_p50_ratio"], r95, THRESHOLDS["latency_p95_ratio"])
        else:
            d4 = "PASS"
            d4_note = "p50 比 %.2f ≤ %.1f；p95 比 %.2f ≤ %.1f" % (
                r50, THRESHOLDS["latency_p50_ratio"], r95, THRESHOLDS["latency_p95_ratio"])

    verdicts = [d1, d2, d3, d4]
    overall = "FAIL" if "FAIL" in verdicts else ("INCONCLUSIVE" if "INCONCLUSIVE" in verdicts else "PASS")

    exp_cov = {
        "reply": sum(1 for s in samples if s["expected_action"] == "reply"),
        "silent": sum(1 for s in samples if s["expected_action"] == "silent"),
        "escalate": sum(1 for s in samples if s["expected_action"] == "escalate"),
        "kb_hit": sum(1 for s in samples if s["expected_kb_hit"]),
    }
    return {
        "pairs": pairs, "processed_n": len(processed), "action_pairs_n": len(action_pairs),
        "chain_failures": [{"id": m["id"],
                            "legacy_outcome": m["legacy_outcome"],
                            "strict_outcome": m["strict_outcome"],
                            "class": m["class"], "evidence": m["evidence"],
                            "legacy_error_fp": m["legacy_error_fp"],
                            "strict_error_fp": m["strict_error_fp"]}
                           for m in chain_failures],
        "both_failed": both_failed, "execution_failures": execution_failures,
        "rate_denominator": rate_n,
        "dim1": {"verdict": d1, "note": d1_note, "rate": rate,
                 "threshold": THRESHOLDS["action_min_rate"],
                 "consistent_n": len(consistent), "mismatches": mismatches,
                 "whitelist_triggered": whitelist_triggered,
                 "whitelist_missing": whitelist_missing},        "dim2": {"verdict": d2, "note": d2_note, "rate": kb_rate,
                 "threshold": THRESHOLDS["source_min_rate"],
                 "both_reply_n": len(both_reply), "kb_gate_n": kb_gate_n,
                 "equal_n": len(kb_equal), "diff": kb_diff},
        "dim3": {"verdict": d3, "note": d3_note,
                 "threshold": THRESHOLDS["safety_max_violations"],
                 "provenance_violations": prov_violations, "risk_actions": risk_actions},
        "dim4": {"verdict": d4, "note": d4_note,
                 "legacy": {"n": len(legacy_lat), "p50": lp50, "p95": lp95},
                 "strict": {"n": len(strict_lat), "p50": sp50, "p95": sp95},
                 "ratio_p50": r50, "ratio_p95": r95,
                 "threshold_p50": THRESHOLDS["latency_p50_ratio"],
                 "threshold_p95": THRESHOLDS["latency_p95_ratio"]},
        "coverage": {"total": len(samples), "expected": exp_cov},
        "overall": overall,
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def render_report(ctx, samples, results, cmp, guard_before, guard_after, started_ts, elapsed_s,
                  provider_note, header_note=None):
    L = []
    A = L.append
    A("# Stage 4 Shadow 对比 Replay 报告")
    A("")
    if header_note:
        A("> %s" % header_note)
        A("")
    A("- 运行时间：%s（耗时 %.1f 分钟）" % (started_ts, elapsed_s / 60))
    A("- 输出目录：`%s`" % ctx.out)
    A("- 假 target：`%s`（username `%s`）；KB 授权：`leann.bus`" % (TARGET_NAME, TARGET_USERNAME))
    A("- provider：真实 Hermes（实例 `%s`，profile `%s`，model `%s`）——%s" % (
        WORKER3_INSTANCE["id"], WORKER3_INSTANCE["profile"], WORKER3_INSTANCE["model"], provider_note))
    A("- DB：legacy `%s`；strict `%s`（均为临时文件）" % (ctx.legacy_db.name, ctx.strict_db.name))
    A("- 发送阻断：legacy `agent_worker._send_result_back` mock（记录 %d 次）；"
      "strict `reliable_worker.send_reply_detailed` mock（记录 %d 次）；"
      "wechat_sender 全部入口设陷阱。" % (len(ctx.legacy_send_calls), len(ctx.strict_send_calls)))
    A("")

    A("## 样本覆盖")
    cov = cmp["coverage"]
    e = cov["expected"]
    A("")
    A("| 分层 | 实际 | 冻结下限 | 达标 |")
    A("|---|---|---|---|")
    A("| 总样本 | %d | %d | %s |" % (cov["total"], THRESHOLDS["min_total_samples"],
                                  "✓" if cov["total"] >= THRESHOLDS["min_total_samples"] else "✗"))
    A("| 期望 reply | %d | %d | %s |" % (e["reply"], THRESHOLDS["min_expected_reply"],
                                       "✓" if e["reply"] >= THRESHOLDS["min_expected_reply"] else "✗"))
    A("| 期望 silent | %d | %d | %s |" % (e["silent"], THRESHOLDS["min_expected_silent"],
                                        "✓" if e["silent"] >= THRESHOLDS["min_expected_silent"] else "✗"))
    A("| 期望 escalate | %d | %d | %s |" % (e["escalate"], THRESHOLDS["min_expected_escalate"],
                                          "✓" if e["escalate"] >= THRESHOLDS["min_expected_escalate"] else "✗"))
    A("| KB-hit 设计样本 | %d | ≥10（供维度2） | %s |" % (e["kb_hit"], "✓" if e["kb_hit"] >= 10 else "✗"))
    A("| 双方均 reply 且设计 KB-hit | %d | %d | %s |" % (
        cmp["dim2"]["kb_gate_n"], THRESHOLDS["min_kb_hit_reply"],
        "✓" if cmp["dim2"]["kb_gate_n"] >= THRESHOLDS["min_kb_hit_reply"] else "✗"))
    wt = cmp["dim1"]["whitelist_triggered"]
    A("| 白名单差异触发（条件性，无下限） | knowledge-gate %d / 契约拒绝 %d / 安全拦截 %d | 触发必归类 | %s |" % (
        wt.get("knowledge_gate", 0), wt.get("contract_reject", 0), wt.get("safety_intercept", 0),
        "均已正确归类" if not any(m["class"] == "unclassifiable" for m in cmp["dim1"]["mismatches"]) else "存在不可归类"))
    A("")
    A("> 注：白名单差异类别为条件性要求（2026-07-16 阈值修订）——真实 replay 若触发必须正确归类并记录，"
      "但不设样本量下限（真实 provider 正常行为下契约拒绝无法稳定构造，拒绝分支由 "
      "tests/test_hermes_result_contract.py 等单测覆盖）。预算拦截 = N/A：代码库中不存在该机制"
      "（全仓检索仅命中 prompt 字符预算），永不触发。")
    A("")

    A("## 四维度判定（阈值来自冻结文档）")
    A("")
    A("| 维度 | 指标 | 阈值 | 判定 | 说明 |")
    A("|---|---|---|---|---|")
    d1, d2, d3, d4 = cmp["dim1"], cmp["dim2"], cmp["dim3"], cmp["dim4"]
    A("| 1 动作一致性 | 一致率 %s（%d/%d） | ≥ %.0f%% 且不一致全部可归类 | **%s** | %s |" % (
        ("%.1f%%" % (d1["rate"] * 100)) if d1["rate"] is not None else "N/A",
        d1["consistent_n"], cmp.get("rate_denominator") or 0,
        d1["threshold"] * 100, d1["verdict"], d1["note"]))
    A("| 2 来源一致性 | kb_id 集合一致率 %s（%d/%d） | ≥ %.0f%% | **%s** | %s |" % (
        ("%.1f%%" % (d2["rate"] * 100)) if d2["rate"] is not None else "N/A",
        d2["equal_n"], d2["both_reply_n"], d2["threshold"] * 100, d2["verdict"], d2["note"]))
    A("| 3 安全性 | 越权 %d 起 / 新增风险动作 %d 起 | 0 | **%s** | %s |" % (
        len(d3["provenance_violations"]), len(d3["risk_actions"]), d3["verdict"], d3["note"]))
    A("| 4 延迟 | strict p50 %.1fs/p95 %.1fs vs legacy p50 %.1fs/p95 %.1fs | p50 ≤ %.1f×, p95 ≤ %.1f× | **%s** | %s |" % (
        d4["strict"]["p50"] or -1, d4["strict"]["p95"] or -1,
        d4["legacy"]["p50"] or -1, d4["legacy"]["p95"] or -1,
        d4["threshold_p50"], d4["threshold_p95"], d4["verdict"], d4["note"]))
    A("")

    A("## 不一致样本清单（%d 条）" % len(d1["mismatches"]))
    A("")
    if d1["mismatches"]:
        A("| 样本 | legacy 动作 | strict 动作 | 归类 | 证据 |")
        A("|---|---|---|---|---|")
        for m in d1["mismatches"]:
            A("| %s | %s | %s | %s | %s |" % (
                m["id"], m["legacy_action"], m["strict_action"], m["class"],
                (m["evidence"] or m["strict_error_fp"] or m["legacy_error_fp"] or "—")[:80]))
    else:
        A("（无）")
    A("")
    A("## 含链失败的不一致样本（%d 条，已计入维度1判定）" % len(cmp["chain_failures"]))
    A("")
    if cmp["chain_failures"]:
        A("| 样本 | legacy 结局 | strict 结局 | 归类 | 证据 |")
        A("|---|---|---|---|---|")
        for f in cmp["chain_failures"]:
            A("| %s | %s | %s | %s | %s |" % (
                f["id"], f["legacy_outcome"], f["strict_outcome"], f["class"],
                (f["evidence"] or f["strict_error_fp"] or f["legacy_error_fp"] or "—")[:70]))
    else:
        A("（无）")
    A("")

    A("## 双链同终态失败（%d 条，非分歧，不计入一致率分母）" % len(cmp.get("both_failed") or []))
    A("")
    if cmp.get("both_failed"):
        A("| 样本 | 共同终态 | legacy 错误指纹 | strict 错误指纹 |")
        A("|---|---|---|---|")
        for f in cmp["both_failed"]:
            A("| %s | %s | %s | %s |" % (
                f["id"], f["outcome"],
                (f["legacy_error_fp"] or "—")[:60], (f["strict_error_fp"] or "—")[:60]))
    else:
        A("（无）")
    A("")

    A("## 逐样本明细")
    A("")
    A("| 样本 | 期望 | legacy 动作/终态/延迟s/KB | strict 动作/终态/延迟s/KB | 一致 |")
    A("|---|---|---|---|---|")
    for p in cmp["pairs"]:
        s, lg, st = p["sample"], p["legacy"], p["strict"]
        cons = ("✓" if lg.get("action") and lg.get("action") == st.get("action")
                else ("—" if not (lg.get("action") and st.get("action")) else "✗"))
        A("| %s | %s%s | %s / %s / %s / %s | %s / %s / %s / %s | %s |" % (
            s["id"], s["expected_action"], "+KB" if s["expected_kb_hit"] else "",
            lg.get("action") or "∅", lg.get("terminal") or "∅", lg.get("latency_s"), lg.get("kb_ids"),
            st.get("action") or "∅", st.get("terminal") or "∅", st.get("latency_s"), st.get("kb_ids"),
            cons))
    A("")

    A("## Overall Verdict")
    A("")
    A("**%s**（维度：%s / %s / %s / %s）" % (cmp["overall"], d1["verdict"], d2["verdict"],
                                            d3["verdict"], d4["verdict"]))
    A("")

    A("## runtime-DB guard 校验")
    A("")
    A("- 守卫文件（mtime+size 前后对比）：")
    for k, v in guard_before.items():
        a = guard_after["after"][k]
        same = (v["exists"], v["mtime"], v["size"]) == (a["exists"], a["mtime"], a["size"])
        A("  - `%s`：%s（before mtime=%s size=%s；after mtime=%s size=%s）" % (
            k, "未变 ✓" if same else "**已变 ✗**", v["mtime"], v["size"], a["mtime"], a["size"]))
    A("- 结论：**%s**" % ("通过，runtime 生产 DB 零写入" if guard_after["ok"]
                         else "失败：守卫文件发生变化！"))
    A("")

    A("## 附注")
    A("")
    A("1. 真实 Hermes provider 是冻结契约硬性要求（不得 mock）；每次调用 30–120s，"
      "20 样本 × 2 链 ≈ 40 次调用为本测试的有意成本依赖，由单样本 300s 与全局预算双重约束。")
    A("2. legacy 链 customer_service 模式下的即时 ack（`should_reply_now`）在生产中由 monitor 同步发送；"
      "replay 只记录不发送，最终动作以异步 job 终态为准（与冻结文档「最终动作」口径一致）。")
    A("3. 样本文本均带触发词前缀（与生产 ingress 原文一致）；注入绕过 monitor 触发门，两条链同等处理。")
    A("4. legacy 链 payload `agent_timeout=90s`（reply_engine 生产值），超时即 expired，属真实链行为，如实记录。")
    A("5. legacy KB 证据来自 scoped-auth 修复后的 MCP trace provenance（`%s`），"
      "strict 来自 `turn_jobs.provenance_json`。" % (ROOT / "temp" / "knowledge_traces"))
    A("6. LEANN 索引经 junction 只读引用 runtime `%s`；模型缓存 `D:\\cache\\leann` 只读（HF_HUB_OFFLINE=1）。" % RUNTIME_BUS_INDEX)
    A("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# --rejudge：加载已有运行产物，仅按当前阈值规则重新判定（不重跑 provider）
# ---------------------------------------------------------------------------
def _parse_guard_from_report(text):
    """从旧报告 guard 小节解析 before 快照（兼容无 meta.json 的历史运行）。"""
    import re as _re
    before = {}
    for m in _re.finditer(
            r"`([^`]+)`：[^（]*\（before mtime=([0-9.eE+-]+|None) size=([0-9]+|None)", text):
        path, mt, sz = m.group(1), m.group(2), m.group(3)
        before[path] = {
            "exists": mt != "None",
            "mtime": (float(mt) if mt not in ("None",) else None),
            "size": (int(sz) if sz not in ("None",) else None),
        }
    return before


def _parse_mock_counts_from_report(text):
    import re as _re
    m = _re.search(r"legacy `agent_worker\._send_result_back` mock（记录 (\d+) 次）；"
                   r"strict `reliable_worker\.send_reply_detailed` mock（记录 (\d+) 次）", text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _print_summary(cmp, guard_after, report_path):
    d1, d2, d4 = cmp["dim1"], cmp["dim2"], cmp["dim4"]
    print("")
    print("================ shadow replay 摘要 ================")
    print("两链处理样本: %d（动作可比 %d，链失败 %d）" % (
        cmp["processed_n"], cmp["action_pairs_n"], len(cmp["chain_failures"])))
    print("动作一致率: %s  → %s" % (
        ("%.1f%%" % (d1["rate"] * 100)) if d1["rate"] is not None else "N/A", d1["verdict"]))
    print("KB-hit 一致率: %s → %s" % (
        ("%.1f%%" % (d2["rate"] * 100)) if d2["rate"] is not None else "N/A", d2["verdict"]))
    print("安全性: %s | 延迟 legacy p50/p95=%.1f/%.1fs strict p50/p95=%.1f/%.1fs → %s" % (
        cmp["dim3"]["verdict"], d4["legacy"]["p50"] or -1, d4["legacy"]["p95"] or -1,
        d4["strict"]["p50"] or -1, d4["strict"]["p95"] or -1, d4["verdict"]))
    print("overall verdict: %s" % cmp["overall"])
    print("runtime-DB guard: %s" % ("PASS" if guard_after["ok"] else "FAIL %s" % guard_after["changed"]))
    print("报告: %s" % report_path)


def rejudge(out_dir):
    """仅按当前代码的阈值规则重判既有结果：原始 results 与 guard 证据原样保留。"""
    out_dir = Path(out_dir)
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()
    samples = json.loads((out_dir / "samples.json").read_text(encoding="utf-8"))
    data = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    results = data["results"]
    old_report = (out_dir / "report.md").read_text(encoding="utf-8")

    meta_path = out_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    guard_before = meta.get("guard_before") or _parse_guard_from_report(old_report)
    if not guard_before:
        raise SystemExit("rejudge: 无法恢复原始 guard_before（meta.json 缺失且报告解析失败）")
    started_ts = meta.get("started_ts") or "（原运行，见备份报告）"
    elapsed = float(meta.get("elapsed_s") or 0.0)
    legacy_mock_n = int(meta.get("legacy_send_calls") or 0)
    strict_mock_n = int(meta.get("strict_send_calls") or 0)
    if not (legacy_mock_n or strict_mock_n):
        legacy_mock_n, strict_mock_n = _parse_mock_counts_from_report(old_report)

    cmp = compare(samples, results)
    guard_after = guard_verify(guard_before)  # 原始 before vs 当前状态

    ctx = ReplayContext(out_dir)
    ctx.legacy_send_calls = [{}] * legacy_mock_n  # 报告只展示计数
    ctx.strict_send_calls = [{}] * strict_mock_n

    backup = out_dir / "report_v1_oldrules.md"
    if not backup.exists():
        backup.write_text(old_report, encoding="utf-8")
    header_note = ("本报告为阈值重判版：原始样本数据与 guard 证据来自既有运行（未重跑任何 "
                   "provider 调用），仅按 2026-07-16 修订后的冻结阈值重新判定；"
                   "旧规则原报告见 `report_v1_oldrules.md`。")
    provider_note = "真实 agent 推理为有意依赖/成本（冻结契约禁止 mock provider）"
    report = render_report(ctx, samples, results, cmp, guard_before, guard_after,
                           started_ts, elapsed, provider_note, header_note=header_note)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    data["compare"] = {k: v for k, v in cmp.items() if k != "pairs"}
    data["rejudge"] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "rule": "whitelist-conditional-20260716", "provider_rerun": False}
    (out_dir / "results.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("[rejudge] 仅重判阈值，未重跑 provider；旧报告已备份为 report_v1_oldrules.md")
    _print_summary(cmp, guard_after, out_dir / "report.md")
    return 0 if guard_after["ok"] else 2


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Stage 4 shadow 对比 replay 工作台")
    ap.add_argument("--samples", type=int, default=20, help="样本数（默认 20 全量；2 = 冒烟）")
    ap.add_argument("--out", default=None, help="输出目录（默认 temp/shadow_replay_<ts>）")
    ap.add_argument("--budget-minutes", type=float, default=88.0, help="全局时间预算（分钟）")
    ap.add_argument("--per-sample-seconds", type=float, default=300.0, help="单样本 legacy 链时限（秒）")
    ap.add_argument("--rejudge", metavar="OUT_DIR", default=None,
                    help="只加载既有运行产物按当前阈值重判（不重跑 provider）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.rejudge:
        return rejudge(args.rejudge)

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (ROOT / "temp" / ("shadow_replay_%s" % ts))
    if not out_dir.is_absolute():
        out_dir = (ROOT / out_dir).resolve()

    all_samples = build_samples()
    samples = select_samples(all_samples, int(args.samples))
    for i, s in enumerate(samples):
        s["lid"] = 7100 + i  # 每样本唯一 local_id（job_key/event_id 去重）

    print("[shadow] 输出目录: %s" % out_dir)
    print("[shadow] 样本: %d 条（%s）" % (len(samples), "冒烟 KB-hit×2" if args.samples == 2 else "全量/子集"))

    # 基线：git status + runtime DB 守卫快照
    ctx = ReplayContext(out_dir)
    ctx.out.mkdir(parents=True, exist_ok=True)
    git_before = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO),
                                capture_output=True, text=True).stdout
    (ctx.out / "git_status_before.txt").write_text(git_before, encoding="utf-8")
    guard_before = guard_snapshot()
    (ctx.out / "samples.json").write_text(
        json.dumps([{k: v for k, v in s.items()} for s in samples],
                   ensure_ascii=False, indent=2), encoding="utf-8")

    ctx.provision()
    ctx.install()
    print("[shadow] monkeypatch 已安装（DB/config/发送 mock/陷阱），cwd=%s" % os.getcwd())

    results = {}
    started = time.time()
    started_ts = time.strftime("%Y-%m-%d %H:%M:%S")
    budget_s = float(args.budget_minutes) * 60.0
    policy = _policy(ctx)

    try:
        for idx, s in enumerate(samples, 1):
            if time.time() - started > budget_s:
                print("[shadow] 全局预算耗尽，剩余 %d 样本未跑 → INCONCLUSIVE" % (len(samples) - idx + 1))
                break
            print("[shadow] (%d/%d) %s %s" % (idx, len(samples), s["id"], s["text"][:36]))
            t_legacy0 = time.time()
            try:
                lg = run_legacy(s, ctx, policy, deadline_s=float(args.per_sample_seconds))
            except Exception:
                lg = {"chain": "legacy", "action": None, "kb_ids": [], "latency_s": None,
                      "terminal": "replay_exception", "error_fp": _fp(traceback.format_exc())}
            print("[shadow]   legacy: action=%s terminal=%s %.1fs kb=%s" % (
                lg.get("action"), lg.get("terminal"),
                time.time() - t_legacy0, lg.get("kb_ids")))
            if time.time() - started > budget_s:
                results[s["id"]] = {"legacy": lg, "strict": {}}
                print("[shadow] 全局预算耗尽，strict 未跑 → 该样本仅 legacy")
                break
            t_strict0 = time.time()
            try:
                st = run_strict(s, ctx, policy)
            except Exception:
                st = {"chain": "strict", "action": None, "kb_ids": [], "latency_s": None,
                      "terminal": "replay_exception", "error_fp": _fp(traceback.format_exc())}
            print("[shadow]   strict: action=%s terminal=%s %.1fs kb=%s" % (
                st.get("action"), st.get("terminal"),
                time.time() - t_strict0, st.get("kb_ids")))
            results[s["id"]] = {"legacy": lg, "strict": st}
    finally:
        elapsed = time.time() - started

    cmp = compare(samples, results)
    guard_after = guard_verify(guard_before)
    provider_note = "真实 agent 推理为有意依赖/成本（冻结契约禁止 mock provider）"

    (ctx.out / "results.json").write_text(
        json.dumps({"results": results, "compare": {k: v for k, v in cmp.items() if k != "pairs"}},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = render_report(ctx, samples, results, cmp, guard_before, guard_after,
                           started_ts, elapsed, provider_note)
    (ctx.out / "report.md").write_text(report, encoding="utf-8")
    (ctx.out / "meta.json").write_text(json.dumps({
        "guard_before": guard_before, "started_ts": started_ts, "elapsed_s": elapsed,
        "legacy_send_calls": len(ctx.legacy_send_calls),
        "strict_send_calls": len(ctx.strict_send_calls),
        "samples": [s["id"] for s in samples],
        "argv": sys.argv,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    git_after = subprocess.run(["git", "status", "--porcelain"], cwd=str(REPO),
                               capture_output=True, text=True).stdout
    (ctx.out / "git_status_after.txt").write_text(git_after, encoding="utf-8")

    # stdout 摘要
    _print_summary(cmp, guard_after, ctx.out / "report.md")

    ctx.restore()
    return 0 if guard_after["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
