#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Digest topics service: background LLM summaries for WeChat targets.

External dependencies are injectable; production defaults live in function
signatures. The default runner calls the Hermes CLI via agent_provider.
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import traceback
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, List, Optional, Tuple

import zstandard as zstd

from target_registry import DECRYPTED_MESSAGE_DIR, connect_ro, msg_table

ROOT = Path(__file__).resolve().parent
DIGEST_DIR = ROOT / "temp" / "digests"
DIGEST_TTL_SECONDS = 600
MIN_REBUILD_INTERVAL = 30
MAX_MESSAGES = 400
MAX_CHARS = 12000
LLM_TIMEOUT = 180

# Single-flight builds: target_id -> start timestamp of currently running build.
_BUILDING: Dict[str, float] = {}
# Last build start timestamp for cooldown (MIN_REBUILD_INTERVAL).
_LAST_BUILD_START: Dict[str, float] = {}
_BUILDING_LOCK = threading.Lock()


class DigestError(Exception):
    """Raised when digest building or its runner cannot proceed."""


PROMPT_TEMPLATE = """你是群聊内容分析助手。下面是微信群「{name}」今天（{date}）的消息记录（格式：HH:MM 内容）。
请归纳今天大家讨论的主要话题，最多 6 个。
要求：
- 只输出 JSON，不要输出任何其他文字。
- 格式：{"topics":[{"title":"话题标题（10字内）","summary":"一两句话概括讨论内容与结论","keywords":["关键词1","关键词2"]}]}
- 按讨论热度从高到低排序；不要编造消息中没有的内容。
- 如果消息没有实质内容，输出 {"topics":[]}。
消息记录：
{lines}
"""


def _local_day_start(now: Optional[float] = None) -> float:
    """Return the Unix timestamp for 00:00:00 of the local calendar day."""
    if now is None:
        now = time.time()
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _today_text(now: Optional[float] = None) -> str:
    """Return local calendar date as YYYY-MM-DD."""
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def _cache_path(target_id: str, digest_dir: Path) -> Path:
    """Path to the per-target JSON cache."""
    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(str(target_id).encode("utf-8")).hexdigest()
    return digest_dir / f"topics_{h}.json"


def _load_cache(target_id: str, digest_dir: Path) -> Optional[Dict[str, Any]]:
    """Load cached digest for a target; return None if missing or invalid."""
    path = _cache_path(target_id, digest_dir)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        return data
    except Exception:
        return None


def _save_cache(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write a JSON cache with tmp + os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(path))
    except Exception:
        # Best-effort cleanup on failure; never raise here so the worker ends
        # cleanly and the in-flight marker is removed.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _read_today_text_messages(
    target: Dict[str, Any],
    message_dir: Path,
    day_start: float,
) -> Tuple[List[Tuple[float, str]], int]:
    """Read today's text messages for a target.

    Returns (messages, decompression_failures). Each message is a tuple of
    (create_time, text). The third return is intentionally the count of rows
    that could not be decompressed, so callers can decide whether to log it.
    """
    message_dir = Path(message_dir)
    db_name = str(target.get("db") or "message_0.db")
    username = str(target.get("username") or "")
    table = str(target.get("table") or "").strip() or msg_table(username)
    db_path = message_dir / db_name
    if not db_path.exists():
        return [], 0

    def _decode(content: Any, ct: Any) -> Optional[str]:
        if ct == 4 and isinstance(content, bytes):
            try:
                return zstd.decompress(content).decode("utf-8", "replace")
            except Exception:
                return None
        if isinstance(content, bytes):
            return content.decode("utf-8", "replace")
        return None if content is None else str(content)

    base_sql = (
        f"SELECT create_time, message_content, WCDB_CT_message_content "
        f"FROM [{table}] WHERE local_type = 1 AND create_time >= ? "
        f"AND (status IS NULL OR status != 2) ORDER BY create_time ASC"
    )
    fallback_sql = (
        f"SELECT create_time, message_content, WCDB_CT_message_content "
        f"FROM [{table}] WHERE local_type = 1 AND create_time >= ? "
        f"ORDER BY create_time ASC"
    )

    rows: List[Tuple[Any, Any, Any]] = []
    try:
        con = connect_ro(db_path)
        try:
            rows = con.execute(base_sql, (day_start,)).fetchall()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "status" in msg or "wcdb_ct_message_content" in msg or "column" in msg:
                con.close()
                con = connect_ro(db_path)
                rows = con.execute(fallback_sql, (day_start,)).fetchall()
            else:
                raise
        finally:
            try:
                con.close()
            except Exception:
                pass
    except Exception:
        return [], 0

    messages: List[Tuple[float, str]] = []
    failures = 0
    for row in rows:
        try:
            create_time = float(row[0]) if row[0] is not None else 0.0
            content = row[1]
            ct = row[2]
            text = _decode(content, ct)
            if text is None:
                failures += 1
                continue
            text = text.strip()
            if not text:
                continue
            messages.append((create_time, text))
        except Exception:
            failures += 1
            continue
    return messages, failures


def _format_prompt(name: str, date: str, messages: List[Tuple[float, str]]) -> str:
    """Format a prompt from the message list, applying truncation rules."""
    # Take the most recent MAX_MESSAGES.
    if len(messages) > MAX_MESSAGES:
        messages = messages[-MAX_MESSAGES:]

    lines = [
        f"{time.strftime('%H:%M', time.localtime(ts))} {text}"
        for ts, text in messages
    ]

    if not lines:
        return ""

    total_chars = sum(len(line) for line in lines) + len(lines) - 1
    if total_chars <= MAX_CHARS:
        joined = "\n".join(lines)
        # Use literal replacements because the prompt template contains JSON
        # braces that must not be treated as format placeholders.
        template = PROMPT_TEMPLATE
        template = template.replace("{name}", name, 1)
        template = template.replace("{date}", date, 1)
        template = template.replace("{lines}", joined, 1)
        return template

    head = lines[:100]
    omitted_marker_len = len("…（中间省略 0000 条）…")
    head_chars = sum(len(line) for line in head) + len(head) - 1
    # Reserve space for head, ellipsis, and newline separators.
    budget = MAX_CHARS - head_chars - omitted_marker_len - 2

    tail: List[str] = []
    tail_chars = 0
    for line in reversed(lines[100:]):
        add = len(line) + (1 if tail else 0)
        if tail_chars + add > budget:
            break
        tail.append(line)
        tail_chars += add
    tail.reverse()

    omitted = len(lines) - len(head) - len(tail)
    body_lines = list(head)
    if omitted > 0:
        body_lines.append(f"…（中间省略 {omitted} 条）…")
    body_lines.extend(tail)
    joined = "\n".join(body_lines)
    # Use single-pass replacements instead of str.format() because the template
    # contains literal JSON braces that must be preserved verbatim.
    template = PROMPT_TEMPLATE
    template = template.replace("{name}", name, 1)
    template = template.replace("{date}", date, 1)
    template = template.replace("{lines}", joined, 1)
    return template


def _parse_topics(raw: str) -> List[Dict[str, Any]]:
    """Parse and validate the topics JSON from LLM stdout."""
    start = raw.find("{")
    if start == -1:
        raise DigestError("no JSON object in response")
    data, _ = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(data, dict):
        raise DigestError("response JSON is not an object")
    topics = data.get("topics")
    if not isinstance(topics, list):
        raise DigestError("'topics' is not a list")
    out: List[Dict[str, Any]] = []
    for item in topics:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        summary = item.get("summary")
        if not isinstance(title, str) or not isinstance(summary, str):
            continue
        keywords = item.get("keywords")
        if not isinstance(keywords, list):
            keywords = []
        keywords = [str(k) for k in keywords if isinstance(k, (str, int, float))]
        out.append({
            "title": title.strip(),
            "summary": summary.strip(),
            "keywords": keywords,
        })
    return out


def _refresh_worker(
    target: Dict[str, Any],
    cfg: Dict[str, Any],
    digest_dir: Path,
    message_dir: Path,
    runner: Callable[[str], str],
    now: float,
) -> None:
    """Background worker that builds and caches a digest for one target."""
    target_id = str(target.get("username") or "")
    name = str(target.get("name") or target_id or "未命名群")
    date = _today_text(now)
    day_start = _local_day_start(now)
    cache_path = _cache_path(target_id, digest_dir)

    try:
        messages, _failures = _read_today_text_messages(
            target, message_dir, day_start
        )

        if not messages:
            _save_cache(
                cache_path,
                {
                    "version": 1,
                    "target_id": target_id,
                    "date": date,
                    "generated_at": now,
                    "status": "empty",
                    "topics": [],
                    "message_count": 0,
                },
            )
            return

        prompt_text = _format_prompt(name, date, messages)
        if not prompt_text:
            _save_cache(
                cache_path,
                {
                    "version": 1,
                    "target_id": target_id,
                    "date": date,
                    "generated_at": now,
                    "status": "empty",
                    "topics": [],
                    "message_count": len(messages),
                },
            )
            return

        try:
            raw = runner(prompt_text)
            topics = _parse_topics(raw)
            _save_cache(
                cache_path,
                {
                    "version": 1,
                    "target_id": target_id,
                    "date": date,
                    "generated_at": now,
                    "status": "ok",
                    "topics": topics,
                    "message_count": len(messages),
                },
            )
            return
        except Exception as exc:
            # Error: preserve same-day topics if available.
            old = _load_cache(target_id, digest_dir)
            preserved_topics: List[Dict[str, Any]] = []
            if old and old.get("date") == date and isinstance(old.get("topics"), list):
                preserved_topics = old["topics"]
            err = str(exc) or traceback.format_exc()
            if len(err) > 300:
                err = err[-300:]
            _save_cache(
                cache_path,
                {
                    "version": 1,
                    "target_id": target_id,
                    "date": date,
                    "generated_at": now,
                    "status": "error",
                    "topics": preserved_topics,
                    "message_count": len(messages),
                    "error": err,
                },
            )
            return

    except Exception as exc:
        # Worker-level failure (e.g., I/O). Preserve same-day topics if any.
        old = _load_cache(target_id, digest_dir)
        preserved_topics: List[Dict[str, Any]] = []
        if old and old.get("date") == date and isinstance(old.get("topics"), list):
            preserved_topics = old["topics"]
        err = str(exc) or traceback.format_exc()
        if len(err) > 300:
            err = err[-300:]
        try:
            _save_cache(
                cache_path,
                {
                    "version": 1,
                    "target_id": target_id,
                    "date": date,
                    "generated_at": now,
                    "status": "error",
                    "topics": preserved_topics,
                    "message_count": 0,
                    "error": err,
                },
            )
        except Exception:
            pass
    finally:
        with _BUILDING_LOCK:
            _BUILDING.pop(target_id, None)


def _hermes_runner(cfg: Dict[str, Any]) -> Callable[[str], str]:
    """Build a runner that calls the Hermes CLI via agent_provider."""
    import agent_provider

    provider = agent_provider.provider_from_config(cfg)
    if not isinstance(provider, agent_provider.HermesProvider):
        raise DigestError("digest requires hermes provider")

    def _run(prompt_text: str) -> str:
        tmp = NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(prompt_text)
            tmp.close()
            prompt_path = str(tmp.name)
            try:
                args = provider._args(prompt_path=prompt_path)
            except Exception:
                args = None

            if not isinstance(args, list):
                # Fallback: build a minimal CLI invocation.
                args = [
                    str(provider.cli_path),
                    *(
                        ("--profile", str(provider.profile)) if provider.profile else []
                    ),
                    "chat",
                    "-q",
                    "@" + prompt_path,
                    *(
                        ("-m", str(provider.model)) if provider.model else []
                    ),
                    "-Q",
                    "--source",
                    "tool",
                ]
            else:
                # Strip skill and toolsets args; digest does not need KB tools.
                clean: List[str] = []
                skip_next = False
                for i, arg in enumerate(args):
                    if skip_next:
                        skip_next = False
                        continue
                    if arg in ("-s", "--skill", "-t", "--toolsets"):
                        if i + 1 < len(args):
                            skip_next = True
                        continue
                    clean.append(arg)
                args = clean

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT,
                env=provider._build_env(),
            )
            if result.returncode != 0:
                err = (result.stderr or "").strip()
                if len(err) > 300:
                    err = err[-300:]
                raise DigestError(f"hermes failed: {err}")
            return result.stdout or ""
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    return _run


def _should_trigger_rebuild(cache: Optional[Dict[str, Any]], now: float) -> bool:
    """Return True if cache is missing, stale, or for a different day."""
    if cache is None:
        return True
    if cache.get("date") != _today_text(now):
        return True
    generated_at = cache.get("generated_at")
    if not isinstance(generated_at, (int, float)):
        return True
    return (now - float(generated_at)) > DIGEST_TTL_SECONDS


def _is_building(target_id: str) -> bool:
    with _BUILDING_LOCK:
        return target_id in _BUILDING


def _rebuild_cooldown_ok(target_id: str, now: float) -> bool:
    with _BUILDING_LOCK:
        last = _LAST_BUILD_START.get(target_id)
    if last is None:
        return True
    return (now - last) >= MIN_REBUILD_INTERVAL


def _start_build(
    target: Dict[str, Any],
    cfg: Dict[str, Any],
    digest_dir: Path,
    message_dir: Path,
    runner: Callable[[str], str],
    now: float,
) -> None:
    target_id = str(target.get("username") or "")
    with _BUILDING_LOCK:
        if target_id in _BUILDING:
            return
        _BUILDING[target_id] = now
        _LAST_BUILD_START[target_id] = now
    t = threading.Thread(
        target=_refresh_worker,
        args=(target, cfg, digest_dir, message_dir, runner, now),
        daemon=True,
    )
    t.start()


def get_topics_state(
    cfg: Dict[str, Any],
    *,
    digest_dir: Path = DIGEST_DIR,
    message_dir: Optional[Path] = None,
    runner: Optional[Callable[[str], str]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the current digest state for all enabled targets.

    Rebuilds are triggered lazily in the background; this function never blocks
    waiting for the LLM.
    """
    if now is None:
        now = time.time()
    if message_dir is None:
        message_dir = DECRYPTED_MESSAGE_DIR
    if runner is None:
        runner = _hermes_runner(cfg)

    digest_dir = Path(digest_dir)
    digest_dir.mkdir(parents=True, exist_ok=True)

    targets_cfg = cfg.get("targets")
    targets = targets_cfg if isinstance(targets_cfg, list) else []
    enabled_targets = [t for t in targets if isinstance(t, dict) and t.get("enabled")]

    today = _today_text(now)
    out_targets: List[Dict[str, Any]] = []

    for target in enabled_targets:
        target_id = str(target.get("username") or "")
        name = str(target.get("name") or target_id or "未命名群")
        cache = _load_cache(target_id, digest_dir)

        if (
            _should_trigger_rebuild(cache, now)
            and not _is_building(target_id)
            and _rebuild_cooldown_ok(target_id, now)
        ):
            _start_build(target, cfg, digest_dir, message_dir, runner, now)

        building = _is_building(target_id)

        if cache is None:
            entry: Dict[str, Any] = {
                "target_id": target_id,
                "name": name,
                "date": today,
                "generated_at": 0,
                "status": "building" if building else "none",
                "topics": [],
                "message_count": 0,
            }
        else:
            entry = {
                "target_id": target_id,
                "name": name,
                "date": cache.get("date", today),
                "generated_at": cache.get("generated_at", 0),
                "status": cache.get("status", "none"),
                "topics": cache.get("topics", []) if isinstance(cache.get("topics"), list) else [],
                "message_count": cache.get("message_count", 0),
            }
            if cache.get("error"):
                entry["error"] = str(cache["error"])
            if building:
                entry["stale"] = True

        out_targets.append(entry)

    return {"ttl_seconds": DIGEST_TTL_SECONDS, "targets": out_targets}


def refresh_now(
    cfg: Dict[str, Any],
    target_id: Optional[str] = None,
    *,
    digest_dir: Path = DIGEST_DIR,
    message_dir: Optional[Path] = None,
    runner: Optional[Callable[[str], str]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Force a rebuild for a specific target or all enabled targets.

    Still respects single-flight and MIN_REBUILD_INTERVAL. Returns the list of
    targets that were actually started.
    """
    if now is None:
        now = time.time()
    if message_dir is None:
        message_dir = DECRYPTED_MESSAGE_DIR
    if runner is None:
        runner = _hermes_runner(cfg)

    targets_cfg = cfg.get("targets")
    targets = targets_cfg if isinstance(targets_cfg, list) else []
    if target_id is not None:
        selected = [
            t for t in targets
            if isinstance(t, dict) and t.get("username") == target_id and t.get("enabled")
        ]
    else:
        selected = [t for t in targets if isinstance(t, dict) and t.get("enabled")]

    building: List[str] = []
    for target in selected:
        tid = str(target.get("username") or "")
        if not tid:
            continue
        if _is_building(tid):
            building.append(tid)
            continue
        if not _rebuild_cooldown_ok(tid, now):
            # Still under cooldown; report it as building (it will start soon).
            building.append(tid)
            continue
        _start_build(target, cfg, digest_dir, message_dir, runner, now)
        building.append(tid)

    return {"building": building}
