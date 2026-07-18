#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Knowledge retrieval layer for the WeChat bot.

Owns all knowledge-base retrieval algorithms, the KnowledgeHit value type,
hit-to-payload conversion, and a target-authorized facade usable by MCP.

This module intentionally does NOT access runtime config files, secrets, or
WeChat data directly; callers supply the config dict and allowed KB IDs.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

try:
    import target_registry as _target_registry
except Exception:
    _target_registry = None  # type: ignore


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
_KB_INDEX_SCHEMA_VERSION = 1


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


@dataclass
class KnowledgeSearchResult:
    """Structured result from the target-authorized knowledge search facade.

    status values:
      - "ok": hits were returned.
      - "no_hit": no hits, but all reachable providers completed successfully.
      - "provider_failure": no hits, and at least one authorized provider failed.
      - "invalid": no KB IDs were authorized for this call or input was malformed.
    """

    status: str
    hits: List[Dict[str, Any]]
    provenance: List[Dict[str, Any]]
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "hits": self.hits,
            "provenance": self.provenance,
            "error": self.error,
        }


def _clean_query_for_fts(query: str) -> str:
    """Strip WeChat sender prefix, @mentions, filler words, and punctuation.

    The trigram FTS tokenizer needs tokens of 3+ characters to match CJK
    body content. Raw chat messages contain short prefixes/mentions that
    pollute the query, so we remove them before building the FTS expression.
    """
    q = query or ""
    q = re.sub(r"^[^:\n\uff1a]{1,40}[:\uff1a]\s*", "", q)
    q = re.sub(r"@[^\s\u2005]+[\s\u2005]*", "", q)
    for pat in _FTS_FILLER_WORDS:
        q = re.sub(pat, " ", q)
    q = re.sub(r"[，,。！？!?:：；;、]+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _query_tokens(query: str) -> List[str]:
    q = _clean_query_for_fts(query)
    tokens = [t for t in re.split(r"[\s,，。！？!?:：；;、/\\]+", q) if len(t) >= 2]
    cjk_spans = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    for span in cjk_spans:
        for n in (2, 3):
            if len(span) >= n:
                tokens.extend(span[i:i + n] for i in range(0, len(span) - n + 1))
    extra = ["小助手", "小助理", "帮助", "功能", "边界", "能做", "确认", "转达", "牛马", "重庆移动"]
    tokens += [t for t in extra if t in q]
    stop = {"什么", "怎么", "可以", "一下", "这个", "那个", "我们", "你能", "我是"}
    return [t for t in dict.fromkeys(tokens) if t not in stop]


def _score_doc(query: str, rel: str, body: str) -> int:
    score = 0
    for tok in _query_tokens(query):
        score += body.count(tok)
        score += rel.count(tok) * 3
    return score


def _strong_scene_hits(query: str, hits: List[KnowledgeHit], min_score: int = 2) -> List[KnowledgeHit]:
    """Keep only scene hits that still overlap with the original query locally."""
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
    """Resolve user-selected 0/N knowledge bases for a WeChat target."""
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


def _resolve_kb_spec(config: Dict[str, Any], target: Dict[str, Any], kb_id_or_spec: Any) -> Optional[Dict[str, Any]]:
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


def _local_kb_path(root: Path, spec: Dict[str, Any]) -> Optional[Path]:
    path_value = spec.get("path") or spec.get("dir")
    if not path_value:
        return None
    p = Path(path_value)
    if not p.is_absolute():
        p = root / p
    return p


def _try_retrieve_local_kb_fts(query: str, root: Path, spec: Dict[str, Any], limit: int) -> Tuple[List[KnowledgeHit], Optional[str]]:
    q = _fts_query(query)
    if not q:
        return [], None
    con = None
    try:
        con = _ensure_local_kb_fts(root, spec)
        if not con:
            return [], None
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
        return out, None
    except Exception as exc:
        return [], str(exc)
    finally:
        try:
            if con:
                con.close()
        except Exception:
            pass


def _retrieve_local_kb_fts(query: str, root: Path, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    hits, _ = _try_retrieve_local_kb_fts(query, root, spec, limit)
    return hits


def _try_retrieve_local_kb(query: str, root: Path, spec: Dict[str, Any], limit: int) -> Tuple[List[KnowledgeHit], Optional[str]]:
    path_value = spec.get("path") or spec.get("dir")
    if not path_value:
        return [], "missing local kb path"
    p = Path(path_value)
    if not p.is_absolute():
        p = root / p
    if not p.exists():
        return [], f"local kb path does not exist: {p}"

    indexed, fts_error = _try_retrieve_local_kb_fts(query, root, spec, limit)
    if indexed:
        return indexed, None

    docs = _load_local_kb_docs(root, spec)
    scored = []
    for rel, body in docs:
        score = _score_doc(query, rel, body)
        if score:
            scored.append((score, rel, body))
    if not scored:
        if fts_error:
            return [], f"local fts failed: {fts_error}"
        return [], None
    scored.sort(key=lambda x: (-x[0], x[1]))
    out = []
    for score, rel, body in scored[:limit]:
        max_chars = int(spec.get("hit_max_chars") or _DEFAULT_HIT_MAX_CHARS)
        out.append(KnowledgeHit("local", str(spec.get("id") or spec.get("path")), str(spec.get("scope") or "scene"), rel, body[:max_chars], score))
    return out, None


def _retrieve_local_kb(query: str, root: Path, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    hits, _ = _try_retrieve_local_kb(query, root, spec, limit)
    return hits


def _fts_query(query: str) -> str:
    toks = _query_tokens(query)
    if not toks:
        return ""
    return " OR ".join('"%s"' % t.replace('"', ' ') for t in toks[:12])


def _ensure_local_kb_fts(root: Path, spec: Dict[str, Any]) -> Optional[sqlite3.Connection]:
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


def _ima_query_variants(query: str, limit: int = 8) -> List[str]:
    """Generate conservative fallback queries for IMA search."""
    raw = query or ""
    variants: List[str] = []

    def add(q: str) -> None:
        q = re.sub(r"\s+", " ", q or "").strip(" ，,。！？!?:：；;、\n\t")
        if q and q not in variants:
            variants.append(q)

    add(raw)
    clean = re.sub(r"^[^:\n：]{1,40}[:：]\s*", "", raw)
    clean = re.sub(r"@[^\s\u2005]+[\s\u2005]*", "", clean)
    clean = _clean_query_for_fts(clean)
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
    focused = [t for t in key_toks if any(x in t for x in ("号", "认证", "办公", "权益", "套餐", "实名", "真实")) or len(t) >= 4]
    add(" ".join(focused[:8]))
    add(" ".join(key_toks[:8]))
    return variants[:limit]


def _get_secret_env(name: str) -> Optional[str]:
    """Return secret env value, falling back to Windows user environment."""
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


def _ima_auth(spec: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    client_env = spec.get("client_id_env") or "IMA_CLIENT_ID"
    api_env = spec.get("api_key_env") or "IMA_API_KEY"
    return _get_secret_env(client_env), _get_secret_env(api_env)


def _ima_post(spec: Dict[str, Any], api_path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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


def _try_retrieve_ima_kb_once(query: str, spec: Dict[str, Any], limit: int) -> Tuple[List[KnowledgeHit], Optional[str]]:
    client_id, api_key = _ima_auth(spec)
    kb_id = spec.get("knowledge_base_id") or spec.get("kb_id") or spec.get("id")
    if not client_id or not api_key:
        return [], "missing ima credentials"
    if not kb_id:
        return [], "missing ima knowledge_base_id"

    api_path = str(spec.get("api_path") or "openapi/wiki/v1/search_knowledge").lstrip("/")
    payload = _ima_post(spec, api_path, {"query": query or "", "cursor": "", "knowledge_base_id": str(kb_id)})
    if not isinstance(payload, dict):
        return [], "ima request failed"

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
    return out, None


def _retrieve_ima_kb_once(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    hits, _ = _try_retrieve_ima_kb_once(query, spec, limit)
    return hits


def _try_retrieve_ima_kb(query: str, spec: Dict[str, Any], limit: int) -> Tuple[List[KnowledgeHit], Optional[str]]:
    out: List[KnowledgeHit] = []
    seen: set = set()
    last_error: Optional[str] = None
    for q in _ima_query_variants(query):
        batch, err = _try_retrieve_ima_kb_once(q, spec, max(1, limit - len(out)))
        if err:
            last_error = err
        if not batch and spec.get("folder_id"):
            loose_spec = dict(spec)
            folder_id = str(loose_spec.pop("folder_id") or "")
            loose_hits, loose_err = _try_retrieve_ima_kb_once(q, loose_spec, max(1, limit - len(out)))
            if loose_err:
                last_error = loose_err
            prefix = folder_id.rstrip("/") + "/"
            batch = [h for h in loose_hits if str(h.rel_path).startswith(prefix)]
        for h in batch:
            key = (h.kb_id, h.rel_path)
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
            if len(out) >= limit:
                return out, None
    if out:
        return out, None
    return [], last_error


def _retrieve_ima_kb(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    hits, _ = _try_retrieve_ima_kb(query, spec, limit)
    return hits


import re as _re


def _sanitize_secrets(text: str) -> str:
    """Redact Feishu App IDs, secrets, and tokens from knowledge content."""
    text = _re.sub(r'cli_[a-z0-9]{16,32}', 'cli_***', text)
    text = _re.sub(r'(?i)secret[：:\s]*[A-Za-z0-9+/=_-]{24,64}', 'Secret：***', text)
    text = _re.sub(r'(?i)(api_key|app_secret|access_token|refresh_token|auth_key)[=：:\s]*[\'"]?[A-Za-z0-9+/=_-]{24,}', r'\1=***', text)
    return text


def _try_retrieve_getnote_kb(query: str, spec: Dict[str, Any], limit: int) -> Tuple[List[KnowledgeHit], Optional[str]]:
    kb_id = str(spec.get("knowledge_base_id") or spec.get("kb_id") or spec.get("id") or "").strip()
    if not kb_id:
        return [], "missing getnote knowledge_base_id"
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
        return [], str(exc)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return [], f"getnote exited {proc.returncode}"
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        return [], f"invalid getnote json: {exc}"
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    items = data.get("results") or data.get("notes") or []
    if not isinstance(items, list):
        return [], "invalid getnote results"
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
    return out, None


def _retrieve_getnote_kb(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    hits, _ = _try_retrieve_getnote_kb(query, spec, limit)
    return hits


def _try_retrieve_hook_kb(query: str, spec: Dict[str, Any], limit: int) -> Tuple[List[KnowledgeHit], Optional[str]]:
    kb_id = str(spec.get("knowledge_base_id") or spec.get("kb_id") or spec.get("id") or "").strip()
    exe = str(spec.get("executable") or spec.get("cli") or "").strip()
    if not exe:
        return [], "missing hook executable"
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
    except Exception as exc:
        return [], str(exc)
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return [], f"hook exited {proc.returncode}"
    try:
        payload = json.loads(proc.stdout)
    except Exception as exc:
        return [], f"invalid hook json: {exc}"
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    items = data.get("results") or data.get("notes") or []
    if not isinstance(items, list):
        return [], "invalid hook results"
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
    return out, None


def _retrieve_hook_kb(query: str, spec: Dict[str, Any], limit: int) -> List[KnowledgeHit]:
    hits, _ = _try_retrieve_hook_kb(query, spec, limit)
    return hits


def _try_retrieve_leann_kb(query: str, spec: Dict[str, Any], per_limit: int, config: Dict[str, Any], config_path: Optional[str] = None) -> Tuple[List[KnowledgeHit], Optional[str]]:
    if not _target_registry:
        return [], "target_registry unavailable"
    kb_id = str(spec.get("id") or spec.get("knowledge_base_id") or "leann")
    try:
        search_kwargs = {"spec": spec, "query": query, "limit": per_limit, "cfg": config}
        if config_path:
            search_kwargs["config_path"] = config_path
        result = _target_registry.search_leann_kb(**search_kwargs)
    except Exception as exc:
        return [], str(exc)
    hits = result.get("hits") if isinstance(result, dict) else []
    error = str(result.get("error") or "").strip() if isinstance(result, dict) else "invalid leann search result"
    if not hits:
        return [], error or None
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
    return out, None


def _retrieve_leann_kb(query: str, spec: Dict[str, Any], per_limit: int, config: Dict[str, Any]) -> List[KnowledgeHit]:
    hits, _ = _try_retrieve_leann_kb(query, spec, per_limit, config)
    return hits


def retrieve_knowledge_layers(query: str, config: Dict[str, Any], target: Dict[str, Any],
                               limit: int = 6, core_limit: Optional[int] = None,
                               scene_limit: Optional[int] = None, skip_core: bool = False) -> Dict[str, List[KnowledgeHit]]:
    root = _kb_root(config)
    core_hits: List[KnowledgeHit] = []
    scene_hits: List[KnowledgeHit] = []

    cfg_re = config.get("reply_engine") or {}
    core_limit = int(core_limit if core_limit is not None else cfg_re.get("core_limit", 3))
    scene_limit = int(scene_limit if scene_limit is not None else cfg_re.get("scene_limit", max(0, limit - core_limit)))

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

    scene_hits.sort(key=lambda h: h.score, reverse=True)
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


def _search_one_kb(query: str, root: Path, config: Dict[str, Any], target: Dict[str, Any],
                   spec: Dict[str, Any], limit: int,
                   config_path: Optional[str] = None) -> Tuple[List[KnowledgeHit], Dict[str, Any]]:
    """Retrieve one resolved KB and return provenance metadata.

    Provenance keys: kb_id, kb_type, status, count, error.
    """
    kb_id = str(spec.get("id") or "unknown")
    kb_type = str(spec.get("type") or "local").lower()
    if spec.get("enabled", True) is False:
        return [], {"kb_id": kb_id, "kb_type": kb_type, "status": "skipped", "count": 0, "error": "disabled"}

    cfg_re = config.get("reply_engine") or {}
    disable_local = bool(cfg_re.get("disable_local_kb"))
    if disable_local and kb_type == "local":
        return [], {"kb_id": kb_id, "kb_type": kb_type, "status": "skipped", "count": 0, "error": "local_kb_disabled"}

    per_limit = int(spec.get("limit") or limit)
    batch: List[KnowledgeHit] = []
    error: Optional[str] = None
    try:
        if kb_type == "local":
            batch, error = _try_retrieve_local_kb(query, root, spec, per_limit)
        elif kb_type == "ima":
            batch, error = _try_retrieve_ima_kb(query, spec, per_limit)
        elif kb_type == "getnote":
            batch, error = _try_retrieve_getnote_kb(query, spec, per_limit)
        elif kb_type == "hook":
            batch, error = _try_retrieve_hook_kb(query, spec, per_limit)
        elif kb_type == "leann":
            batch, error = _try_retrieve_leann_kb(query, spec, per_limit, config, config_path=config_path)
        else:
            error = f"unknown kb type: {kb_type}"
    except Exception as exc:
        error = str(exc)

    status = "hit" if batch else ("failure" if error else "no_hit")
    provenance = {
        "kb_id": kb_id,
        "kb_type": kb_type,
        "status": status,
        "count": len(batch),
        "error": error,
    }
    return batch, provenance


def search_knowledge(
    query: str,
    config: Dict[str, Any],
    allowed_kb_ids: List[str],
    limit: int = 5,
    core_limit: int = 0,
    scene_limit: Optional[int] = None,
    config_path: Optional[str] = None,
) -> KnowledgeSearchResult:
    """Target-authorized knowledge search facade for MCP.

    Args:
        query: user query text.
        config: reply engine / bot configuration dict.
        allowed_kb_ids: explicit list of KB IDs the target is authorized to use.
        limit: max total hits (core + scene).
        core_limit: max core hits; 0 means skip core (MCP default).
        scene_limit: max scene hits; defaults to limit.

    Returns:
        KnowledgeSearchResult with status, hits, provenance, and error.
    """
    if not allowed_kb_ids:
        return KnowledgeSearchResult(
            status="invalid",
            hits=[],
            provenance=[],
            error="no authorized knowledge bases",
        )

    root = _kb_root(config)
    target: Dict[str, Any] = {"knowledge_bases": allowed_kb_ids}
    wiki_dir_override = config.get("wiki_dir")
    if wiki_dir_override:
        target["wiki_dir"] = str(wiki_dir_override)

    all_hits: List[KnowledgeHit] = []
    provenance: List[Dict[str, Any]] = []
    any_failure = False

    if core_limit and core_limit > 0:
        core_docs = load_wiki(root / "core")
        core_ranked = _rank_wiki_docs(query, [(f"core/{rel}", body) for rel, body in core_docs], limit=core_limit)
        core_hits = [KnowledgeHit("local", "core", "first_principles", rel, body, _score_doc(query, rel, body) or 1)
                     for rel, body in core_ranked]
        all_hits.extend(core_hits)
        provenance.append({
            "kb_id": "core",
            "kb_type": "local",
            "status": "hit" if core_hits else "no_hit",
            "count": len(core_hits),
            "error": None,
        })

    effective_scene_limit = scene_limit if scene_limit is not None else limit
    for kb_id in allowed_kb_ids:
        spec = _resolve_kb_spec(config, target, kb_id)
        if not spec:
            provenance.append({
                "kb_id": kb_id,
                "kb_type": "unknown",
                "status": "failure",
                "count": 0,
                "error": "kb spec not found",
            })
            any_failure = True
            continue
        batch, meta = _search_one_kb(query, root, config, target, spec, effective_scene_limit, config_path)
        all_hits.extend(batch)
        provenance.append(meta)
        if meta.get("status") == "failure":
            any_failure = True

    # Deduplicate across providers by source/kb_id/rel_path, preserving score order.
    all_hits.sort(key=lambda h: h.score, reverse=True)
    seen: set = set()
    deduped: List[KnowledgeHit] = []
    for h in all_hits:
        key = (h.source, h.kb_id, h.rel_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(h)

    total_limit = (core_limit or 0) + (scene_limit if scene_limit is not None else limit)
    final_hits = deduped[:total_limit]
    payload_hits = _knowledge_hits_to_payload(final_hits)

    if payload_hits:
        status = "ok"
        error = None
    elif any_failure:
        status = "provider_failure"
        error = "one or more authorized knowledge providers failed"
    else:
        status = "no_hit"
        error = None

    return KnowledgeSearchResult(
        status=status,
        hits=payload_hits,
        provenance=provenance,
        error=error,
    )


__all__ = [
    "KnowledgeHit",
    "KnowledgeSearchResult",
    "_knowledge_hits_to_payload",
    "search_knowledge",
    "retrieve_knowledge",
    "retrieve_knowledge_layers",
    "resolve_target_kb_ids",
    "load_wiki",
    "retrieve_wiki",
    "retrieve_scoped_wiki",
    "diagnose_local_kb",
    "_strong_scene_hits",
    "_clean_query_for_fts",
    "_resolve_kb_spec",
    "_knowledge_bases",
    "_kb_root",
]
