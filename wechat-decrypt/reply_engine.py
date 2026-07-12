#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local wiki + boundary reply engine for WeChat bot.

V1 is dependency-free and safe-by-default:
- local markdown wiki retrieval
- pre/post boundary checks
- pluggable LLM provider placeholder
- deterministic fallback when no provider is configured
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import argparse
import sqlite3
import shlex
from typing import Any, Dict, Iterable, List, Tuple
import logging

logger = logging.getLogger(__name__)

import base64
import urllib.request
import urllib.error
import urllib.request

try:
    import task_router as _task_router
    import agent_jobs as _agent_jobs
    _HAS_TASK_ROUTER = True
except Exception:
    _task_router = None  # type: ignore
    _agent_jobs = None  # type: ignore
    _HAS_TASK_ROUTER = False

try:
    import target_registry as _target_registry
except Exception:
    _target_registry = None  # type: ignore

try:
    from message_aggregator import get_capabilities, CapabilityRegistry
except Exception:
    get_capabilities = None  # type: ignore
    CapabilityRegistry = None  # type: ignore


def _extract_image_md5_from_xml(content: str) -> str | None:
    """从微信图片消息XML中提取图片md5或aeskey用于解码。"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(content.strip())
        # 处理 <msg><img .../></msg> 格式
        if root.tag == 'msg':
            img = root.find('img')
            if img is not None:
                md5 = img.get('md5')
                if md5:
                    return md5
                aeskey = img.get('aeskey')
                if aeskey:
                    return aeskey
        # 处理 <img .../> 根格式
        if root.tag == 'img':
            md5 = root.get('md5')
            if md5:
                return md5
            aeskey = root.get('aeskey')
            if aeskey:
                return aeskey
    except Exception:
        pass
    # 兜底: 正则提取md5
    m = re.search(r'md5="([a-fA-F0-9]{32})"', content)
    if m:
        return m.group(1)
    return None


def _resolve_vision_sources(target: Dict[str, Any] | None, config: Dict[str, Any] | None):
    """Resolve the ordered vision source list and hook map from config/target.

    Returns:
        (sources, hooks, fallback_metadata)
    """
    cfg = config or {}
    t = target or {}
    vision_cfg: Dict[str, Any] = dict(t.get("vision") or cfg.get("vision") or {})

    explicit_sources = vision_cfg.get("sources")
    if isinstance(explicit_sources, list) and explicit_sources:
        return (
            [str(s) for s in explicit_sources if s],
            dict(vision_cfg.get("hooks") or {}),
            bool(vision_cfg.get("fallback_metadata", True)),
        )

    mode = str(vision_cfg.get("mode", "agent_llm")).lower()
    hooks: Dict[str, Any] = dict(vision_cfg.get("hooks") or {})
    fallback_metadata = bool(vision_cfg.get("fallback_metadata", True))

    if mode == "hook":
        hook_cmd = vision_cfg.get("hook_cmd")
        if hook_cmd:
            # Register the legacy hook_cmd under a default name and try it first.
            hooks.setdefault("default", hook_cmd)
            sources = ["hook:default", "mmx", "ocr"]
        else:
            sources = ["mmx", "ocr"]
    elif mode == "llm_vision":
        sources = ["mmx", "ocr"]
    else:
        # agent_llm (default): local vision is optional; still try mmx then ocr.
        sources = ["mmx", "ocr"]

    return sources, hooks, fallback_metadata


def _call_vision_recognizer(
    image_path: str,
    user_prompt: str = "简要的描述一下图片内容",
    target: Dict[str, Any] | None = None,
    config: Dict[str, Any] | None = None,
) -> str | None:
    """识别图片，支持多源 fallback（mmx → hook → ocr → metadata）。"""
    try:
        from image_handler import recognize_image_with_fallback, is_vision_error
        sources, hooks, fallback_metadata = _resolve_vision_sources(target, config)
        desc = recognize_image_with_fallback(
            image_path,
            prompt=user_prompt,
            sources=sources,
            hooks=hooks,
            fallback_metadata=fallback_metadata,
        )
        if desc and not is_vision_error(desc):
            logger.info("[VLM] success path=%s text=%s", image_path, desc[:120])
            return desc
        logger.warning("[VLM] failed path=%s result=%s", image_path, desc)
        return None
    except Exception as exc:
        logger.warning("[VLM] exception path=%s err=%r", image_path, exc)
        return None


def _prepend_vision_description(raw_text: str, desc: str) -> str:
    """把图片描述拼接到用户消息前。"""
    return f"{raw_text or ''}\n\n【系统识别：用户发送了图片，图片内容如下】{desc}".strip()

def _try_llm_vision(image_path: str, raw_text: str, target: Dict[str, Any] | None = None, config: Dict[str, Any] | None = None) -> str:
    """本地 VLM 识别图片并返回拼接后的文本（失败返回原文本）。"""
    desc = _call_vision_recognizer(image_path, target=target, config=config)
    if desc:
        return _prepend_vision_description(raw_text, desc)
    logger.warning("vision llm_vision failed: %s", image_path)
    return raw_text


def _try_vision_hook(image_path: str, hook_cmd: list[str] | str, raw_text: str, target: Dict[str, Any] | None = None, config: Dict[str, Any] | None = None) -> str:
    """调用用户自定义 hook 识别图片并返回拼接后的文本（失败返回原文本）。

    已接入统一 fallback 链：hook 失败后会继续尝试 mmx/ocr/metadata。
    """
    # Build a one-shot vision config that puts the requested hook first.
    vision_cfg: Dict[str, Any] = {"sources": ["hook:default", "mmx", "ocr"], "hooks": {"default": hook_cmd}}
    effective_target = dict(target or {})
    effective_target["vision"] = vision_cfg
    return _try_llm_vision(image_path, raw_text, target=effective_target, config=config)

def _resolve_image_for_message(message: Dict[str, Any], config: Dict[str, Any]) -> str | None:
    """对图片消息解码并调用VLM理解，返回图片描述文本。

    优先使用主循环已解码的 image_path（由 image_handler.process_image_message 设置），
    失败后再走备用路径: message local_id → message_resource.db packed_info → md5 →
          attach/{md5(username)}/*/Img/{md5}.dat → decrypt → VLM
    """
    # ---- 路径A: 优先使用主循环已解码图片 ----
    image_path = message.get("image_path")
    if image_path and os.path.isfile(str(image_path)):
        desc = _call_vision_recognizer(str(image_path), config=config)
        if desc:
            return f"[图片内容: {desc}]"

    # ---- 路径B: 备用复杂路径 ----
    import sqlite3
    import hashlib
    import glob as glob_mod

    local_id = message.get("local_id") or message.get("msg_local_id")
    if not local_id:
        return None

    decrypted_dir = config.get("decrypted_dir")
    db_dir = config.get("db_dir")
    if not decrypted_dir or not db_dir:
        return None

    wechat_base_dir = str(Path(db_dir).parent)
    resource_path = os.path.join(decrypted_dir, "message", "message_resource.db")

    if not os.path.isfile(resource_path):
        return None

    md5_hex = None
    try:
        conn = sqlite3.connect(resource_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT packed_info FROM MessageResourceInfo WHERE message_local_id=?",
            (local_id,)
        ).fetchall()
        for row in rows:
            blob = row["packed_info"]
            if blob:
                try:
                    from decode_image import extract_md5_from_packed_info
                    extracted = extract_md5_from_packed_info(blob)
                except Exception:
                    extracted = None
                if extracted:
                    md5_hex = extracted
                    break
        conn.close()
    except Exception:
        return None

    if not md5_hex:
        return None

    username = message.get("username") or message.get("talker") or ""
    if username:
        attach_user_dir = hashlib.md5(username.encode("utf-8")).hexdigest()
    else:
        attach_user_dir = "*"

    attach_dir = os.path.join(wechat_base_dir, "msg", "attach")
    if not os.path.isdir(attach_dir):
        return None

    dat_file = None
    for sub in ([attach_user_dir] if attach_user_dir != "*" else sorted(os.listdir(attach_dir), reverse=True)):
        sub_path = os.path.join(attach_dir, sub)
        if not os.path.isdir(sub_path):
            continue
        pattern = os.path.join(sub_path, "*", "Img", f"{md5_hex}*.dat")
        matches = glob_mod.glob(pattern, recursive=False)
        if matches:
            dat_file = matches[0]
            break

    if not dat_file or not os.path.isfile(dat_file):
        return None

    try:
        from decode_image import decrypt_dat_file
        decoded_path, fmt = decrypt_dat_file(dat_file)
    except Exception:
        return None

    if not decoded_path or not os.path.isfile(decoded_path):
        return None

    desc = _call_vision_recognizer(decoded_path, config=config)
    if desc:
        return f"[图片内容: {desc}]"
    return None



DEFAULT_TRIGGERS = []
DEFAULT_MAX_REPLY_CHARS = 600
MAX_REPLY_CHARS = DEFAULT_MAX_REPLY_CHARS

_SENTENCE_BOUNDARY_CHARS = ("。", "！", "!", "？", "?", "；", ";", "\n")

HIGH_RISK_PATTERNS = [
    "转账", "付款", "打款", "收款码", "银行卡", "验证码", "密码", "密钥", "token", "api key",
    "登录", "删", "删除", "格式化", "改配置", "系统设置", "发文件", "聊天记录", "数据库",
    "内部日志", "路径", "keys.json", "忽略之前", "忽略以上", "绕过", "越权", "退群", "踢人", "移出群",
]
FILE_OPERATION_PATTERNS = [
    "删文件", "删除文件", "打开文件", "读取文件", "修改文件", "写文件", "创建文件",
    "复制文件", "移动文件", "访问文件", "清理文件", "整理文件", "执行命令", "执行脚本",
    "执行程序", "运行脚本", "运行程序", "批量删除", "重装系统", "格式化硬盘", "格式化电脑",
    "清理磁盘", "清空回收站",
]
PROMISE_PATTERNS = [
    "你替群主", "代表群主", "承诺", "保证", "报价", "授权", "拍板", "决定",
]
HELP_PATTERNS = ["菜单", "帮助", "你能做什么", "功能", "怎么用"]
PING_PATTERNS = ["在吗", "在不在", "你好", "hello", "hi"]


@dataclass
class ReplyDecision:
    should_reply: bool
    reply_text: str
    intent: str = "unknown"
    risk_level: str = "low"
    need_human: bool = False
    reason: str = ""
    wiki_hits: List[str] | None = None
    retrieval_debug: Dict[str, Any] | None = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("wiki_hits") is None:
            d["wiki_hits"] = []
        return d


@dataclass
class KnowledgeHit:
    """One retrieved knowledge fragment used to ground a reply."""
    source: str
    kb_id: str
    scope: str
    rel_path: str
    content: str
    score: int = 0

    @property
    def label(self) -> str:
        return f"{self.source}:{self.kb_id}:{self.rel_path}"

def _knowledge_hits_to_payload(hits: List[KnowledgeHit]) -> List[Dict[str, Any]]:
    """Convert KnowledgeHit objects to the dicts expected by the agent payload."""
    out: List[Dict[str, Any]] = []
    for h in hits:
        if isinstance(h, KnowledgeHit):
            out.append({
                "label": h.label,
                "source": h.source,
                "kb_id": h.kb_id,
                "scope": h.scope,
                "rel_path": h.rel_path,
                "score": h.score,
                "content": h.content,
            })
        elif isinstance(h, (list, tuple)) and len(h) >= 2:
            out.append({"label": str(h[0]), "content": str(h[1])})
        else:
            out.append({"label": str(h), "content": str(h)})
    return out

def _json_safe(value: Any) -> Any:
    """Recursively make a value JSON-serializable (no bytes)."""
    if isinstance(value, (bytes, bytearray)):
        return "<bytes %d>" % len(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value

def strip_group_sender_prefix(text: str) -> str:
    """Remove the decrypted group sender prefix: "username:\ncontent"."""
    text = text or ""
    return re.sub(r"^[^:\n]{1,80}:\n", "", text, count=1)


def strip_triggers(text: str, triggers: Iterable[str] | None = None) -> str:
    text = strip_group_sender_prefix(text or "")
    triggers = list(triggers if triggers is not None else DEFAULT_TRIGGERS)
    for trig in sorted(triggers, key=len, reverse=True):
        text = text.replace(trig, " ")
    text = re.sub(r"@[^\s\u2005]+", " ", text)
    text = text.replace("@", " ")
    text = re.sub(r"\s+", " ", text).strip(" ，,。:：\n\t\u2005")
    return text


def _contains_any(text: str, words: Iterable[str]) -> bool:
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


def _looks_like_smalltalk(text: str) -> bool:
    """Cheap gate before online retrieval so casual chat never blocks monitor.

    Return True only for obvious conversational messages.  Product/work/wiki-style
    questions still go through scene retrieval.
    """
    t = (text or "").strip()
    if not t:
        return True
    if len(t) <= 18 and not _contains_any(t, [
        "资料", "知识库", "产品", "业务", "方案", "套餐", "认证", "实名", "真实号", "工作号", "权益", "重庆", "移动", "中移", "介绍", "是什么", "怎么", "如何", "多少", "价格", "报价", "合同", "授权", "审批", "记录", "笔记",
        "分析", "图片", "看图", "识别", "截图", "提取", "总结", "对比", "整理", "生成", "写",
    ]):
        return True
    return _contains_any(t, [
        "哈哈", "笑死", "讲个笑话", "开个玩笑", "在吗", "在不在", "早上好", "下午好", "晚上好", "无聊", "天气不错", "吃饭了吗", "你觉得呢", "咋样啊", "聊聊"
    ])


def _is_image_followup_request(text: str) -> bool:
    t = strip_triggers(text or "")
    if not t:
        return False
    return _contains_any(t, ["图", "图片", "照片", "截图", "看图", "识别", "分析", "看看", "描述", "提取", "总结"])


def _recent_image_prompt_from_context(context_messages: Any, current_local_id: int | None = None) -> str:
    if not isinstance(context_messages, list):
        return ""
    for msg in reversed(context_messages[-12:]):
        if not isinstance(msg, dict):
            continue
        try:
            local_id = int(msg.get("local_id") or 0)
        except Exception:
            local_id = 0
        if current_local_id and local_id >= current_local_id:
            continue
        text = str(msg.get("content") or msg.get("str_content") or msg.get("message") or msg.get("message_content") or "")
        if not _is_image_followup_request(text):
            continue
        clean = strip_triggers(text)
        if clean:
            return clean
    return ""


def _describe_images_for_agent(
    image_paths: List[str],
    prompt: str,
    target: Dict[str, Any] | None = None,
    config: Dict[str, Any] | None = None,
) -> List[Dict[str, str]]:
    """Return image descriptions enriched with vision source and failure reason.

    Each entry contains:
      - path: local image file path
      - description: recognized text or error marker
      - vision_source: source that produced the description (e.g. mmx, ocr,
        metadata) or None on total failure
      - vision_error: short failure reason when description is an error marker
    """
    descriptions: List[Dict[str, str]] = []
    if not image_paths:
        return descriptions
    user_prompt = prompt or "请简要描述图片内容，并提取对用户请求有用的信息"
    sources, hooks, fallback_metadata = _resolve_vision_sources(target, config)
    for p in image_paths[:3]:
        try:
            from image_handler import recognize_image_with_fallback, is_vision_error
            desc, source, error = recognize_image_with_fallback(
                p,
                prompt=user_prompt,
                sources=sources,
                hooks=hooks,
                fallback_metadata=fallback_metadata,
                return_source=True,
            )
            # Unwrap tuple type hints for runtime safety.
            desc = str(desc or "")
            source = str(source) if source else ""
            error = str(error) if error else ""
            if is_vision_error(desc):
                logger.warning("[VLM] failed path=%s source=%s error=%s", p, source or "None", error or desc)
                descriptions.append({
                    "path": p,
                    "description": desc,
                    "vision_source": source or "",
                    "vision_error": error or desc,
                })
            else:
                logger.info("[VLM] success path=%s source=%s text=%s", p, source, desc[:120])
                descriptions.append({
                    "path": p,
                    "description": desc,
                    "vision_source": source,
                    "vision_error": "",
                })
        except Exception as exc:
            logger.warning("[VLM] exception path=%s err=%r", p, exc)
            descriptions.append({
                "path": p,
                "description": "",
                "vision_source": "",
                "vision_error": f"exception: {exc}",
            })
    return descriptions


def _local_image_descriptions(
    image_paths: List[str],
    prompt: str,
    target: Dict[str, Any] | None = None,
    config: Dict[str, Any] | None = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """返回 (query_text, descriptions)。query_text 用于 FTS，descriptions 用于 agent payload。"""
    if not image_paths:
        return "", []
    descs = _describe_images_for_agent(image_paths, prompt, target=target, config=config)
    if not descs:
        logger.warning("[VLM] no descriptions returned for paths=%s", image_paths)
        return "", []
    lines = []
    for d in descs:
        text = d.get("description") or ""
        if not text:
            # Keep the failure marker for the agent payload, but don't pollute the FTS query.
            logger.warning("[VLM] empty description for path=%s", d.get("path"))
            d["description"] = "[图片识别失败]"
            continue
        lines.append("图片识别：%s" % text)
    logger.info("[VLM] built query text from %d/%d images", len(lines), len(descs))
    return "\n".join(lines), descs


def load_wiki(base_dir: str | Path | None = None) -> List[Tuple[str, str]]:
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent / "wiki"
    docs: List[Tuple[str, str]] = []
    if not base.exists():
        return docs
    for p in sorted(base.rglob("*.md")):
        try:
            rel = str(p.relative_to(base)).replace("\\", "/")
            docs.append((rel, p.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            continue
    return docs


def retrieve_wiki(query: str, limit: int = 3, base_dir: str | Path | None = None) -> List[Tuple[str, str]]:
    docs = load_wiki(base_dir)
    return _rank_wiki_docs(query, docs, limit=limit)


_FTS_FILLER_WORDS = [
    r"简单介绍一下",
    r"简单介绍",
    r"介绍一下",
    r"介绍下",
    r"简单说下",
    r"说一下",
    r"讲一下",
    r"简单",
    r"介绍",
    r"一下",
    r"说下",
    r"讲下",
]


_DEFAULT_HIT_MAX_CHARS = 4000



def _clean_query_for_fts(query: str) -> str:
    """Strip WeChat sender prefix, @mentions, filler words, and punctuation.

    The trigram FTS tokenizer needs tokens of 3+ characters to match CJK
    body content. Raw chat messages contain short prefixes/mentions that
    pollute the query, so we remove them before building the FTS expression.
    """
    q = query or ""
    # Remove common WeChat sender prefix and @mentions.
    q = re.sub(r"^[^:\n\uff1a]{1,40}[:\uff1a]\s*", "", q)
    q = re.sub(r"@[^\s\u2005]+[\s\u2005]*", "", q)
    # Remove filler words that would otherwise become short/noisy tokens.
    for pat in _FTS_FILLER_WORDS:
        q = re.sub(pat, " ", q)
    # Normalize punctuation to spaces.
    q = re.sub(r"[，,。！？!?:：；;、]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _query_tokens(query: str) -> List[str]:
    q = _clean_query_for_fts(query)
    tokens = [t for t in re.split(r"[\s,，。！？!?:：；;、/\\]+", q) if len(t) >= 2]
    # Add useful Chinese substrings for short group messages.  Chinese group
    # questions often look like "苹果是什么"; without a tokenizer the whole
    # phrase would miss a document that only contains "苹果".  Keep this
    # dependency-free by adding conservative 2/3-char ngrams for CJK spans.
    cjk_spans = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    for span in cjk_spans:
        for n in (2, 3):
            if len(span) >= n:
                tokens.extend(span[i:i + n] for i in range(0, len(span) - n + 1))
    extra = ["小助手", "小助理", "帮助", "功能", "边界", "能做", "确认", "转达", "牛马", "重庆移动"]
    tokens += [t for t in extra if t in q]
    # Remove overly generic particles that create noisy hits.
    stop = {"什么", "怎么", "可以", "一下", "这个", "那个", "我们", "你能", "我是"}
    return [t for t in dict.fromkeys(tokens) if t not in stop]


def _score_doc(query: str, rel: str, body: str) -> int:
    score = 0
    hay = rel + "\n" + body
    for tok in _query_tokens(query):
        score += body.count(tok)
        score += rel.count(tok) * 3
    return score


def _strong_scene_hits(query: str, hits: List[KnowledgeHit], min_score: int = 2) -> List[KnowledgeHit]:
    """Keep only scene hits that still overlap with the original query locally.

    Some providers return broad semantic/default results even for pure pings such
    as "哈哈" or "在吗".  Those are not reliable evidence that the user asked a
    knowledge-bound question.  Use our local token-overlap scorer as a provider
    agnostic quality gate; any surviving hit means the message should be treated
    as KB-grounded rather than casual chat.

    Local KBs already use the same token scorer during retrieval, so we trust
    their computed score with a relaxed threshold.  External providers are
    re-scored against the cleaned query to filter broad default results.
    """
    strong: List[KnowledgeHit] = []
    for h in hits or []:
        if h.source == "local":
            score = getattr(h, "score", 0) or 0
            effective_min = 1
        else:
            score = _score_doc(query, h.rel_path, h.content)
            effective_min = min_score
        if score >= effective_min:
            strong.append(h)
    return strong


def _rank_wiki_docs(query: str, docs: List[Tuple[str, str]], limit: int = 3) -> List[Tuple[str, str]]:
    if not docs:
        return []
    scored = []
    for rel, body in docs:
        score = _score_doc(query, rel, body)
        if score:
            scored.append((score, rel, body))
    if not scored:
        # Core rules are always useful as grounding.
        scored = [(1, rel, body) for rel, body in docs if rel.startswith("core/")]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(rel, body[:_DEFAULT_HIT_MAX_CHARS]) for _, rel, body in scored[:limit]]


def _resolve_wiki_path(path_value: str | Path | None, default: Path | None = None) -> Path | None:
    if not path_value:
        return default
    p = Path(path_value)
    if p.is_absolute():
        return p
    return Path(__file__).resolve().parent / p


def retrieve_scoped_wiki(query: str, global_dir: str | Path | None = None,
                         group_dir: str | Path | None = None,
                         limit: int = 5) -> List[Tuple[str, str]]:
    """Backward compatible wrapper: core plus one legacy group/local path."""
    hits = retrieve_knowledge(query, {"wiki_dir": str(global_dir) if global_dir else None},
                              {"wiki_dir": str(group_dir) if group_dir else None}, limit=limit)
    return [(h.label, h.content) for h in hits]


def _kb_root(config: Dict[str, Any]) -> Path:
    return _resolve_wiki_path(config.get("wiki_dir"), Path(__file__).resolve().parent / "wiki") or (Path(__file__).resolve().parent / "wiki")


def _knowledge_bases(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("knowledge_bases") or {}


def resolve_target_kb_ids(config: Dict[str, Any], target: Dict[str, Any]) -> List[str]:
    """Resolve user-selected 0/N knowledge bases for a WeChat target.

    New design: wiki/core is always first-principle grounding. A target can opt into
    zero or more scene/provider knowledge bases via target.knowledge_bases.
    Legacy target.wiki_dir/group_wiki_dirs are accepted only as a migration fallback.
    """
    raw = target.get("knowledge_bases")
    if raw is None:
        mapping = config.get("target_knowledge_bases") or {}
        for key in (target.get("name"), target.get("username"), target.get("table")):
            if key and key in mapping:
                raw = mapping[key]
                break
    if raw is None and target.get("wiki_dir"):
        raw = [{"id": f"legacy:{target.get('name') or 'target'}", "type": "local", "path": target.get("wiki_dir"), "scope": "legacy"}]
    if raw is None:
        raw = []
    if isinstance(raw, str):
        return [raw]
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
    return out


def _resolve_kb_spec(config: Dict[str, Any], target: Dict[str, Any], kb_id_or_spec: Any) -> Dict[str, Any] | None:
    if isinstance(kb_id_or_spec, dict):
        return dict(kb_id_or_spec)
    kb_id = str(kb_id_or_spec)
    if kb_id.startswith("legacy:") and target.get("wiki_dir"):
        return {"id": kb_id, "type": "local", "path": target.get("wiki_dir"), "scope": "legacy"}
    spec = _knowledge_bases(config).get(kb_id)
    if not spec:
        return None
    spec = dict(spec)
    spec.setdefault("id", kb_id)
    return spec


def _load_local_kb_docs(root: Path, spec: Dict[str, Any]) -> List[Tuple[str, str]]:
    path_value = spec.get("path") or spec.get("dir")
    if not path_value:
        return []
    p = Path(path_value)
    if not p.is_absolute():
        p = root / p
    return load_wiki(p)


def _retrieve_local_kb(query: str, root: Path, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    indexed = _retrieve_local_kb_fts(query, root, spec, limit)
    if indexed:
        return indexed
    docs = _load_local_kb_docs(root, spec)
    scored = []
    for rel, body in docs:
        score = _score_doc(query, rel, body)
        if score:
            scored.append((score, rel, body))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, rel, body in scored[:limit]:
        max_chars = int(spec.get("hit_max_chars") or _DEFAULT_HIT_MAX_CHARS)
        out.append(KnowledgeHit("local", str(spec.get("id") or spec.get("path")), str(spec.get("scope") or "scene"), rel, body[:max_chars], score))
    return out


def _local_kb_path(root: Path, spec: Dict[str, Any]) -> Path | None:
    path_value = spec.get("path") or spec.get("dir")
    if not path_value:
        return None
    p = Path(path_value)
    if not p.is_absolute():
        p = root / p
    return p


def _fts_query(query: str) -> str:
    toks = _query_tokens(query)
    if not toks:
        return ""
    return " OR ".join('"%s"' % t.replace('"', ' ') for t in toks[:12])


_KB_INDEX_SCHEMA_VERSION = 1


def _ensure_local_kb_fts(root: Path, spec: Dict[str, Any]) -> sqlite3.Connection | None:
    base = _local_kb_path(root, spec)
    if not base or not base.exists() or not base.is_dir():
        return None
    db_path = base / ".kb_index.sqlite"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS docs (rel_path TEXT PRIMARY KEY, mtime REAL NOT NULL, body TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(rel_path, body, content='docs', content_rowid='rowid', tokenize='trigram')")
    row = con.execute("SELECT value FROM index_meta WHERE key='schema_version'").fetchone()
    if row and int(row["value"]) != _KB_INDEX_SCHEMA_VERSION:
        con.execute("DROP TABLE docs_fts")
        con.execute("DELETE FROM docs")
        con.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(rel_path, body, content='docs', content_rowid='rowid', tokenize='trigram')")
        con.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES ('schema_version', ?)", (str(_KB_INDEX_SCHEMA_VERSION),))
        con.commit()
        row = None
    if row is None:
        con.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES ('schema_version', ?)", (str(_KB_INDEX_SCHEMA_VERSION),))
    known = {row["rel_path"]: float(row["mtime"] or 0) for row in con.execute("SELECT rel_path, mtime FROM docs")}
    seen = set()
    changed = False
    for p in sorted(base.rglob("*.md")):
        if p.name == ".kb_index.sqlite":
            continue
        try:
            rel = str(p.relative_to(base)).replace("\\", "/")
            mtime = p.stat().st_mtime
            seen.add(rel)
            if known.get(rel) == mtime:
                continue
            body = p.read_text(encoding="utf-8", errors="replace")
            db_row = con.execute("SELECT rowid FROM docs WHERE rel_path=?", (rel,)).fetchone()
            if db_row:
                con.execute("UPDATE docs SET mtime=?, body=? WHERE rel_path=?", (mtime, body, rel))
                con.execute("DELETE FROM docs_fts WHERE rowid=?", (db_row["rowid"],))
                con.execute("INSERT INTO docs_fts(rowid, rel_path, body) VALUES (?, ?, ?)", (db_row["rowid"], rel, body))
            else:
                cur = con.execute("INSERT INTO docs(rel_path, mtime, body) VALUES (?, ?, ?)", (rel, mtime, body))
                con.execute("INSERT INTO docs_fts(rowid, rel_path, body) VALUES (?, ?, ?)", (cur.lastrowid, rel, body))
            changed = True
        except Exception:
            continue
    for rel in set(known) - seen:
        db_row = con.execute("SELECT rowid FROM docs WHERE rel_path=?", (rel,)).fetchone()
        if db_row:
            con.execute("DELETE FROM docs_fts WHERE rowid=?", (db_row["rowid"],))
        con.execute("DELETE FROM docs WHERE rel_path=?", (rel,))
        changed = True
    if changed:
        con.commit()
    return con

def diagnose_local_kb(root: Path, spec: Dict[str, Any], query: str = "") -> Dict[str, Any]:
    """Return diagnostic info for a local KB index and a sample query."""
    base = _local_kb_path(root, spec)
    if not base or not base.exists() or not base.is_dir():
        return {"error": "path does not exist or is not a directory", "path": str(base)}
    db_path = base / ".kb_index.sqlite"
    result: Dict[str, Any] = {
        "kb_id": str(spec.get("id") or ""),
        "index_path": str(db_path),
        "index_exists": db_path.exists(),
        "schema_version": _KB_INDEX_SCHEMA_VERSION,
    }
    con = None
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        meta = con.execute("SELECT value FROM index_meta WHERE key='schema_version'").fetchone()
        result["stored_schema_version"] = int(meta["value"]) if meta else None
        row = con.execute("SELECT COUNT(*) AS cnt FROM docs").fetchone()
        result["doc_count"] = int(row["cnt"]) if row else 0
        if query:
            fts_query = _fts_query(query)
            result["sample_query"] = query
            result["sample_fts_query"] = fts_query
            if fts_query:
                try:
                    rows = con.execute(
                        "SELECT rel_path, body, bm25(docs_fts) AS rank FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT 5",
                        (fts_query,),
                    ).fetchall()
                    result["sample_hits"] = [
                        {"rel_path": str(r["rel_path"]), "rank": float(r["rank"]), "snippet": str(r["body"])[:200]}
                        for r in rows
                    ]
                except Exception as e:
                    result["sample_error"] = str(e)
    except Exception as e:
        result["error"] = str(e)
    finally:
        if con:
            try:
                con.close()
            except Exception:
                pass
    return result


def _retrieve_local_kb_fts(query: str, root: Path, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    q = _fts_query(query)
    if not q:
        return []
    con = None
    try:
        con = _ensure_local_kb_fts(root, spec)
        if not con:
            return []
        rows = con.execute(
            "SELECT rel_path, body, bm25(docs_fts) AS rank FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, max(1, int(limit or spec.get("limit") or 5))),
        ).fetchall()
        out: List[KnowledgeHit] = []
        for row in rows:
            rel = str(row["rel_path"])
            body = str(row["body"] or "")
            score = _score_doc(query, rel, body) or max(1, len(out) + 1)
            max_chars = int(spec.get("hit_max_chars") or _DEFAULT_HIT_MAX_CHARS)
            out.append(KnowledgeHit("local", str(spec.get("id") or spec.get("path")), str(spec.get("scope") or "scene"), rel, body[:max_chars], score))
        return out
    except Exception:
        return []
    finally:
        try:
            if con:
                con.close()
        except Exception:
            pass


def _ima_query_variants(query: str, limit: int = 8) -> List[str]:
    """Generate conservative fallback queries for IMA search.

    IMA's search endpoint can miss long chat messages that include sender names,
    mentions and filler words.  Keep the original query first, then try cleaned
    variants without changing API contract or local ranking behavior.
    """
    raw = query or ""
    variants: List[str] = []

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q or "").strip(" ，,。！？!?:：；;、\n\t")
        if q and q not in variants:
            variants.append(q)

    add(raw)
    # Remove common WeChat sender prefix and @mentions.
    clean = re.sub(r"^[^:\n：]{1,40}[:：]\s*", "", raw)
    clean = re.sub(r"@[^\s\u2005]+[\s\u2005]*", "", clean)
    clean = strip_triggers(clean)
    noise_patterns = [
        r"知识库里?找找?",
        r"帮我?找找?",
        r"你?找找?",
        r"我让你",
        r"请你?",
        r"麻烦你?",
        r"介绍的是",
        r"介绍一下",
        r"介绍下",
        r"简单介绍",
        r"说一下",
        r"讲一下",
    ]
    focused_clean = clean
    for pat in noise_patterns:
        focused_clean = re.sub(pat, " ", focused_clean)
    focused_clean = re.sub(r"[，,。！？!?:：；;、]+", " ", focused_clean)
    add(clean)
    add(focused_clean)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,12}(?:产品|业务|方案|材料|培训|办公|真实号|工作号)", focused_clean):
        phrase = m.group(0)
        phrase = re.sub(r"^(是|的|让|给|把|找|讲|说)+", "", phrase)
        phrase = re.sub(r"(资料|材料|介绍)+$", "", phrase)
        add(phrase)

    # Extract compact product phrases such as "工作号真实号" from chatty text.
    for m in re.finditer(r"[\u4e00-\u9fff]{2,12}(?:号|认证|办公|权益|套餐|实名|真实)[\u4e00-\u9fff]{0,8}(?:号|认证|办公|权益|套餐)?", clean):
        phrase = m.group(0)
        prev = None
        while phrase and phrase != prev:
            prev = phrase
            phrase = re.sub(r"^(简单介绍|简单|介绍|一下|说下|讲下)+", "", phrase)
            phrase = re.sub(r"(产品|业务|呢|吗|是什么|介绍|一下)+$", "", phrase)
        add(phrase)

    toks = [t for t in _query_tokens(clean) if len(t) >= 2]
    generic = {"简单", "介绍", "一下", "产品", "这个", "那个", "什么", "怎么", "一下工", "介绍一"}
    key_toks = [t for t in toks if t not in generic]
    # Keep tokens that contain product-like words or are longer domain phrases.
    focused = [t for t in key_toks if any(x in t for x in ("号", "认证", "办公", "权益", "套餐", "实名", "真实")) or len(t) >= 4]
    add(" ".join(focused[:8]))
    add(" ".join(key_toks[:8]))
    return variants[:limit]


def _get_secret_env(name: str) -> str | None:
    """Return secret env value, falling back to Windows user environment.

    This intentionally reads only named environment variables and never reads
    secret files, logs values, or persists them.
    """
    value = os.environ.get(name)
    if value:
        return value
    if os.name != "nt":
        return None
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            reg_value, _ = winreg.QueryValueEx(key, name)
        if reg_value:
            value = str(reg_value)
            os.environ[name] = value
            return value
    except Exception:
        return None
    return None


def _ima_auth(spec: Dict[str, Any]) -> Tuple[str | None, str | None]:
    client_env = spec.get("client_id_env") or "IMA_CLIENT_ID"
    api_env = spec.get("api_key_env") or "IMA_API_KEY"
    return _get_secret_env(client_env), _get_secret_env(api_env)


def _ima_post(spec: Dict[str, Any], api_path: str, payload: Dict[str, Any]) -> Dict[str, Any] | None:
    client_id, api_key = _ima_auth(spec)
    if not client_id or not api_key:
        return None
    import urllib.request
    base_url = str(spec.get("base_url") or "https://ima.qq.com").rstrip("/")
    skill_version = str(spec.get("skill_version") or os.environ.get("IMA_SKILL_VERSION") or "1.1.7")
    timeout = float(spec.get("timeout") or 8)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/{api_path.lstrip('/')}",
        data=body,
        method="POST",
        headers={
            "ima-openapi-clientid": client_id,
            "ima-openapi-apikey": api_key,
            "ima-openapi-ctx": f"skill_version={skill_version}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw or "{}")
    except Exception:
        return None


def _read_url_text(url: str, headers: Any, timeout: float, max_chars: int) -> str:
    if not url:
        return ""
    import urllib.request
    req_headers: Dict[str, str] = {}
    if isinstance(headers, dict):
        req_headers = {str(k): str(v) for k, v in headers.items()}
    elif isinstance(headers, list):
        for h in headers:
            if isinstance(h, dict) and h.get("key"):
                req_headers[str(h.get("key"))] = str(h.get("value") or "")
    try:
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(max_chars * 4)
        return raw.decode(ctype, errors="replace")[:max_chars].strip()
    except Exception:
        return ""


def _fetch_ima_media_content(media_id: str, spec: Dict[str, Any], max_chars: int = 6000) -> str:
    if not media_id:
        return ""
    payload = _ima_post(spec, "openapi/wiki/v1/get_media_info", {"media_id": media_id})
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return ""
    media_type = data.get("media_type")
    nb = data.get("notebook_ext_info") or {}
    note_id = nb.get("notebook_id") or nb.get("note_id") if isinstance(nb, dict) else ""
    if str(media_type) == "11" and note_id:
        note = _ima_post(spec, "openapi/note/v1/get_doc_content", {"note_id": str(note_id), "target_content_format": 0})
        nd = note.get("data") if isinstance(note, dict) and isinstance(note.get("data"), dict) else note
        content = nd.get("content") if isinstance(nd, dict) else ""
        return str(content or "")[:max_chars].strip()
    url_info = data.get("url_info") or {}
    if isinstance(url_info, dict):
        return _read_url_text(str(url_info.get("url") or ""), url_info.get("headers"), float(spec.get("timeout") or 8), max_chars)
    return ""


def _retrieve_ima_kb_once(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    client_id, api_key = _ima_auth(spec)
    kb_id = spec.get("knowledge_base_id") or spec.get("kb_id") or spec.get("id")
    if not client_id or not api_key or not kb_id:
        return []

    api_path = str(spec.get("api_path") or "openapi/wiki/v1/search_knowledge").lstrip("/")
    payload = _ima_post(spec, api_path, {"query": query or "", "cursor": "", "knowledge_base_id": str(kb_id)})
    if not isinstance(payload, dict):
        return []

    data = payload.get("data") if isinstance(payload, dict) else None
    if data is None and isinstance(payload, dict):
        data = payload
    items = (data or {}).get("info_list") or []
    folder_id = str(spec.get("folder_id") or "")
    kb_id_s = str(kb_id)
    out: List[KnowledgeHit] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        media_type = item.get("media_type")
        media_id = str(item.get("media_id") or "")
        parent = str(item.get("parent_folder_id") or "")
        # IMA returns folders (media_type=99, media_id starts with folder_) in search results.
        # For a target bound to one IMA folder, only use files directly under that folder.
        if media_type == 99 or media_id.startswith("folder_"):
            continue
        if folder_id and parent != folder_id:
            continue
        title = str(item.get("title") or "")
        highlight = str(item.get("highlight_content") or "")
        body = _fetch_ima_media_content(media_id, spec, int(spec.get("content_max_chars") or 6000))
        content = "\n".join(x for x in [title, highlight, body] if x).strip()
        rel = media_id or title or f"search_result_{idx + 1}"
        if parent:
            rel = f"{parent}/{rel}"
        if content:
            out.append(KnowledgeHit("ima", kb_id_s, str(spec.get("scope") or "online"), rel, content[:int(spec.get("hit_max_chars") or 6000)], max(1, limit - len(out))))
        if len(out) >= limit:
            break
    return out


def _retrieve_ima_kb(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    """Retrieve hits from Tencent IMA OpenAPI knowledge-base search.

    Official contract verified from ima-skills-1.1.7:
    - POST https://ima.qq.com/openapi/wiki/v1/search_knowledge
    - headers: ima-openapi-clientid, ima-openapi-apikey, ima-openapi-ctx, Content-Type
    - body: {query, cursor, knowledge_base_id}

    Security: credentials are never read from files here.  They must be supplied
    via environment variables named by config (client_id_env/api_key_env), so the
    repo never stores secrets.  Any missing config/network/API error degrades to
    no online hits; local core boundaries remain active.
    """
    out: List[KnowledgeHit] = []
    seen: set[tuple[str, str]] = set()
    for q in _ima_query_variants(query):
        batch = _retrieve_ima_kb_once(q, spec, max(1, limit - len(out)))
        if not batch and spec.get("folder_id"):
            # IMA occasionally omits/varies parent_folder_id filtering metadata even
            # when the returned rel_path clearly belongs to the configured folder.
            # Retry without server-side folder filtering, then keep only hits whose
            # normalized rel_path is under the target folder.
            loose_spec = dict(spec)
            folder_id = str(loose_spec.pop("folder_id") or "")
            loose_hits = _retrieve_ima_kb_once(q, loose_spec, max(1, limit - len(out)))
            prefix = folder_id.rstrip("/") + "/"
            batch = [h for h in loose_hits if str(h.rel_path).startswith(prefix)]
        for h in batch:
            key = (h.kb_id, h.rel_path)
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) >= limit:
                return out
    return out




import re as _re

def _sanitize_secrets(text: str) -> str:
    """Redact Feishu App IDs, secrets, and tokens from knowledge content."""
    # Feishu/Lark App ID: cli_ + 16-32 hex chars
    text = _re.sub(r'cli_[a-z0-9]{16,32}', 'cli_***', text)
    # Secrets following "Secret" label (Chinese colon or ASCII colon)
    text = _re.sub(r'(?i)secret[：:\s]*[A-Za-z0-9+/=_-]{24,64}', 'Secret：***', text)
    # Generic token-like strings (32+ alphanumeric without spaces)
    text = _re.sub(r'(?i)(api_key|app_secret|access_token|refresh_token|auth_key)[=：:\s]*[\'"]?[A-Za-z0-9+/=_-]{24,}', r'\1=***', text)
    return text


def _retrieve_getnote_kb(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    """Retrieve hits from Get笔记 CLI knowledge base.

    CLI contract verified with getnote.exe:
    - getnote search <query> --kb <knowledge_base_id> --limit N -o json
    - JSON: {success: true, data: {results: [{note_id,title,content,created_at,...}]}}

    This path intentionally degrades to [] on any error so local core boundaries
    remain active and the WeChat monitor is not blocked by online retrieval.
    """
    kb_id = str(spec.get("knowledge_base_id") or spec.get("kb_id") or spec.get("id") or "").strip()
    if not kb_id:
        return []
    exe = str(spec.get("executable") or spec.get("cli") or "getnote").strip() or "getnote"
    try:
        per_limit = max(1, min(int(limit or spec.get("limit") or 5), 10))
    except Exception:
        per_limit = 5
    cmd = [exe, "search", query or "", "--kb", kb_id, "--limit", str(per_limit), "-o", "json"]
    api_key_env = str(spec.get("api_key_env") or "").strip()
    env = os.environ.copy()
    if api_key_env and os.environ.get(api_key_env):
        env["GETNOTE_API_KEY"] = os.environ.get(api_key_env, "")
    timeout = float(spec.get("timeout") or 25)
    try:
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
            startupinfo=startupinfo, creationflags=creationflags,
        )
    except Exception as exc:
        logger.warning("getnote search failed for kb=%s query=%r: %s", kb_id, query, exc)
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        logger.warning("getnote search returned rc=%s stderr=%s kb=%s query=%r", proc.returncode, (proc.stderr or "")[:200], kb_id, query)
        return []
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    items = data.get("results") or data.get("notes") or []
    if not isinstance(items, list):
        return []
    out: List[KnowledgeHit] = []
    max_chars = int(spec.get("hit_max_chars") or 6000)
    scope = str(spec.get("scope") or "online")
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        note_id = str(item.get("note_id") or item.get("id") or f"result_{idx + 1}")
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or item.get("summary") or "").strip()
        created = str(item.get("created_at") or "").strip()
        body = _sanitize_secrets("\n".join(x for x in [title, created, content] if x).strip())
        if not body:
            continue
        rel = note_id if not title else f"{note_id}/{title[:80]}"
        out.append(KnowledgeHit("getnote", kb_id, scope, rel, body[:max_chars], max(1, per_limit - len(out))))
        if len(out) >= per_limit:
            break
    return out

def _retrieve_hook_kb(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    """
    Generic hook adapter for any external knowledge provider.

    Contract:
    - spec["executable"] or spec["cli"] points to the adapter script/binary.
    - The adapter receives query via KB_QUERY, kb_id via KB_ID, limit via KB_LIMIT env vars.
    - Adapter prints JSON to stdout: {"results": [{"title", "content", "source", "id"}, ...]}
    - Degrades to [] on any error so local core boundaries remain active.
    """
    kb_id = str(spec.get("knowledge_base_id") or spec.get("kb_id") or spec.get("id") or "").strip()
    exe = str(spec.get("executable") or spec.get("cli") or "").strip()
    if not exe:
        return []
    try:
        per_limit = max(1, min(int(limit or spec.get("limit") or 5), 10))
    except Exception:
        per_limit = 5
    env = os.environ.copy()
    env["KB_QUERY"] = query or ""
    if kb_id:
        env["KB_ID"] = kb_id
    env["KB_LIMIT"] = str(per_limit)
    timeout = float(spec.get("timeout") or 25)
    try:
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            [exe], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
            startupinfo=startupinfo, creationflags=creationflags,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    items = data.get("results") or data.get("notes") or []
    if not isinstance(items, list):
        return []
    out: List[KnowledgeHit] = []
    max_chars = int(spec.get("hit_max_chars") or 6000)
    scope = str(spec.get("scope") or "online")
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        note_id = str(item.get("id") or item.get("note_id") or f"result_{idx + 1}")
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or item.get("summary") or "").strip()
        source = str(item.get("source") or "").strip()
        body = "\n".join(x for x in [title, source, content] if x).strip()
        if not body:
            continue
        rel = note_id if not title else f"{note_id}/{title[:80]}"
        out.append(KnowledgeHit("hook", kb_id, scope, rel, body[:max_chars], max(1, per_limit - len(out))))
        if len(out) >= per_limit:
            break
    return out


def _retrieve_leann_kb(query: str, spec: Dict[str, Any], per_limit: int, config: Dict[str, Any]) -> List[KnowledgeHit]:
    """Retrieve hits from a LEANN semantic index via target_registry."""
    if not _target_registry:
        return []
    kb_id = str(spec.get("id") or spec.get("knowledge_base_id") or "leann")
    try:
        result = _target_registry.search_leann_kb(spec, query, limit=per_limit, cfg=config)
    except Exception:
        return []
    hits = result.get("hits") if isinstance(result, dict) else []
    if not hits:
        return []
    # Normalize faiss distances within this result set to a 1-10 integer scale
    # so semantic hits compete fairly with token-count scores from other KBs.
    # Lower distance = better, so the closest hit becomes 10 and the farthest 1.
    distances: List[float] = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        try:
            distances.append(float(item.get("score")))
        except Exception:
            pass
    if distances:
        dmin, dmax = min(distances), max(distances)
        span = (dmax - dmin) if dmax > dmin else 1.0
        dist_score = {d: max(1, int(((dmax - d) / span) * 9) + 1) for d in distances}
    else:
        dist_score = {}
    out: List[KnowledgeHit] = []
    max_chars = int(spec.get("hit_max_chars") or _DEFAULT_HIT_MAX_CHARS)
    for idx, item in enumerate(hits, start=1):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        rel = str(item.get("rel_path") or item.get("title") or "").strip() or f"hit_{idx}"
        # LEANN returns multiple passages from the same source file; include the
        # passage id in rel_path so cross-provider dedup keeps distinct chunks.
        if item_id:
            rel = "%s#%s" % (rel, item_id)
        content = str(item.get("snippet") or item.get("content") or item.get("text") or "").strip()
        if not content:
            continue
        try:
            dist = float(item.get("score")) if item.get("score") is not None else None
        except Exception:
            dist = None
        if dist is not None:
            score_int = dist_score.get(dist, 5)
        else:
            score_int = max(1, per_limit - len(out))
        out.append(KnowledgeHit("leann", kb_id, str(spec.get("scope") or "scene"), rel, content[:max_chars], score_int))
        if len(out) >= per_limit:
            break
    return out


def retrieve_knowledge_layers(query: str, config: Dict[str, Any], target: Dict[str, Any],
                               limit: int = 6, core_limit: int | None = None,
                               scene_limit: int | None = None, skip_core: bool = False) -> Dict[str, List[KnowledgeHit]]:
    root = _kb_root(config)
    core_hits: List[KnowledgeHit] = []
    scene_hits: List[KnowledgeHit] = []

    cfg_re = config.get("reply_engine") or {}
    core_limit = int(core_limit if core_limit is not None else cfg_re.get("core_limit", 3))
    scene_limit = int(scene_limit if scene_limit is not None else cfg_re.get("scene_limit", max(0, limit - core_limit)))

    # First principle: core rules/boundaries always apply for every target.
    if not skip_core:
        core_docs = load_wiki(root / "core")
        core_ranked = _rank_wiki_docs(query, [(f"core/{rel}", body) for rel, body in core_docs], limit=core_limit)
        for rel, body in core_ranked:
            core_hits.append(KnowledgeHit("local", "core", "first_principles", rel, body, _score_doc(query, rel, body) or 1))

    selected = target.get("knowledge_bases")
    if selected is None:
        selected = resolve_target_kb_ids(config, target)
    disable_local = bool(cfg_re.get("disable_local_kb"))
    for kb in selected or []:
        spec = _resolve_kb_spec(config, target, kb)
        if not spec or spec.get("enabled", True) is False:
            continue
        typ = str(spec.get("type") or "local").lower()
        if disable_local and typ == "local":
            continue
        per_limit = int(spec.get("limit") or limit)
        batch: List[KnowledgeHit] = []
        if typ == "local":
            batch = _retrieve_local_kb(query, root, spec, per_limit)
        elif typ == "ima":
            batch = _retrieve_ima_kb(query, spec, per_limit)
        elif typ == "getnote":
            batch = _retrieve_getnote_kb(query, spec, per_limit)
        elif typ == "hook":
            batch = _retrieve_hook_kb(query, spec, per_limit)
        elif typ == "leann":
            batch = _retrieve_leann_kb(query, spec, per_limit, config)
        for h in batch:
            if h.scope == "core" or str(h.rel_path).startswith("core/"):
                core_hits.append(h)
            else:
                scene_hits.append(h)

    # Rank scene hits across providers by score so a strong semantic match from
    # LEANN is not crowded out just because it was fetched after getnote/ima.
    scene_hits.sort(key=lambda h: h.score, reverse=True)

    # Cross-provider dedup: the same note/document may surface from getnote + ima.
    seen: set = set()
    deduped_scene: List[KnowledgeHit] = []
    for h in scene_hits:
        key = (h.source, h.kb_id, h.rel_path)
        if key in seen:
            continue
        seen.add(key)
        deduped_scene.append(h)

    return {"core": core_hits[:core_limit], "scene": deduped_scene[:scene_limit]}


def retrieve_knowledge(query: str, config: Dict[str, Any], target: Dict[str, Any], limit: int = 6) -> List[KnowledgeHit]:
    layers = retrieve_knowledge_layers(query, config, target, limit=limit)
    return (layers["core"] + layers["scene"])[:limit]


def precheck(user_text: str) -> ReplyDecision | None:
    if _contains_any(user_text, FILE_OPERATION_PATTERNS):
        return ReplyDecision(
            True,
            "这个涉及电脑文件或系统操作，我不能直接处理，需要飞扬确认。",
            intent="need_human",
            risk_level="high",
            need_human=True,
            reason="pre_boundary_file_operation",
        )
    if _contains_any(user_text, HIGH_RISK_PATTERNS):
        return ReplyDecision(
            True,
            "这个涉及敏感或高风险信息，我不能在群里直接处理，需要本人确认。",
            intent="need_human",
            risk_level="high",
            need_human=True,
            reason="pre_boundary_high_risk",
        )
    if _contains_any(user_text, PROMISE_PATTERNS):
        return ReplyDecision(
            True,
            "这个需要本人确认，我不能替群主/负责人承诺、授权或做决定。",
            intent="need_human",
            risk_level="medium",
            need_human=True,
            reason="pre_boundary_promise",
        )
    return None


def _max_reply_chars(config: Dict[str, Any] | None) -> int:
    """Resolve the configured cap for final reply length.

    Reads ``config.reply_engine.max_reply_chars`` and falls back to
    :data:`DEFAULT_MAX_REPLY_CHARS` (600).  Invalid/non-positive values are
    coerced to the default.  ``None`` and missing keys are also defaulted so
    legacy callers that pass nothing still get a sane cap.
    """
    raw = _reply_engine_config(config or {}).get("max_reply_chars") if config else None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_REPLY_CHARS
    if n <= 0:
        return DEFAULT_MAX_REPLY_CHARS
    return n


def _truncate_to_max(text: str, max_chars: int) -> str:
    """Trim ``text`` to at most ``max_chars`` chars using a soft cut.

    Strategy:
    1. If the text already fits, return it unchanged (after stripping).
    2. Otherwise, slice the candidate window to ``max_chars - 1`` and try to
       find the last sentence boundary (``。 ! ? ; ! ? ; \\n``); if found within
       the window, cut right after it.
    3. If no boundary is present, hard-cut and append ``…`` so the reader can
       tell the reply was trimmed.
    """
    text = (text or "").strip()
    if not text or max_chars <= 0 or len(text) <= max_chars:
        return text
    limit = max(1, max_chars - 1)
    candidate = text[:limit]
    last_boundary = -1
    for ch in _SENTENCE_BOUNDARY_CHARS:
        idx = candidate.rfind(ch)
        if idx > last_boundary:
            last_boundary = idx
    if last_boundary > 0:
        # Keep at least one full character before the cut, otherwise fall through
        # to a hard cut.  Skip leading whitespace so the result starts cleanly.
        cut = candidate[: last_boundary + 1].rstrip()
        if cut:
            return cut
    return candidate.rstrip() + "…"


def postcheck(text: str, config: Dict[str, Any] | None = None) -> str:
    text = text or ""
    blocked = ["keys.json", "MINIMAX_API_KEY", "数据库", "内部日志", "自动化实现", "系统路径"]
    if _contains_any(text, blocked):
        return "这个问题我先收到啦，涉及内部信息或需要确认的内容，需要本人确认后再处理。"
    text = re.sub(r"\s+", " ", text).strip()
    cap = _max_reply_chars(config) if config is not None else MAX_REPLY_CHARS
    if len(text) > cap:
        text = _truncate_to_max(text, cap)
    return text


def sanitize_reply_text(
    text: str,
    payload: Dict[str, Any] | None = None,
    config: Dict[str, Any] | None = None,
) -> str:
    """Sanitize an agent reply before it is stored or sent.

    Applies the same format cleanup used by the job queue layer (strip ANSI
    escapes, terminal box characters, resume noise) first, then runs the same
    postcheck used by the synchronous reply path. The length cap is resolved from
    the job payload first (``max_reply_chars``), then from
    ``config.reply_engine.max_reply_chars``, falling back to 600.

    This is the shared helper for async raw-agent/deep jobs: both the worker and
    the control API recover/poll/send paths should call it so that the persisted
    result and the sent message are identical.
    """
    if _agent_jobs is not None:
        text = _agent_jobs.sanitize_agent_result_text(text)
    payload = payload or {}
    max_chars = payload.get("max_reply_chars")
    if max_chars is None:
        max_chars = _max_reply_chars(config)
    else:
        try:
            max_chars = int(max_chars)
            if max_chars <= 0:
                max_chars = _max_reply_chars(config)
        except (TypeError, ValueError):
            max_chars = _max_reply_chars(config)
    return postcheck(text, {"reply_engine": {"max_reply_chars": max_chars}})


def _resolve_max_reply_chars(payload: Dict[str, Any], config: Dict[str, Any] | None) -> int:
    """Resolve the length cap from payload first, then config, then default."""
    max_chars = payload.get("max_reply_chars")
    if max_chars is not None:
        try:
            max_chars = int(max_chars)
            if max_chars > 0:
                return max_chars
        except (TypeError, ValueError):
            pass
    return _max_reply_chars(config)


def _clean_agent_output(text: str) -> str:
    text = text or ""
    text = text.replace("[ROUND END]", "")
    text = re.sub(r"<summary>[\s\S]*?</summary>", "", text).strip()
    # Agent output may include planning/prose before the final WeChat reply,
    # e.g. "Since ... I'll give ... @张三 正文". Prefer the last @-reply span.
    mention_spans = list(re.finditer(r"@[\w\-\u4e00-\u9fff]+\s+[^\n]+", text))
    from_mention = False
    if mention_spans:
        text = text[mention_spans[-1].start():].strip()
        from_mention = True
    # Prefer the final non-empty prose line; discard obvious tool/code noise.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bad_prefix = ("🛠️", "```", "<thinking>", "</thinking>", "<summary>")
    useful = [
        ln for ln in lines
        if "LLM Running" not in ln and not ln.startswith(bad_prefix) and not ln.startswith("{") and not ln.startswith("}")
    ]
    if not useful:
        return text.strip()
    return " ".join(useful).strip() if from_mention else " ".join(useful[-3:]).strip()





def _extract_command_reply(out: str) -> str | None:
    """Extract reply from command stdout.

    Supports:
    - JSON object with reply/text/content/output
    - Hermes quiet-mode output (filter metadata/session lines, keep final response)
    - Plain text: last non-empty line
    """
    out = (out or "").strip()
    if not out:
        return None

    lines = [line.strip() for line in out.splitlines() if line.strip()]

    # Metadata/session lines emitted by Hermes even in quiet mode.
    _META_RE = re.compile(
        r"^(Warning:|session_id:|Session:|Duration:|Messages?|Tokens?|Resume this|Model:|\x1b\[|\s*$)",
        re.IGNORECASE,
    )
    content_lines = [line for line in lines if not _META_RE.match(line)]
    if content_lines:
        # The actual response is usually the last non-metadata line.
        return content_lines[-1].strip()

    # JSON fallback: scan from the last non-empty line backwards.
    for cand in reversed(lines):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                for key in ("reply", "text", "content", "output"):
                    value = obj.get(key)
                    if value:
                        return str(value).strip()
            if isinstance(obj, str):
                return obj.strip()
        except Exception:
            continue
    return lines[-1] if lines else out


def _call_command_provider(prompt: str, config: Dict[str, Any], payload: Dict[str, Any] | None = None) -> str | None:
    """Call an external agent app through a tiny stdin/stdout protocol.

    This is the product-friendly path for users who already have
    Hermes or any other agent app configured with its own LLM.

    Supported config:
      provider: "command"
      cmd: ["agent", "--single-turn"] or "agent --single-turn"
      cmd may include the literal placeholder "{prompt}" which is replaced
      with the built prompt (wiki hits + user message) before execution.
      input_format: "plain" | "json"   (default: plain)
      timeout / llm_timeout: seconds

    stdin:
      plain -> prompt text
      json  -> payload JSON, always including payload["prompt"]

    stdout:
      plain text, or JSON object with one of: reply/text/content/output
    """
    cmd = config.get("cmd") or config.get("llm_provider_cmd") or os.environ.get("WECHAT_REPLY_LLM_CMD")
    if not cmd:
        return None
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    if not isinstance(cmd, list) or not cmd:
        return None

    # Substitute the literal {prompt} placeholder in the command argv.
    # The caller's prompt (built from wiki hits + user message) is passed here.
    cmd = [str(p).replace("{prompt}", prompt) if isinstance(p, str) else p for p in cmd]

    fmt = str(config.get("input_format") or "plain").lower()
    if fmt == "json":
        body = dict(payload or {})
        body.setdefault("prompt", prompt)
        stdin_data = json.dumps(body, ensure_ascii=False)
    else:
        stdin_data = prompt

    timeout = float(config.get("timeout", config.get("llm_timeout", 30)))
    env = os.environ.copy()
    # UTF-8 is part of the command-provider contract. Force Python-based
    # bridge apps on Windows away from the inherited ANSI code page (GBK/CP936).
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        startupinfo = None
        creationflags = 0
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    return _extract_command_reply(out)


def _post_http_agent_body(config: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any] | None:
    agent_url = config.get("agent_url")
    if not agent_url:
        return None

    timeout = float(config.get("agent_timeout", 30))
    api_key = config.get("agent_api_key", "")
    extra_headers = config.get("agent_headers") or {}

    req_data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **extra_headers,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        agent_url,
        data=req_data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            if not resp_body:
                return None
            try:
                data = json.loads(resp_body)
                return data if isinstance(data, dict) else {"reply_text": str(data)}
            except (json.JSONDecodeError, ValueError):
                return {"reply_text": resp_body.strip()} if resp_body.strip() else None
    except urllib.error.URLError as e:
        import logging
        logging.getLogger("reply_engine").warning("http_agent URL error: %r", e)
        return None
    except Exception as e:
        import logging
        logging.getLogger("reply_engine").warning("http_agent exception: %r", e)
        return None


def _call_http_agent(prompt: str, config: Dict[str, Any], payload: Dict[str, Any] | None = None) -> str | None:
    """Call an external agent via HTTP POST.

    This is the product-friendly path for users who already have
    Hermes or any other agent app running with an HTTP endpoint.
    Supported config:
      provider: "http_agent"
      agent_url: "http://localhost:8000/chat"
      agent_api_key: "sk-xxx" (optional)
      agent_timeout: 30 (seconds, default 30)
      agent_headers: {"x-custom": "value"} (optional extra headers)

    Request body (JSON):
      {
        "task": "wechat_reply",
        "mode": "standard" | "raw_agent",
        "messages": [
          {"role": "system", "content": "..."},
          {"role": "user", "content": "..."}
        ],
        "payload": {...},
        "context": {
          "target_name": "...",
          "sender": "...",
          "wiki_hits": [...],
          "image_paths": [...]
        }
      }

    Expected response (JSON):
      {"reply_text": "..."}
    """
    # Build request body
    body = {
        "task": "wechat_reply",
        "mode": (payload or {}).get("mode") or "standard",
        "messages": [
            {"role": "system", "content": "你是群聊小助手。请根据用户问题和提供的知识库内容生成简洁、自然的回复。"},
            {"role": "user", "content": prompt},
        ],
        "payload": payload or {},
        "context": {},
    }
    if payload:
        body["context"] = {
            "target_name": payload.get("target", {}).get("name"),
            "sender": payload.get("mention_name"),
            "wiki_hits": payload.get("wiki_hits", []),
            "image_paths": payload.get("image_paths", []),
            "mode": payload.get("mode"),
        }

    data = _post_http_agent_body(config, body)
    if not data or data.get("should_reply") is False:
        return None
    reply = data.get("reply_text") or data.get("text") or data.get("content") or data.get("output") or data.get("response") or data.get("reply")
    return str(reply).strip() if reply else None


def call_llm_provider(prompt: str, config: Dict[str, Any] | None = None, payload: Dict[str, Any] | None = None) -> str | None:
    """Optional provider hook. Supports lightweight external agent command first-class."""
    config = config or {}
    if config.get("enabled") is False:
        return None
    provider = str(config.get("provider") or "").lower()

    # Product-friendly path: users keep their own agent app and LLM config;
    # we only pass one task over stdin and read one reply from stdout.
    if provider == "command":
        cmd_reply = _call_command_provider(prompt, config, payload)
        if cmd_reply:
            return postcheck(cmd_reply)
        return None

    # HTTP agent path: connect to any local or remote agent via HTTP POST.
    if provider == "http_agent":
        http_reply = _call_http_agent(prompt, config, payload)
        if http_reply:
            return postcheck(http_reply)
        return None

    # Legacy command fallback for older configs without provider="command".
    cmd_reply = _call_command_provider(prompt, config, payload)
    if cmd_reply:
        return postcheck(cmd_reply)
    return None


def fallback_reply(clean_text: str, wiki_hits: List[Any], mode: str = "scene") -> Tuple[str, str, bool]:
    text = clean_text or ""
    if not text:
        return "我在，有事可以直接说，我会尽量基于已有信息帮你整理或转达。", "smalltalk", False
    if _contains_any(text, HELP_PATTERNS):
        return "我可以在被叫到时，基于已有资料做简短说明、整理问题、回答常见问题；需要本人判断的事，我会提示需要他确认。", "assistant_help", False
    if _contains_any(text, PING_PATTERNS):
        return "我在，有事可以直接说。", "smalltalk", False
    if mode == "chat":
        if _contains_any(text, ["哈哈", "笑死", "好玩", "有意思"]):
            return "哈哈，我也觉得挺有意思的。", "smalltalk", False
        if _contains_any(text, ["你是谁", "你是干嘛", "你能干嘛"]):
            return "我是群里的小助手呀，被@到的时候可以陪聊、整理信息，也能按已有资料回答问题。", "smalltalk", False
        if len(text) <= 20:
            return "哈哈收到，我在呢。", "smalltalk", False
        return "我懂你意思了，咱们可以继续聊；如果要问具体资料，我再帮你按已有信息整理。", "smalltalk", False
    if wiki_hits:
        names = "、".join((h.label if isinstance(h, KnowledgeHit) else h[0]) for h in wiki_hits[:2])
        return f"我先按已有资料理解：这件事我可以帮你整理或说明；如果涉及决定、承诺或执行，还需要本人确认。", "wiki_qa", False
    return "我先收到啦，这个问题需要结合更多背景，建议等本人确认后再处理。", "need_human", True


def resolve_wiki_dir(config: Dict[str, Any], target: Dict[str, Any]) -> str:
    """Legacy helper kept for older callers; new callers use knowledge_bases."""
    root = Path(__file__).resolve().parent
    value = target.get("wiki_dir")
    if not value:
        mapping = config.get("group_wiki_dirs") or {}
        for key in (target.get("name"), target.get("username"), target.get("table")):
            if key and key in mapping:
                value = mapping[key]
                break
    value = value or config.get("wiki_dir") or str(root / "wiki")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def _extract_mention_name(message: Dict[str, Any] | str) -> str:
    if isinstance(message, dict):
        for key in ("mention_name", "sender_display_name", "sender_name", "from_display_name"):
            value = str(message.get(key) or "").strip()
            if value:
                return value.lstrip("@").strip()
    return ""



def _reply_engine_config(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("reply_engine")
    return raw if isinstance(raw, dict) else {}


def _agent_mode(config: Dict[str, Any], target: Dict[str, Any] | None = None) -> str:
    """Return reply_engine.agent_mode, with optional target-level override."""
    target_mode = str((target or {}).get("agent_mode") or "").strip().lower()
    if target_mode:
        return target_mode
    return str(_reply_engine_config(config).get("agent_mode") or "standard").lower()


def _leann_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return reply_engine.leann block with safe defaults."""
    raw = _reply_engine_config(config).get("leann") or {}
    return {
        "cli_path": str(raw.get("cli_path") or "leann"),
        "embedding_model": str(raw.get("embedding_model") or "sentence-transformers/all-MiniLM-L6-v2"),
        "timeout": float(raw.get("timeout") or 120),
    }


def _enable_wechat_mcp_tool(config: Dict[str, Any]) -> bool:
    """Return reply_engine.enable_wechat_mcp_tool, defaulting to False."""
    return bool(_reply_engine_config(config).get("enable_wechat_mcp_tool", False))


def _thin_monitor_enabled(config: Dict[str, Any], target: Dict[str, Any]) -> bool:
    """Return whether this target should run in thin-monitor mode.

    In thin mode the monitor delegates knowledge retrieval, vision, session
    management, and reply generation to the agent side.  The monitor only keeps
    listen/send/ack responsibilities.
    """
    if target.get("thin_monitor") is not None:
        return bool(target.get("thin_monitor"))
    return bool(_reply_engine_config(config).get("thin_monitor", False))


def _resolve_skill_name(
    config: Dict[str, Any],
    target: Dict[str, Any],
    *,
    is_tool_agent: bool = False,
    thin_monitor: bool = False,
) -> str:
    """Return the skill name to use for this reply.

    Priority: target.skill_name > global thin skill (when thin/tool_agent) >
    global default skill.
    """
    target_skill = str(target.get("skill_name") or "").strip()
    if target_skill:
        return target_skill
    re_cfg = _reply_engine_config(config)
    if thin_monitor or is_tool_agent:
        return str(re_cfg.get("wechat_auto_skill") or "wechat_auto").strip() or "wechat_auto"
    return str(re_cfg.get("skill_name") or "wechat_task").strip() or "wechat_task"


def _wechat_side_payload(thin_monitor: bool) -> Dict[str, Any]:
    """Return the wechat_side responsibility split for the agent payload.

    In thin-monitor mode the project side only listens and sends; everything
    else is delegated to the agent.  In the legacy path the monitor still owns
    trigger/session/image_extract before handing content generation to the agent.
    """
    if thin_monitor:
        return {
            "responsibilities": ["listen", "send"],
            "delegated_to_agent": [
                "content_understanding",
                "vision",
                "wiki_match",
                "rag",
                "session_management",
                "reply_generation",
            ],
        }
    return {
        "responsibilities": ["listen", "trigger", "session", "image_extract", "send"],
        "delegated_to_agent": ["content_understanding", "vision", "wiki_match", "rag", "reply_generation"],
    }


def _tool_agent_allowed_leann_indexes(config: Dict[str, Any], target: Dict[str, Any] | None = None) -> List[str]:
    """Return the LEANN index names this target is allowed to search.

    Only ``type: leann`` knowledge bases contribute. An empty list means no
    target-specific restriction (backward compatible).
    """
    indexes: List[str] = []
    if not target:
        return indexes
    for kb_id in resolve_target_kb_ids(config, target) or []:
        spec = _resolve_kb_spec(config, target, kb_id)
        if not spec:
            continue
        if str(spec.get("type") or "").lower() != "leann":
            continue
        idx = str(spec.get("index_name") or spec.get("knowledge_base_id") or "").strip()
        if idx and idx not in indexes:
            indexes.append(idx)
    return indexes


def _tool_agent_available_tools(config: Dict[str, Any], target: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """Build the available_tools list for tool_agent mode.

    leann_search is handled by the Python tool loop fallback.  WeChat query
    and vision tools are normally executed by Hermes's native MCP layer; when
    enable_wechat_mcp_tool is true we also expose their schema in the prompt so
    the agent knows how to call them.
    """
    allowed = _tool_agent_allowed_leann_indexes(config, target)
    description = "本地 LEANN 语义搜索工具，按需求检索知识库索引。"
    tool: Dict[str, Any] = {
        "name": "leann_search",
        "description": description,
    }
    if allowed:
        tool["allowed_index_names"] = allowed
        tool["default_index_name"] = allowed[0]
        tool["description"] = description + " 当前目标只允许使用以下索引：%s。" % ", ".join(allowed)
    # NOTE: describe_image MCP tool exists in mcp_server.py, but the async
    # Hermes job path does not execute the Python tool loop and native MCP tool
    # execution depends on the user's Hermes config.  Until that path is verified,
    # we do not advertise the tool in the prompt to avoid the model emitting raw
    # JSON tool calls that would leak into the WeChat reply.
    return [tool]


def _is_raw_agent_mode(config: Dict[str, Any], target: Dict[str, Any]) -> bool:
    """Whether reply work is delegated fully to the external agent.

    In this mode the Python process only keeps the WeChat-side responsibilities:
    listen, trigger/session decision, image file extraction, and sending.  It does
    not retrieve wiki, run local vision, build grounded prompts, or fallback with
    canned replies.
    """
    raw_engine_cfg = config.get("reply_engine")
    engine_cfg: Dict[str, Any] = raw_engine_cfg if isinstance(raw_engine_cfg, dict) else {}
    values = [
        engine_cfg.get("raw_mode"),
        engine_cfg.get("agent_raw_mode"),
        target.get("raw_mode"),
        target.get("agent_raw_mode"),
    ]
    if any(v is True for v in values):
        return True
    mode = str(engine_cfg.get("mode") or target.get("reply_engine_mode") or "").lower()
    return mode in {"raw", "raw_agent", "agent_raw"}


def _build_raw_agent_prompt(
    clean_text: str,
    mention_name: str,
    response_mode: str = "group_assistant",
    knowledge_hits: List[Dict[str, Any]] | None = None,
) -> str:
    mode_instruction = _mode_instruction(response_mode)
    mention_rule = f"如需回复，建议以 @{mention_name} + 空格 开头。" if mention_name else "如需回复，建议直接输出适合微信群发送的一段话。"
    knowledge_block = ""
    if knowledge_hits:
        lines = []
        for i, h in enumerate(knowledge_hits, start=1):
            if isinstance(h, dict):
                label = str(h.get("label") or "")
                content = str(h.get("content") or "")
            elif isinstance(h, (list, tuple)) and len(h) >= 2:
                label, content = str(h[0]), str(h[1])
            else:
                label, content = "", str(h)
            lines.append(f"[{i}] {label}\n{content}")
        knowledge_block = "\n[知识库资料]\n" + "\n\n".join(lines) + "\n\n"
    return (
        "你正在执行 wechat-raw-agent 任务。请根据用户消息和群聊上下文，只输出最终要发送到微信群的一段中文回复。\n"
        f"{mode_instruction}\n"
        f"{mention_rule}\n"
        "硬规则：\n"
        "1. 不能冒充本人；不能替群主/负责人承诺、授权、报价、决策或执行高风险操作。\n"
        "2. 不能泄露密钥、系统路径、数据库、内部日志、自动化实现细节。\n"
        "3. 不能读取、修改、删除、执行或以其他方式操作电脑本地文件、文件夹、系统命令、脚本、程序。\n"
        "4. 不确定就说不确定，不要编造事实。\n"
        "5. 最多300字。\n\n"
        f"{knowledge_block}"
        f"[用户消息]\n{clean_text}\n\n"
        "请只输出要发送到微信群的一段中文回复。"
    )



def _normalize_response_mode(mode: str | None) -> str:
    """Normalize product response mode to one of the two supported values."""
    return "customer_service" if str(mode or "").lower() == "customer_service" else "group_assistant"


def _mode_instruction(mode: str) -> str:
    normalized = _normalize_response_mode(mode)
    if normalized == "customer_service":
        return "当前响应模式：客服。用客服口吻，先确认诉求；如果知识库里有相关资料，请基于资料给出处理建议或排查步骤；只有资料无法覆盖时才追问一个必要澄清问题。不要替负责人承诺、报价、授权或做高风险决定。"
    return "当前响应模式：平衡。简短克制，只回答用户明确表达的问题；不主动扩展，不替负责人承诺。"


def _agent_ack(response_mode: str) -> Tuple[str, bool]:
    """Return (ack_text, should_reply_now) for an agent-queued decision.

    Only customer_service mode gets an immediate acknowledgement; free and
    balanced modes should feel natural and wait for the final async reply.
    """
    if str(response_mode or "").lower() == "customer_service":
        return "正在处理中，请稍等。", True
    return "", False


def _run_tool_agent_sync(
    raw_text: str,
    clean: str,
    mention_name: str,
    config: Dict[str, Any],
    target: Dict[str, Any],
    image_paths: List[str],
    context_messages: List[Dict[str, Any]] | None,
) -> ReplyDecision | None:
    """Route a tool_agent request through the deep-agent provider synchronously.

    The synchronous build_prompt()/call_llm_provider() path does not inject tool
    instructions or run the tool loop, so agent_mode='tool_agent' must be handled
    by an AgentProvider (HermesProvider) that does both. If the provider is
    unavailable or returns no reply, return None so the caller can fall back to
    standard retrieval.
    """
    from agent_provider import provider_from_config

    leann_cfg = _leann_config(config)
    selected_kbs = resolve_target_kb_ids(config, target) or []
    response_mode = _normalize_response_mode(target.get("mode") or "group_assistant")
    thin_monitor = _thin_monitor_enabled(config, target)
    skill_name = _resolve_skill_name(config, target, is_tool_agent=True, thin_monitor=thin_monitor)
    payload: Dict[str, Any] = {
        "clean_text": clean or raw_text,
        "raw_text": raw_text,
        "agent_mode": "tool_agent",
        "skill_name": skill_name,
        "available_tools": _tool_agent_available_tools(config, target),
        "enable_wechat_mcp_tool": _enable_wechat_mcp_tool(config),
        "leann": leann_cfg,
        "knowledge_hits": [],
        "knowledge_bases": selected_kbs,
        "image_paths": image_paths,
        "mention_name": mention_name,
        "mode_instruction": _mode_instruction(response_mode),
        "max_reply_chars": _max_reply_chars(config),
        "target": {
            "id": target.get("id"),
            "name": target.get("name"),
            "username": target.get("username"),
            "table": target.get("table"),
        },
    }
    job: Dict[str, Any] = {
        "sender": mention_name,
        "payload": payload,
        "timeout": 240.0 if image_paths else 90.0,
    }
    try:
        instance_id = str(target.get("dedicated_agent_instance_id") or "").strip() or None
        provider = provider_from_config(config, instance_id=instance_id)
        result = provider.run(job, timeout=job["timeout"])
    except Exception as exc:
        logger.warning("tool_agent sync provider failed: %r; falling back to standard retrieval", exc)
        return None

    if not result.ok or not result.reply_text:
        logger.warning("tool_agent sync provider returned no reply: %s", getattr(result, "error", "") or "")
        return None

    reply = postcheck(_clean_agent_output(result.reply_text), config)
    if reply == "[SILENT]":
        return ReplyDecision(
            should_reply=False,
            reply_text="",
            intent="tool_agent_silent",
            risk_level="low",
            need_human=False,
            reason="tool_agent_provider_silent",
            wiki_hits=[],
            retrieval_debug={"agent_mode": "tool_agent", "route": "tool_agent_sync"},
        )
    return ReplyDecision(
        True,
        reply,
        intent="tool_agent_sync_reply",
        risk_level="low",
        need_human=False,
        reason="tool_agent_provider_sync",
        wiki_hits=[],
        retrieval_debug={"agent_mode": "tool_agent", "route": "tool_agent_sync"},
    )



def _agent_reply_from_response(data: Dict[str, Any] | None) -> str | None:
    if not data or data.get("should_reply") is False:
        return None
    reply = (
        data.get("reply_text")
        or data.get("text")
        or data.get("content")
        or data.get("output")
        or data.get("response")
        or data.get("reply")
    )
    return str(reply).strip() if reply else None


def _selected_kb_specs(config: Dict[str, Any], target: Dict[str, Any]) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    for kb_id in resolve_target_kb_ids(config, target):
        spec = _resolve_kb_spec(config, target, kb_id)
        if not spec:
            specs.append({"id": kb_id, "missing": True})
            continue
        specs.append(spec)
    return specs


def _call_raw_http_agent(llm_config: Dict[str, Any], payload: Dict[str, Any]) -> str | None:
    prompt = str(payload.get("prompt") or "")
    body = {
        "task": "wechat_reply",
        "mode": "raw_agent",
        "messages": [
            {"role": "system", "content": "你是专用微信群回复 agent。请自行完成内容理解、图片识别、wiki匹配和回复生成。"},
            {"role": "user", "content": prompt},
        ],
        "payload": payload,
        "context": {
            "target_name": payload.get("target", {}).get("name"),
            "sender": payload.get("mention_name"),
            "image_paths": payload.get("image_paths", []),
            "knowledge_bases": payload.get("knowledge_bases", []),
        },
    }
    return _agent_reply_from_response(_post_http_agent_body(llm_config, body))


def _ensure_mention_prefix(reply: str, mention_name: str) -> str:
    """prefix responsibility moved to wechat_sender.send_reply_detailed(mention_name=...)"""
    return reply



def build_prompt(raw_text: str, clean_text: str, wiki_hits: List[Any], context_messages: list | None = None, mention_name: str = "", mode: str = "scene", max_chars: int | None = None) -> str:
    wiki_parts = []
    for h in wiki_hits:
        if isinstance(h, KnowledgeHit):
            wiki_parts.append(f"## {h.label} score={h.score} scope={h.scope}\n{h.content}")
        else:
            rel, body = h
            wiki_parts.append(f"## {rel}\n{body}")
    wiki = "\n\n".join(wiki_parts)
    ctx_lines = []
    if context_messages:
        for cm in context_messages:
            role = "用户" if cm.get('real_sender_id') and cm['real_sender_id'] != 2 else "我"
            sender = cm.get('real_sender_id') or ''
            ts = cm.get('create_time_local') or cm.get('create_time') or ''
            lid = cm.get('local_id') or ''
            content = cm.get('message_content', '')
            if content and isinstance(content, str):
                prefix = f"[{ts} #{lid} sender={sender}] " if (ts or lid or sender) else ""
                ctx_lines.append(f"{prefix}{role}: {content}")
    ctx_block = "\n".join(ctx_lines[-20:])
    mention_rule = f"必须以 @{mention_name} + 空格 开头。" if mention_name else "必须以 @提问人昵称 + 空格 开头。"
    if mode == "chat":
        task_rule = "当前没有命中场景知识库。请只依据core边界约束和群聊上下文自然闲聊；不要编造产品/业务事实；不确定就轻松说明可以继续问具体问题。"
        wiki_label = "[core约束]"
    else:
        task_rule = "当前已命中场景知识库。请优先依据场景wiki回答，同时遵守core边界约束；知识库无依据时说明不确定。"
        wiki_label = "[core约束与场景wiki]"
    ctx_header = ('[群聊上下文]\n' + ctx_block + '\n\n') if ctx_lines else ''
    cap = max_chars if isinstance(max_chars, int) and max_chars > 0 else MAX_REPLY_CHARS
    return f"""你是群聊小助手，只在微信群中被明确叫到时回复。\n强边界：不能冒充本人；不能替群主/负责人承诺、授权、报价、决策或执行高风险操作；不能读取、修改、删除、执行或以其他方式操作电脑本地文件、文件夹、系统命令、脚本、程序；不能泄露密钥、系统路径、数据库、内部日志、自动化实现细节；知识库无依据时说明不确定。\n回复要求：简短、自然、适合微信群，最多{cap}字（超出部分会被系统强制截断）；{mention_rule}\n任务策略：{task_rule}\n\n{ctx_header}[群消息]\n{raw_text}\n\n[清洗后问题]\n{clean_text}\n\n{wiki_label}\n{wiki}\n\n请只输出要发送到微信群的一段中文回复。"""


def generate_reply(message: Dict[str, Any] | str,
                   target: Dict[str, Any] | None = None,
                   config: Dict[str, Any] | None = None,
                   config_path: str | None = None) -> ReplyDecision:
    config = config or {}
    target = target or {}
    agent_mode = _agent_mode(config, target)
    leann_cfg = _leann_config(config)
    is_tool_agent = agent_mode == "tool_agent"
    raw_text = message if isinstance(message, str) else (message.get("content") or message.get("str_content") or message.get("message") or message.get("message_content") or "")
    raw_agent_mode = _is_raw_agent_mode(config, target)
    # Strip embedded image XML from WeChat mixed messages so it doesn't pollute the KB query.
    if isinstance(raw_text, str):
        raw_text = re.sub(r'<msg\b.*?</msg>', '', raw_text, flags=re.DOTALL).strip()
    # Vision routing: collect any image paths attached to the message (local_type==3 or aggregator-provided).
    image_paths = []
    if isinstance(message, dict):
        image_path = message.get("image_path")
        if image_path and os.path.isfile(str(image_path)):
            image_paths.append(str(image_path))
        # Strip raw binary placeholder from image messages so the LLM never sees "<bytes 516>"
        if isinstance(raw_text, str) and raw_text.startswith("<bytes "):
            raw_text = ""
        # Also pick up images from session context (e.g. user sent a photo then asked about it)
        session_img_paths = message.get("session_image_paths")
        if session_img_paths:
            import logging
            logging.getLogger('reply_engine').info('session_image_paths received: %s', session_img_paths)
            for p in session_img_paths:
                if not p:
                    continue
                sp = str(p)
                if os.path.isfile(sp):
                    if sp not in image_paths:
                        image_paths.append(sp)
                        logging.getLogger('reply_engine').info('session_image_paths added: %s', sp)
                else:
                    logging.getLogger('reply_engine').warning('session_image_paths file not found: %s', sp)
    # Route vision processing according to target config. In raw agent mode,
    # image recognition is delegated to the agent; WeChat side only passes paths.
    if image_paths and isinstance(message, dict) and not raw_agent_mode:
        vision_mode = (target.get("vision") or {}).get("mode", "agent_llm")
        if vision_mode == "llm_vision":
            raw_text = _try_llm_vision(image_paths[0], raw_text, target=target, config=config)
        elif vision_mode == "hook":
            hook_cmd = (target.get("vision") or {}).get("hook_cmd")
            if hook_cmd:
                raw_text = _try_vision_hook(image_paths[0], hook_cmd, raw_text, target=target, config=config)
        # agent_llm (default): image_paths flow into provider payload, agent handles vision
    triggers = target.get("triggers") or config.get("default_triggers") or DEFAULT_TRIGGERS
    clean = strip_triggers(raw_text, triggers)
    mention_name = _extract_mention_name(message)

    if raw_agent_mode:
        context_messages = (None if isinstance(message, str) else message.get('context_messages')) or []
        # ---- Aggregator summary for deep-agent context (M5) ----
        ctx = message.get("context_messages") or []
        parts = []
        for idx, cm in enumerate(ctx, start=1):
            text = str(cm.get("message_content") or cm.get("content") or "").strip()
            image_path = cm.get("image_path") or None
            parts.append({
                "index": idx,
                "local_id": int(cm.get("local_id") or 0),
                "sender": str(cm.get("sender_id") or ""),
                "sender_display_name": str(message.get("sender_display_name") or ""),
                "text": text,
                "image_path": image_path,
                "timestamp": str(cm.get("create_time") or ""),
            })
        aggregator_summary = {
            "is_aggregated": bool(message.get("is_aggregated")),
            "text_parts_count": int(message.get("text_parts_count") or len(parts)),
            "parts": parts,
            "conversation_id": f"{message.get('chat_id') or message.get('talker_id') or ''}::{message.get('sender_id') or message.get('username') or ''}",
        }
        # -------------------------------------------------------
        selected_kbs = resolve_target_kb_ids(config, target)
        target_policy = (None if isinstance(message, str) else message.get("target_policy")) or {}
        response_mode = _normalize_response_mode((target_policy or {}).get("mode") or target.get("mode") or "group_assistant")
        # The async deep-agent path previously forced tool_agent back to standard
        # because the Python-level tool loop lived in HermesProvider.run.  With
        # Hermes configured to use the WeChat/LEANN MCP servers, the async path can
        # now keep tool_agent mode and delegate tool execution to Hermes's native
        # MCP layer.  Synchronous _run_tool_agent_sync still handles tool_agent
        # directly when not queued.
        effective_agent_mode = agent_mode
        async_is_tool_agent = effective_agent_mode == "tool_agent"
        thin_monitor = _thin_monitor_enabled(config, target)
        delegate_to_agent = async_is_tool_agent or thin_monitor
        effective_skill_name = _resolve_skill_name(
            config, target, is_tool_agent=async_is_tool_agent, thin_monitor=thin_monitor
        )
        # KB retrieval debug for raw_agent mode: log query, hits, and selected KBs.
        # ---- Pre-compute image descriptions and inject into KB query ----
        # FTS query must see image content, otherwise image messages always miss the scene KB.
        # In thin-monitor mode the agent handles KB retrieval itself, but vision still
        # runs locally because the async Hermes job does not execute the Python tool loop
        # and native MCP vision execution depends on the user's Hermes config.  The
        # resulting descriptions are passed to the agent prompt.  Non-thin tool_agent
        # keeps the original agent-side vision path; standard mode pre-describes for KB.
        if thin_monitor:
            # Thin-monitor delegates vision to the agent's multimodal provider or the
            # decode_image MCP tool.  Keep image_paths in the payload and do not
            # pre-describe locally.
            vision_query_text, vision_image_descriptions = "", []
        elif delegate_to_agent:
            # Non-thin tool_agent: agent side handles vision directly.
            vision_query_text, vision_image_descriptions = "", []
        else:
            vision_query_text, vision_image_descriptions = _local_image_descriptions(
                image_paths, clean or raw_text, target=target, config=config
            )
        retrieval_debug = {"mode": "raw_agent", "selected_kbs": selected_kbs}
        if delegate_to_agent:
            # thin-monitor mode: agent retrieves knowledge on demand via tools.
            kb_layers = {"core": [], "scene": []}
            payload_knowledge_hits = []
            query = ""
            kb_query_text = clean or raw_text
        else:
            kb_query_text = clean or raw_text
            if vision_query_text:
                kb_query_text = "%s\n\n[图片识别结果]\n%s" % (kb_query_text, vision_query_text)
            query = _clean_query_for_fts(kb_query_text)
            kb_layers = retrieve_knowledge_layers(query, config, target)
        import logging
        logging.getLogger("reply_engine").warning(
            "[KB_DEBUG_RAW] raw=%r clean=%r kb_query=%r query=%r core=%d scene=%d kbs=%r target=%s agent_mode=%s",
            raw_text, clean, kb_query_text, query,
            len(kb_layers.get("core") or []),
            len(kb_layers.get("scene") or []),
            selected_kbs,
            target.get("username"),
            effective_agent_mode,
        )
        raw_kb_hits = (kb_layers.get("core") or []) + (kb_layers.get("scene") or [])
        try:
            payload_knowledge_hits = _knowledge_hits_to_payload(raw_kb_hits)
        except Exception as exc:
            logging.getLogger("reply_engine").exception("[KB_DEBUG_RAW] payload formatter crashed: %r", exc)
            payload_knowledge_hits = []
        scene_hits = kb_layers.get("scene") or []
        core_hits = kb_layers.get("core") or []
        retrieval_debug = {
            "mode": "raw_agent",
            "agent_mode": effective_agent_mode,
            "selected_kbs": selected_kbs,
            "core_hit_count": len(core_hits),
            "scene_hit_count": len(scene_hits),
        }
        logging.getLogger("reply_engine").warning(
            "[KB_DEBUG_RAW] enqueue hits count=%d target=%s agent_mode=%s",
            len(payload_knowledge_hits), target.get("username"), effective_agent_mode,
        )
        # Thin-monitor: the agent decides whether to clarify; do not short-circuit
        # here based on KB hit status.
        llm_config = dict(config.get("reply_engine", config) or {})
        image_descriptions: List[Dict[str, str]] = []
        if thin_monitor:
            # Thin-monitor: agent handles vision; keep image_paths only.
            image_descriptions = []
        elif delegate_to_agent:
            # Non-thin tool_agent: agent side handles vision directly.
            image_descriptions = []
        elif image_paths and not vision_image_descriptions:
            # Standard path: always attempt local recognition; the fallback chain
            # degrades through mmx → hooks → ocr → metadata.
            image_descriptions = _describe_images_for_agent(image_paths, clean or raw_text, target=target, config=config)
        else:
            image_descriptions = vision_image_descriptions
        prompt = _build_raw_agent_prompt(clean or raw_text, mention_name, response_mode, knowledge_hits=payload_knowledge_hits)
        # Inject readable aggregator context before the [用户消息] block
        if aggregator_summary["is_aggregated"] and aggregator_summary["text_parts_count"] >= 2:
            prefix_lines = [f"以下是你需要回复的连续对话（共 {aggregator_summary['text_parts_count']} 条消息）："]
            sender = message.get("sender_display_name") or message.get("mention_name") or "用户"
            for i, p in enumerate(aggregator_summary["parts"], start=1):
                text = p["text"]
                if p.get("image_path") and text:
                    text = f"[图片] {text}"
                elif p.get("image_path"):
                    text = "[图片]"
                prefix_lines.append(f"[{i}] {sender}: {text}")
            prompt = "\n".join(prefix_lines) + "\n\n" + prompt
        boundary = precheck(clean or raw_text)
        if boundary:
            boundary.reply_text = postcheck(boundary.reply_text)
            boundary.retrieval_debug = {"mode": "raw_agent", "selected_kbs": selected_kbs, "blocked_before_agent": True}
            return boundary
        # --- Task complexity routing (M2) ---
        # Decide whether this message should enter the job queue (deep_agent)
        # or be handled locally (fast_reply).  Must run BEFORE smalltalk check
        # because short messages like "分析这张图" are deep_agent, not smalltalk.
        route_decision = None
        if _HAS_TASK_ROUTER:
            local_type = message.get("local_type") if isinstance(message, dict) else None
            if aggregator_summary["is_aggregated"]:
                has_image = any(bool(p.get("image_path")) for p in aggregator_summary["parts"])
                last_part = aggregator_summary["parts"][-1]
                if has_image and str(last_part.get("text") or "").strip():
                    msg_type = "image"
                else:
                    msg_type = "text"
            else:
                msg_type = "text"
                if local_type == 3:
                    msg_type = "image"
                elif local_type == 34:
                    msg_type = "voice"
                elif local_type == 49:
                    msg_type = "file"
                has_image = bool(image_paths)
            route_decision = _task_router.route_message(  # type: ignore
                clean,
                message_type=msg_type,
                has_image=has_image,
                has_file=(local_type == 49),
            )
            if route_decision.route == _task_router.ROUTE_DEEP:  # type: ignore
                try:
                    job_key = "%s:%s" % (target.get("username", "unknown"), message.get("local_id", 0) if isinstance(message, dict) else 0)
                    group_key = target.get("username") or target.get("name") or "unknown"
                    payload_context_messages = context_messages[-10:] if isinstance(context_messages, list) else context_messages
                    job = _agent_jobs.enqueue_job(  # type: ignore
                        job_key=job_key,
                        group_key=group_key,
                        target_name=target.get("name"),
                        sender=mention_name,
                        message_local_id=message.get("local_id") if isinstance(message, dict) else None,
                        is_aggregated=message.get("is_aggregated") if isinstance(message, dict) else None,
                        aggregated_local_ids=message.get("aggregated_local_ids") if isinstance(message, dict) else None,
                        session_image_paths=message.get("session_image_paths") if isinstance(message, dict) else None,
                        text_parts_count=message.get("text_parts_count") if isinstance(message, dict) else None,
                        aggregator_summary=aggregator_summary,
                        agent_timeout=240.0 if image_paths else 90.0,
                        task_type="deep_agent",
                        payload=_json_safe({
                            "prompt": prompt,
                            "clean_text": clean,
                            "raw_text": raw_text,
                            "skill_name": effective_skill_name,
                            "knowledge_hits": payload_knowledge_hits,
                            "knowledge_bases": selected_kbs,
                            # Strict-pipeline hints consumed by ``_prepare_model_job``.
                            "_allowed_kb_ids": selected_kbs,
                            "_config_path": config_path,
                            "reply_mode": "raw_agent",
                            "agent_mode": effective_agent_mode,
                            "available_tools": _tool_agent_available_tools(config, target) if delegate_to_agent else [],
                            "leann": leann_cfg if delegate_to_agent else {},
                            "response_mode": response_mode,
                            "max_reply_chars": _max_reply_chars(config),
                            "target_policy": target_policy or {},
                            "mode_instruction": _mode_instruction(response_mode),
                            "retrieval_debug": {**retrieval_debug, "route": route_decision.route, "route_reason": route_decision.reason},
                            "image_paths": image_paths,
                            "image_descriptions": image_descriptions,
                            "context_messages": [] if thin_monitor else payload_context_messages,
                            "mention_name": mention_name,
                            "target": {
                                "name": target.get("name"),
                                "username": target.get("username"),
                                "table": target.get("table"),
                            },
                            "wechat_side": _wechat_side_payload(thin_monitor),
                        }),
                    )
                    ack_text, ack_now = _agent_ack(response_mode)
                    return ReplyDecision(
                        ack_now,
                        ack_text,
                        intent="deep_agent_queued",
                        risk_level="low",
                        need_human=False,
                        reason="deep_agent_enqueued",
                        wiki_hits=[],
                        retrieval_debug={
                            "mode": "raw_agent",
                            "route": route_decision.route,
                            "route_reason": route_decision.reason,
                            "selected_kbs": selected_kbs,
                        },
                    )
                except Exception:
                    pass
        is_smalltalk = _looks_like_smalltalk(clean)
        if not clean:
            reply = "我在，想聊什么直接说就行。"
            return ReplyDecision(True, postcheck(reply, config), intent="smalltalk",
                                 risk_level="low", need_human=False,
                                 reason="raw_agent_empty_message_fallback",
                                 wiki_hits=[],
                                 retrieval_debug=retrieval_debug)
        if _HAS_TASK_ROUTER:
            try:
                local_type = message.get("local_type") if isinstance(message, dict) else None
                route_name = getattr(route_decision, "route", "agent_provider")
                route_reason = getattr(route_decision, "reason", "agent_provider")
                job_key = "%s:%s" % (target.get("username", "unknown"), message.get("local_id", 0) if isinstance(message, dict) else 0)
                group_key = target.get("username") or target.get("name") or "unknown"
                payload_context_messages = context_messages[-10:] if isinstance(context_messages, list) else context_messages
                dedicated_instance_id = (_target_registry.get_target_dedicated_instance_id(target, config)
                                         if _target_registry else None)
                deep_agent_provider = target.get("agent_provider") or target.get("provider") or None
                if dedicated_instance_id and _target_registry:
                    inst = _target_registry.get_registered_agent_instance(config, dedicated_instance_id)
                    if inst:
                        deep_agent_provider = inst.get("provider") or deep_agent_provider
                job = _agent_jobs.enqueue_job(  # type: ignore
                    job_key=job_key,
                    group_key=group_key,
                    target_name=target.get("name"),
                    sender=mention_name,
                    message_local_id=message.get("local_id") if isinstance(message, dict) else None,
                    task_type=str(route_name or "agent_provider"),
                    provider=deep_agent_provider,
                    dedicated_agent_instance_id=dedicated_instance_id,
                    session_image_paths=message.get("session_image_paths") if isinstance(message, dict) else None,
                    text_parts_count=message.get("text_parts_count") if isinstance(message, dict) else None,
                    aggregator_summary=aggregator_summary,
                    agent_timeout=240.0 if image_paths else 90.0,
                    payload=_json_safe({
                        "prompt": prompt,
                        "clean_text": clean,
                        "raw_text": raw_text,
                        "message_type": "image" if local_type == 3 else ("voice" if local_type == 34 else ("file" if local_type == 49 else "text")),
                        "skill_name": effective_skill_name,
                        "knowledge_hits": payload_knowledge_hits,
                        "knowledge_bases": selected_kbs,
                        "reply_mode": "raw_agent",
                        "agent_mode": effective_agent_mode,
                        "available_tools": _tool_agent_available_tools(config, target) if delegate_to_agent else [],
                        "leann": leann_cfg if delegate_to_agent else {},
                        "response_mode": response_mode,
                        "max_reply_chars": _max_reply_chars(config),
                        "target_policy": target_policy or {},
                        "mode_instruction": _mode_instruction(response_mode),
                        "retrieval_debug": {**retrieval_debug, "route": str(route_name or "agent_provider"), "route_reason": str(route_reason or "agent_provider")},
                        "image_paths": image_paths,
                        "image_descriptions": image_descriptions,
                        "context_messages": [] if thin_monitor else payload_context_messages,
                        "mention_name": mention_name,
                        "route": str(route_name or "agent_provider"),
                        "route_reason": str(route_reason or "agent_provider"),
                        "wiki_dir": str(resolve_wiki_dir(config, target)),
                        "knowledge_base_specs": _selected_kb_specs(config, target),
                        "target": {
                            "id": target.get("id"),
                            "name": target.get("name"),
                            "username": target.get("username"),
                            "table": target.get("table"),
                        },
                        "wechat_side": _wechat_side_payload(thin_monitor),
                    }),
                )
                ack_text, ack_now = _agent_ack(response_mode)
                return ReplyDecision(
                    ack_now,
                    ack_text,
                    intent="agent_job_queued",
                    risk_level="low",
                    need_human=False,
                    reason="agent_provider_enqueued",
                    wiki_hits=[],
                    retrieval_debug={**retrieval_debug, "route": str(route_name or "agent_provider"), "route_reason": str(route_reason or "agent_provider")},
                )
            except Exception as e:
                return ReplyDecision(True, "复杂处理服务暂不可用，我先收到。",
                                     intent="agent_job_enqueue_failed", risk_level="low", need_human=False,
                                     reason="agent_provider_enqueue_failed:%r" % (e,), wiki_hits=[],
                                     retrieval_debug=retrieval_debug)
        return ReplyDecision(True, "复杂处理服务暂不可用，我先收到。",
                             intent="agent_job_router_unavailable", risk_level="low", need_human=False,
                             reason="agent_job_router_unavailable", wiki_hits=[],
                             retrieval_debug=retrieval_debug)
    if not clean:
        reply, intent, need_human = fallback_reply(clean, [])
        return ReplyDecision(True, postcheck(reply), intent=intent,
                             risk_level="medium" if need_human else "low",
                             need_human=need_human,
                             reason="empty_after_trigger_fallback",
                             wiki_hits=[])

    pre = precheck(clean)
    reply_mode = str(target.get("reply_mode") or config.get("default_reply_mode") or "standard").lower()
    if pre:
        pre.reply_text = postcheck(pre.reply_text)
        return pre

    else:
        if is_tool_agent:
            context_messages = (None if isinstance(message, str) else message.get('context_messages')) or []
            decision = _run_tool_agent_sync(
                raw_text, clean, mention_name, config, target, image_paths, context_messages
            )
            if decision is not None:
                return decision
            logger.warning(
                "tool_agent sync provider unavailable; falling back to standard retrieval"
            )
        layers = retrieve_knowledge_layers(clean or raw_text, config, target)
    core_hits = layers.get("core") or []
    raw_scene_hits = layers.get("scene") or []
    scene_hits = _strong_scene_hits(clean or raw_text, raw_scene_hits)

    # strict mode: refuse to reply when no scene wiki hit.
    if reply_mode == "strict" and not scene_hits:
        return ReplyDecision(
            should_reply=False,
            reply_text="",
            intent="strict_no_wiki",
            risk_level="low",
            need_human=False,
            reason="strict_mode_no_scene_hit",
            wiki_hits=[],
        )

    retrieval_debug = {
        "query": clean or raw_text,
        "reply_mode": reply_mode,
        "agent_mode": agent_mode,
        "selected_kbs": list(target.get("knowledge_bases") or resolve_target_kb_ids(config, target) or []),
        "core_count": len(core_hits),
        "raw_scene_count": len(raw_scene_hits),
        "strong_scene_count": len(scene_hits),
        "raw_scene_hits": [h.label if isinstance(h, KnowledgeHit) else str(h) for h in raw_scene_hits[:10]],
        "strong_scene_hits": [h.label if isinstance(h, KnowledgeHit) else str(h) for h in scene_hits[:10]],
    }
    mode = "scene" if scene_hits else "chat"
    wiki_hits = (core_hits + scene_hits) if scene_hits else core_hits
    context_messages = (None if isinstance(message, str) else message.get('context_messages')) or []
    mention_name = _extract_mention_name(message)
    prompt = build_prompt(raw_text, clean, wiki_hits, context_messages=context_messages, mention_name=mention_name, mode=mode, max_chars=_max_reply_chars(config))

    llm_config = dict(config.get("reply_engine", config) or {})
    if mode == "chat":
        # No scene wiki hit: this is the normal small-talk path. Give the LLM a short,
        # bounded window so the monitor never blocks the whole chat loop for minutes.
        llm_config["llm_timeout"] = float(llm_config.get("chat_llm_timeout", 20))
    provider_payload = {
        "prompt": prompt,
        "raw_text": raw_text,
        "clean_text": clean,
        "wiki_hits": [
            {
                "label": h.label,
                "source": h.source,
                "kb_id": h.kb_id,
                "scope": h.scope,
                "rel_path": h.rel_path,
                "score": h.score,
                "content": h.content,
            } if isinstance(h, KnowledgeHit) else {"label": h[0], "content": h[1]}
            for h in wiki_hits
        ],
        "context_messages": context_messages,
        "mention_name": mention_name,
        "mode": mode,
        "agent_mode": agent_mode,
        # tool_agent is handled earlier via _run_tool_agent_sync; this fallback path
        # runs standard retrieval and should not expose tools to the local prompt.
        "available_tools": [],
        "leann": {},
        "target": {
            "id": target.get("id"),
            "name": target.get("name"),
            "username": target.get("username"),
            "table": target.get("table"),
        },
        "retrieval_debug": retrieval_debug,
        "image_paths": image_paths,
    }
    llm_text = call_llm_provider(prompt, llm_config, provider_payload)
    if llm_text:
        llm_text = _clean_agent_output(llm_text)
    if llm_text:
        reply = postcheck(llm_text, config)
        return ReplyDecision(True, reply, intent="wiki_qa" if scene_hits else "smalltalk", risk_level="low", need_human=False,
                             reason="llm_provider_scene" if scene_hits else "llm_provider_core_chat",
                             wiki_hits=[h.label if isinstance(h, KnowledgeHit) else h[0] for h in wiki_hits],
                             retrieval_debug=retrieval_debug)

    reply, intent, need_human = fallback_reply(clean, scene_hits, mode=mode)
    return ReplyDecision(True, postcheck(reply, config), intent=intent,
                         risk_level="medium" if need_human else "low",
                         need_human=need_human,
                         reason="safe_fallback_scene_no_provider" if scene_hits else "safe_fallback_core_chat_no_provider",
                         wiki_hits=[h.label if isinstance(h, KnowledgeHit) else h[0] for h in wiki_hits],
                         retrieval_debug=retrieval_debug)


if __name__ == "__main__":
    sample = "小助手 你能做什么"
    d = generate_reply(sample, {}, {"default_triggers": DEFAULT_TRIGGERS})
    print(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
