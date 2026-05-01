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
import shlex
from typing import Any, Dict, Iterable, List, Tuple


DEFAULT_TRIGGERS = ["@群聊小助手", "群聊小助手", "小助理", "小助手"]
MAX_REPLY_CHARS = 300

HIGH_RISK_PATTERNS = [
    "转账", "付款", "打款", "收款码", "银行卡", "验证码", "密码", "密钥", "token", "api key",
    "登录", "删", "删除", "格式化", "改配置", "系统设置", "发文件", "聊天记录", "数据库",
    "内部日志", "路径", "keys.json", "忽略之前", "忽略以上", "绕过", "越权", "退群", "踢人", "移出群",
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


def strip_group_sender_prefix(text: str) -> str:
    """Remove the decrypted group sender prefix: "username:\ncontent"."""
    text = text or ""
    return re.sub(r"^[^:\n]{1,80}:\n", "", text, count=1)


def strip_triggers(text: str, triggers: Iterable[str] | None = None) -> str:
    text = strip_group_sender_prefix(text or "")
    triggers = list(triggers or DEFAULT_TRIGGERS)
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
        "资料", "知识库", "产品", "业务", "方案", "套餐", "认证", "实名", "真实号", "工作号", "权益", "重庆", "移动", "中移", "介绍", "是什么", "怎么", "如何", "多少", "价格", "报价", "合同", "授权", "审批", "记录", "笔记"
    ]):
        return True
    return _contains_any(t, [
        "哈哈", "笑死", "讲个笑话", "开个玩笑", "在吗", "在不在", "早上好", "下午好", "晚上好", "无聊", "天气不错", "吃饭了吗", "你觉得呢", "咋样啊", "聊聊"
    ])


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


def _query_tokens(query: str) -> List[str]:
    q = query or ""
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
    return [(rel, body[:1200]) for _, rel, body in scored[:limit]]


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
    docs = _load_local_kb_docs(root, spec)
    scored = []
    for rel, body in docs:
        score = _score_doc(query, rel, body)
        if score:
            scored.append((score, rel, body))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, rel, body in scored[:limit]:
        out.append(KnowledgeHit("local", str(spec.get("id") or spec.get("path")), str(spec.get("scope") or "scene"), rel, body[:1200], score))
    return out


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
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
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
        note_id = str(item.get("note_id") or item.get("id") or f"result_{idx + 1}")
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or item.get("summary") or "").strip()
        created = str(item.get("created_at") or "").strip()
        body = "\n".join(x for x in [title, created, content] if x).strip()
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
        proc = subprocess.run([exe], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
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

def retrieve_knowledge_layers(query: str, config: Dict[str, Any], target: Dict[str, Any], limit: int = 6) -> Dict[str, List[KnowledgeHit]]:
    root = _kb_root(config)
    core_hits: List[KnowledgeHit] = []
    scene_hits: List[KnowledgeHit] = []

    # First principle: core rules/boundaries always apply for every target.
    core_docs = load_wiki(root / "core")
    core_ranked = _rank_wiki_docs(query, [(f"core/{rel}", body) for rel, body in core_docs], limit=3)
    for rel, body in core_ranked:
        core_hits.append(KnowledgeHit("local", "core", "first_principles", rel, body, _score_doc(query, rel, body) or 1))

    selected = target.get("knowledge_bases")
    if selected is None:
        selected = resolve_target_kb_ids(config, target)
    for kb in selected or []:
        spec = _resolve_kb_spec(config, target, kb)
        if not spec or spec.get("enabled", True) is False:
            continue
        typ = str(spec.get("type") or "local").lower()
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
        for h in batch:
            if h.scope == "core" or str(h.rel_path).startswith("core/"):
                core_hits.append(h)
            else:
                scene_hits.append(h)

    return {"core": core_hits[:3], "scene": scene_hits[:max(0, limit - min(len(core_hits), 3))]}


def retrieve_knowledge(query: str, config: Dict[str, Any], target: Dict[str, Any], limit: int = 6) -> List[KnowledgeHit]:
    layers = retrieve_knowledge_layers(query, config, target, limit=limit)
    return (layers["core"] + layers["scene"])[:limit]


def precheck(user_text: str) -> ReplyDecision | None:
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


def postcheck(text: str) -> str:
    text = text or ""
    blocked = ["keys.json", "MINIMAX_API_KEY", "数据库", "内部日志", "自动化实现", "系统路径"]
    if _contains_any(text, blocked):
        return "这个问题我先收到啦，涉及内部信息或需要确认的内容，需要本人确认后再处理。"
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS - 1].rstrip() + "…"
    return text


def _clean_agent_output(text: str) -> str:
    text = text or ""
    text = text.replace("[ROUND END]", "")
    text = re.sub(r"<summary>[\s\S]*?</summary>", "", text).strip()
    # Prefer the final non-empty prose line; discard obvious tool/code noise.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bad_prefix = ("LLM Running", "🛠️", "```", "<thinking>", "</thinking>", "<summary>")
    useful = [ln for ln in lines if not ln.startswith(bad_prefix) and not ln.startswith("{") and not ln.startswith("}")]
    return " ".join(useful[-3:]).strip() if useful else text.strip()


_LOCAL_LLM_CLIENTS: Dict[str, Any] = {}
_LOCAL_LLM_LOCK = threading.Lock()


def _resolve_code_root(config: Dict[str, Any]) -> Path | None:
    """Resolve external agent runtime root used to import llmcore in-process."""
    cfg_root = config.get("code_root")
    if cfg_root:
        p = Path(cfg_root).expanduser().resolve()
        return p if p.exists() else None
    # Attempt to locate via the wechat-auto CLI entry point (pip-installed)
    try:
        import shutil
        exe = shutil.which("wechat-auto")
        if exe:
            p = Path(exe).resolve().parent
            # If installed in Scripts/ or bin/, go up one level
            if p.name.lower() in ("scripts", "bin"):
                p = p.parent
            return p if p.exists() else None
    except Exception:
        pass
    # Project root detection fallback: two levels up from this file.
    p = Path(__file__).resolve().parents[2]
    return p if p.exists() else None


def _load_local_llm_client(config: Dict[str, Any]):
    """Build one GenericAgent llmcore client without spawning agentmain/subagent."""
    code_root = _resolve_code_root(config)
    if code_root is None:
        return None
    cache_key = f"{code_root}|{config.get('llm_no', 0)}"
    with _LOCAL_LLM_LOCK:
        cached = _LOCAL_LLM_CLIENTS.get(cache_key)
        if cached is not None:
            return cached
        llmcore_path = code_root / "llmcore.py"
        if not llmcore_path.exists():
            return None
        root_str = str(code_root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        try:
            from llmcore import (  # type: ignore
                reload_mykeys, LLMSession, ToolClient, ClaudeSession, MixinSession,
                NativeToolClient, NativeClaudeSession, NativeOAISession,
            )
            mykeys, _changed = reload_mykeys()
            sessions: List[Any] = []
            for key, cfg in mykeys.items():
                if not any(x in key for x in ["api", "config", "cookie"]):
                    continue
                try:
                    if "native" in key and "claude" in key:
                        sessions.append(NativeToolClient(NativeClaudeSession(cfg=cfg)))
                    elif "native" in key and "oai" in key:
                        sessions.append(NativeToolClient(NativeOAISession(cfg=cfg)))
                    elif "claude" in key:
                        sessions.append(ToolClient(ClaudeSession(cfg=cfg)))
                    elif "oai" in key:
                        sessions.append(ToolClient(LLMSession(cfg=cfg)))
                    elif "mixin" in key:
                        sessions.append({"mixin_cfg": cfg})
                except Exception:
                    continue
            for i, sess in enumerate(list(sessions)):
                if isinstance(sess, dict) and "mixin_cfg" in sess:
                    try:
                        mixin = MixinSession(sessions, sess["mixin_cfg"])
                        if isinstance(mixin._sessions[0], (NativeClaudeSession, NativeOAISession)):
                            sessions[i] = NativeToolClient(mixin)
                        else:
                            sessions[i] = ToolClient(mixin)
                    except Exception:
                        sessions[i] = None
            sessions = [s for s in sessions if s is not None and not isinstance(s, dict)]
            if not sessions:
                return None
            llm_no = int(config.get("llm_no", 0) or 0)
            client = sessions[llm_no % len(sessions)]
            _LOCAL_LLM_CLIENTS[cache_key] = client
            return client
        except Exception:
            return None


def _call_local_llm_provider(prompt: str, config: Dict[str, Any]) -> str | None:
    provider = str(config.get("provider") or "").lower()
    if provider and provider not in {"genericagent_local", "genericagent_llmcore", "genericagent_subagent"}:
        return None
    client = _load_local_llm_client(config)
    if client is None:
        return None
    input_text = (
        "你是群聊小助手。请根据下面的群消息、本地wiki片段和边界约束，"
        "只输出一段可以直接发送到微信群的中文回复；不要解释过程，不要使用工具，不要写summary。\n\n"
        + prompt
    )
    messages = [{"role": "user", "content": input_text}]
    try:
        gen = client.chat(messages, tools=[])
        try:
            while True:
                next(gen)
        except StopIteration as e:
            resp = e.value
    except Exception:
        return None
    if resp is not None and getattr(resp, "content", None):
        return postcheck(str(resp.content).strip())
    return None


def _call_subagent_provider(prompt: str, config: Dict[str, Any]) -> str | None:
    if not config.get("use_subagent", False):
        return None
    code_root = _resolve_code_root(config)
    if code_root is None:
        return None
    agentmain = code_root / "agentmain.py"
    if not agentmain.exists():
        return None
    task_prefix = str(config.get("subagent_task_prefix") or "wechat_reply")
    task = f"{task_prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    llm_no = str(config.get("llm_no", 1))
    timeout = float(config.get("llm_timeout", 120))
    input_text = (
        "你是群聊小助手。请根据下面的群消息、本地wiki片段和边界约束，"
        "只输出一段可以直接发送到微信群的中文回复；不要解释过程，不要使用工具，不要写summary。\n\n"
        + prompt
    )
    try:
        # agentmain.py writes task output under <code_root>/temp/<task> and does not support --temp_root.
        # Keep archive/temp_root config for callers that manage files externally, but do not pass it to CLI.
        cmd = [sys.executable, str(agentmain), "--task", task, "--input", input_text, "--llm_no", llm_no]
        proc = subprocess.Popen(
            cmd, cwd=str(code_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace"
        )
        out_path = code_root / "temp" / task / "output.txt"
        deadline = time.time() + timeout
        polled = False
        while time.time() < deadline:
            time.sleep(0.5)
            if out_path.exists():
                polled = True
                text = out_path.read_text(encoding="utf-8", errors="replace")
                if "[ROUND END]" in text:
                    proc.terminate()
                    proc.wait(timeout=5)
                    return _clean_agent_output(text)
        # timeout: gracefully terminate
        if not polled and proc.poll() is None:
            # maybe output not created yet, fallback to stdout on quick failure
            try:
                out, err = proc.communicate(timeout=10)
                return _clean_agent_output(out) if out and out.strip() else None
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if out_path.exists():
            text = out_path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return _clean_agent_output(text)
    except Exception:
        return None
    return None


def _extract_command_reply(out: str) -> str | None:
    """Extract reply from command stdout.

    Preferred stdout is a single JSON object, but some agent apps print logs
    before the final JSON. Be forgiving: scan from the last non-empty line
    backwards and accept JSON dict/string; otherwise return the last line.
    """
    out = (out or "").strip()
    if not out:
        return None
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    candidates = list(reversed(lines)) + [out]
    for cand in candidates:
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

    This is the product-friendly path for users who already have OpenClaw,
    Hermes, GenericAgent, or any other agent app configured with its own LLM.

    Supported config:
      provider: "command"
      cmd: ["agent", "--single-turn"] or "agent --single-turn"
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
        r = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    if not out:
        return None
    return _extract_command_reply(out)


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

    # Existing GA in-process adapter stays available as just another agent app.
    local = _call_local_llm_provider(prompt, config)
    if local:
        return local
    # Backward-compatible escape hatch only; sub-wechat should not spawn subagent during normal replies.
    if config.get("allow_subagent_fallback", False):
        sub = _call_subagent_provider(prompt, config)
        if sub:
            return sub
    # Legacy command fallback for older configs without provider="command".
    cmd_reply = _call_command_provider(prompt, config, payload)
    if cmd_reply:
        return postcheck(cmd_reply)
    return None


def fallback_reply(clean_text: str, wiki_hits: List[Tuple[str, str]], mode: str = "scene") -> Tuple[str, str, bool]:
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
        raw = str(message.get("message_content") or message.get("content") or "")
    else:
        raw = str(message or "")
    # Group messages in decrypted DB commonly start with: sender:\ncontent.
    m = re.match(r"^([^:\n]{1,80}):\n", raw)
    if m:
        return m.group(1).strip().lstrip("@")
    return ""


def _ensure_mention_prefix(reply: str, mention_name: str) -> str:
    text = (reply or "").strip()
    name = (mention_name or "").strip().lstrip("@")
    if not text or not name:
        return text
    if text.startswith("@"):
        return text
    return f"@{name} {text}"


def build_prompt(raw_text: str, clean_text: str, wiki_hits: List[Any], context_messages: list | None = None, mention_name: str = "", mode: str = "scene") -> str:
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
    return f"""你是群聊小助手，只在微信群中被明确叫到时回复。\n强边界：不能冒充本人；不能替群主/负责人承诺、授权、报价、决策或执行高风险操作；不能泄露密钥、系统路径、数据库、内部日志、自动化实现细节；知识库无依据时说明不确定。\n回复要求：简短、自然、适合微信群，最多{MAX_REPLY_CHARS}字；{mention_rule}\n任务策略：{task_rule}\n\n{('[群聊上下文]\n' + ctx_block + '\n\n') if ctx_lines else ''}[群消息]\n{raw_text}\n\n[清洗后问题]\n{clean_text}\n\n{wiki_label}\n{wiki}\n\n请只输出要发送到微信群的一段中文回复。"""


def generate_reply(message: Dict[str, Any] | str,
                   target: Dict[str, Any] | None = None,
                   config: Dict[str, Any] | None = None) -> ReplyDecision:
    config = config or {}
    target = target or {}
    raw_text = message if isinstance(message, str) else (message.get("content") or message.get("str_content") or message.get("message") or message.get("message_content") or "")
    triggers = target.get("triggers") or config.get("default_triggers") or DEFAULT_TRIGGERS
    clean = strip_triggers(raw_text, triggers)
    mention_name = _extract_mention_name(message)

    if not clean:
        reply, intent, need_human = fallback_reply(clean, [])
        return ReplyDecision(True, _ensure_mention_prefix(postcheck(reply), mention_name), intent=intent,
                             risk_level="medium" if need_human else "low",
                             need_human=need_human,
                             reason="empty_after_trigger_fallback",
                             wiki_hits=[])

    pre = precheck(clean)
    if pre:
        pre.reply_text = _ensure_mention_prefix(postcheck(pre.reply_text), mention_name)
        return pre

    # Retrieval comes before chat classification.  A message with scene KB hits is
    # by definition a knowledge-bound question, even if it looks casual/short.
    # If retrieval yields no scene hits, core boundaries still apply and the
    # request falls back to bounded chat mode.
    layers = retrieve_knowledge_layers(clean or raw_text, config, target)
    core_hits = layers.get("core") or []
    raw_scene_hits = layers.get("scene") or []
    scene_hits = _strong_scene_hits(clean or raw_text, raw_scene_hits)
    retrieval_debug = {
        "query": clean or raw_text,
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
    prompt = build_prompt(raw_text, clean, wiki_hits, context_messages=context_messages, mention_name=mention_name, mode=mode)

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
        "target": {
            "id": target.get("id"),
            "name": target.get("name"),
            "username": target.get("username"),
            "table": target.get("table"),
        },
        "retrieval_debug": retrieval_debug,
    }
    llm_text = call_llm_provider(prompt, llm_config, provider_payload)
    if llm_text:
        llm_text = _clean_agent_output(llm_text)
    if llm_text:
        reply = _ensure_mention_prefix(postcheck(llm_text), mention_name)
        return ReplyDecision(True, reply, intent="wiki_qa" if scene_hits else "smalltalk", risk_level="low", need_human=False,
                             reason="llm_provider_scene" if scene_hits else "llm_provider_core_chat",
                             wiki_hits=[h.label if isinstance(h, KnowledgeHit) else h[0] for h in wiki_hits],
                             retrieval_debug=retrieval_debug)

    reply, intent, need_human = fallback_reply(clean, scene_hits, mode=mode)
    return ReplyDecision(True, _ensure_mention_prefix(postcheck(reply), mention_name), intent=intent,
                         risk_level="medium" if need_human else "low",
                         need_human=need_human,
                         reason="safe_fallback_scene_no_provider" if scene_hits else "safe_fallback_core_chat_no_provider",
                         wiki_hits=[h.label if isinstance(h, KnowledgeHit) else h[0] for h in wiki_hits],
                         retrieval_debug=retrieval_debug)


if __name__ == "__main__":
    sample = "小助手 你能做什么"
    d = generate_reply(sample, {}, {"default_triggers": DEFAULT_TRIGGERS})
    print(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))