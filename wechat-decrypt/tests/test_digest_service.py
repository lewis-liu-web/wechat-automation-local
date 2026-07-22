"""Tests for digest_service lazy topic digest builds."""

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
import zstandard as zstd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import digest_service


@pytest.fixture(autouse=True)
def _reset_building_state(monkeypatch):
    """Drain in-flight builds and reset module-level flight state."""
    for _ in range(200):
        with digest_service._BUILDING_LOCK:
            if not digest_service._BUILDING:
                break
        time.sleep(0.01)
    monkeypatch.setattr(digest_service, "_BUILDING", {})
    monkeypatch.setattr(digest_service, "_LAST_BUILD_START", {})


def _make_message_db(db_path: Path, table: str, rows):
    """Create a WeChat message DB with the given rows.

    Columns: local_id, local_type, create_time, real_sender_id, status,
    message_content, WCDB_CT_message_content.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            f"""CREATE TABLE [{table}] (
                local_id INTEGER PRIMARY KEY,
                local_type INT,
                create_time INT,
                real_sender_id INT,
                status INT,
                message_content BLOB,
                WCDB_CT_message_content INT
            )"""
        )
        conn.executemany(
            f"""INSERT INTO [{table}]
                (local_id, local_type, create_time, real_sender_id, status,
                 message_content, WCDB_CT_message_content)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _target():
    return {
        "username": "u1",
        "name": "测试群",
        "db": "message_0.db",
        "table": "Msg_x",
        "enabled": True,
    }


def _cfg(targets):
    return {"targets": targets}


def _wait_build(target_id: str):
    # Generous window: the full suite leaks many background threads and Windows
    # AV can stall temp-file I/O; the poll interval keeps the happy path fast.
    for _ in range(6000):
        if not digest_service._is_building(target_id):
            return
        time.sleep(0.01)
    raise RuntimeError(
        f"build for {target_id} did not finish; "
        f"threads={threading.active_count()} "
        f"names={[t.name for t in threading.enumerate()][:20]}"
    )


def _latest_cache(target_id: str, digest_dir: Path):
    path = digest_service._cache_path(target_id, digest_dir)
    return digest_service._load_cache(target_id, digest_dir) if path.exists() else None


def test_ok_path_builds_and_caches_topics(monkeypatch, tmp_path):
    """3 valid text messages (incl. zstd) are summarized; self/image/yesterday excluded."""
    digest_dir = tmp_path / "digests"
    message_dir = tmp_path / "messages"
    db_path = message_dir / "message_0.db"

    now = time.time()
    day_start = digest_service._local_day_start(now)

    zstd_text = "这是 zstd 压缩的消息"
    zstd_blob = zstd.compress(zstd_text.encode("utf-8"))

    rows = [
        # local_id, local_type, create_time, real_sender_id, status, content, ct
        (1, 1, day_start + 10, 100, 0, "今日第一条", 0),
        (2, 1, day_start + 20, 101, 2, "托管号自己发的要排除", 0),  # self, excluded
        (3, 1, day_start + 30, 102, 0, zstd_blob, 4),  # compressed text, included
        (4, 1, day_start + 35, 105, 0, "今日第二条", 0),
        (5, 3, day_start + 40, 103, 0, b"\x89PNG", 0),  # image, excluded
        (6, 1, day_start - 3600, 104, 0, "昨天的消息", 0),  # yesterday, excluded
    ]
    _make_message_db(db_path, "Msg_x", rows)

    expected_topics = [
        {"title": "话题A", "summary": "摘要A", "keywords": ["k1", "k2"]},
    ]
    calls = []

    def fake_runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"topics": expected_topics}, ensure_ascii=False)

    cfg = _cfg([_target()])
    state = digest_service.get_topics_state(
        cfg,
        digest_dir=digest_dir,
        message_dir=message_dir,
        runner=fake_runner,
        now=now,
    )
    assert state["targets"][0]["status"] == "building"

    _wait_build("u1")

    cache = _latest_cache("u1", digest_dir)
    assert cache is not None
    assert cache["status"] == "ok"
    assert cache["message_count"] == 3
    assert cache["topics"] == expected_topics
    assert cache["date"] == digest_service._today_text(now)

    # zstd message should appear decoded in the prompt.
    assert len(calls) == 1
    assert zstd_text in calls[0]
    assert "托管号自己发的要排除" not in calls[0]
    assert "昨天的消息" not in calls[0]
    assert "PNG" not in calls[0]


def test_empty_day_writes_empty_and_skips_runner(monkeypatch, tmp_path):
    """No messages today -> status empty and runner is never invoked."""
    digest_dir = tmp_path / "digests"
    message_dir = tmp_path / "messages"
    db_path = message_dir / "message_0.db"

    now = time.time()
    day_start = digest_service._local_day_start(now)
    _make_message_db(db_path, "Msg_x", [])

    calls = []

    def fake_runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"topics": []}, ensure_ascii=False)

    cfg = _cfg([_target()])
    digest_service.get_topics_state(
        cfg,
        digest_dir=digest_dir,
        message_dir=message_dir,
        runner=fake_runner,
        now=now,
    )
    _wait_build("u1")

    cache = _latest_cache("u1", digest_dir)
    assert cache is not None
    assert cache["status"] == "empty"
    assert cache["message_count"] == 0
    assert cache["topics"] == []
    assert calls == []


def test_runner_error_preserves_same_day_topics(monkeypatch, tmp_path):
    """Runner failure keeps the previous same-day topics in the cache."""
    digest_dir = tmp_path / "digests"
    message_dir = tmp_path / "messages"
    db_path = message_dir / "message_0.db"

    now = time.time()
    day_start = digest_service._local_day_start(now)
    date = digest_service._today_text(now)

    _make_message_db(
        db_path,
        "Msg_x",
        [(1, 1, day_start + 10, 100, 0, "今日内容", 0)],
    )

    old_topics = [{"title": "老话题", "summary": "老摘要", "keywords": ["k"]}]
    digest_service._save_cache(
        digest_service._cache_path("u1", digest_dir),
        {
            "version": 1,
            "target_id": "u1",
            "date": date,
            "generated_at": now - digest_service.DIGEST_TTL_SECONDS - 1,
            "status": "ok",
            "topics": old_topics,
            "message_count": 1,
        },
    )

    def bad_runner(prompt: str) -> str:
        raise RuntimeError("LLM exploded")

    cfg = _cfg([_target()])
    digest_service.get_topics_state(
        cfg,
        digest_dir=digest_dir,
        message_dir=message_dir,
        runner=bad_runner,
        now=now,
    )
    _wait_build("u1")

    cache = _latest_cache("u1", digest_dir)
    assert cache is not None
    assert cache["status"] == "error"
    assert cache["topics"] == old_topics
    assert cache["message_count"] == 1
    assert "LLM exploded" in cache["error"]


def test_ttl_does_not_trigger_rebuild(monkeypatch, tmp_path):
    """Fresh cache within TTL is returned as-is; runner is not called."""
    digest_dir = tmp_path / "digests"
    message_dir = tmp_path / "messages"

    now = time.time()
    date = digest_service._today_text(now)

    calls = []

    def fake_runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"topics": []}, ensure_ascii=False)

    digest_service._save_cache(
        digest_service._cache_path("u1", digest_dir),
        {
            "version": 1,
            "target_id": "u1",
            "date": date,
            "generated_at": now - 100,
            "status": "ok",
            "topics": [{"title": "现有话题", "summary": "摘要", "keywords": []}],
            "message_count": 5,
        },
    )

    cfg = _cfg([_target()])
    state = digest_service.get_topics_state(
        cfg,
        digest_dir=digest_dir,
        message_dir=message_dir,
        runner=fake_runner,
        now=now,
    )

    assert state["targets"][0]["status"] == "ok"
    assert state["targets"][0]["message_count"] == 5
    assert calls == []
    assert not digest_service._is_building("u1")


def test_legacy_db_without_status_column_falls_back(monkeypatch, tmp_path):
    """Missing status column does not crash; query falls back and returns messages."""
    digest_dir = tmp_path / "digests"
    message_dir = tmp_path / "messages"
    db_path = message_dir / "message_0.db"

    now = time.time()
    day_start = digest_service._local_day_start(now)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """CREATE TABLE [Msg_x] (
                local_id INTEGER PRIMARY KEY,
                local_type INT,
                create_time INT,
                real_sender_id INT,
                message_content BLOB,
                WCDB_CT_message_content INT
            )"""
        )
        conn.execute(
            "INSERT INTO [Msg_x] VALUES (?, ?, ?, ?, ?, ?)",
            (1, 1, day_start + 10, 100, "老库文本", 0),
        )
        conn.commit()
    finally:
        conn.close()

    calls = []

    def fake_runner(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"topics": []}, ensure_ascii=False)

    cfg = _cfg([_target()])
    digest_service.get_topics_state(
        cfg,
        digest_dir=digest_dir,
        message_dir=message_dir,
        runner=fake_runner,
        now=now,
    )
    _wait_build("u1")

    cache = _latest_cache("u1", digest_dir)
    assert cache is not None
    assert cache["status"] == "ok"
    assert cache["message_count"] == 1
    assert "老库文本" in calls[0]
