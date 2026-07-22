"""Tests for control_api overview stats endpoints."""

import json
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import control_api
import reliable_pipeline


def _route(method, path, params=None, body=None):
    handler = control_api.ControlHandler.__new__(control_api.ControlHandler)
    body_b, status, _ = handler._route(method, path, params or {}, body or {})
    return status, json.loads(body_b.decode("utf-8"))


def _write_cfg(tmp_path, targets):
    cfg_path = tmp_path / "wechat_bot_targets.json"
    cfg_path.write_text(
        json.dumps({"targets": targets}, ensure_ascii=False),
        encoding="utf-8",
    )
    return cfg_path


def _insert_inbound(con, target_id, received_at):
    con.execute(
        """
        INSERT INTO inbound_events
        (source_event_id, target_id, group_key, sender_id, local_id, received_at, payload_json, status, created_at)
        VALUES (?, ?, 'g', 's', 1, ?, '{}', 'pending', ?)
        """,
        (str(uuid.uuid4()), target_id, received_at, time.time()),
    )


def _insert_turn(con, target_id, created_at):
    con.execute(
        """
        INSERT INTO turns
        (turn_key, target_id, group_key, sender_id, start_event_id, end_event_id, payload_json, status, created_at, closed_at)
        VALUES (?, ?, 'g', 's', 1, 2, '{}', 'closed', ?, ?)
        """,
        (str(uuid.uuid4()), target_id, created_at, created_at),
    )
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_job(con, turn_id, target_id, status, finished_at):
    con.execute(
        """
        INSERT INTO turn_jobs
        (job_key, turn_id, target_id, group_key, payload_json, status, created_at, finished_at)
        VALUES (?, ?, ?, 'g', '{}', ?, ?, ?)
        """,
        (str(uuid.uuid4()), turn_id, target_id, status, finished_at or time.time(), finished_at),
    )
    return con.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_outbox(con, job_id, target_id, status, sent_at=None, dead_at=None):
    con.execute(
        """
        INSERT INTO send_outbox
        (outbox_key, job_id, target_id, group_key, before_local_id, reply_text, status, next_attempt_at, created_at, sent_at, dead_at)
        VALUES (?, ?, ?, 'g', 1, 'hi', ?, ?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), job_id, target_id, status, time.time(), time.time(), sent_at, dead_at),
    )


def test_overview_today_counts_per_target_and_totals(monkeypatch, tmp_path):
    db_path = tmp_path / "pipeline.db"
    con = reliable_pipeline.open_db(db_path)
    cfg_path = _write_cfg(
        tmp_path,
        [
            {"name": "Known群", "username": "known_t", "enabled": True},
            {"name": "Disabled群", "username": "disabled_t", "enabled": False},
        ],
    )

    cfg = json.loads(cfg_path.read_text())
    monkeypatch.setattr(control_api.reg, "load_config", lambda _path=None, cfg=cfg: cfg)
    monkeypatch.setattr(
        control_api.reliable_worker, "_resolve_db_path", lambda _cfg, _db: db_path
    )

    t0 = control_api._local_day_start()

    # configured target with multiple metrics
    _insert_inbound(con, "known_t", t0 + 60)
    _insert_inbound(con, "known_t", t0 + 120)
    turn1 = _insert_turn(con, "known_t", t0 + 60)
    job1 = _insert_job(con, turn1, "known_t", "failed", t0 + 120)
    _insert_outbox(con, job1, "known_t", "sent", sent_at=t0 + 120)
    turn2 = _insert_turn(con, "known_t", t0 + 60)
    _insert_job(con, turn2, "known_t", "escalated", t0 + 120)

    # disabled target appears because it is configured
    _insert_inbound(con, "disabled_t", t0 + 60)

    # unknown target appears because it has DB activity today
    turn3 = _insert_turn(con, "unknown_t", t0 + 60)
    job3 = _insert_job(con, turn3, "unknown_t", "failed", t0 + 120)
    _insert_outbox(con, job3, "unknown_t", "dead_letter", dead_at=t0 + 120)
    _insert_inbound(con, "unknown_t", t0 + 60)

    # cross-midnight boundary: yesterday 23:59 must not count
    _insert_inbound(con, "known_t", t0 - 60)
    turn_y = _insert_turn(con, "known_t", t0 - 60)
    job_y = _insert_job(con, turn_y, "known_t", "sent", t0 - 60)
    _insert_outbox(con, job_y, "known_t", "sent", sent_at=t0 - 60)
    turn_y2 = _insert_turn(con, "known_t", t0 - 60)
    job_y2 = _insert_job(con, turn_y2, "known_t", "failed", t0 - 60)
    _insert_outbox(con, job_y2, "known_t", "dead_letter", dead_at=t0 - 60)

    con.commit()
    con.close()

    status, payload = _route("GET", "/overview/today")
    assert status == 200
    assert payload["ok"] is True
    assert payload["date"] == time.strftime("%Y-%m-%d", time.localtime(t0))
    assert payload["totals"]["received"] == 4
    assert payload["totals"]["replied"] == 1
    assert payload["totals"]["failed"] == 2
    assert payload["totals"]["escalated"] == 1
    assert payload["totals"]["dead"] == 1

    by_id = {t["target_id"]: t for t in payload["targets"]}
    assert by_id["known_t"]["received"] == 2
    assert by_id["known_t"]["replied"] == 1
    assert by_id["known_t"]["failed"] == 1
    assert by_id["known_t"]["escalated"] == 1
    assert by_id["known_t"]["dead"] == 0
    assert by_id["known_t"]["name"] == "Known群"
    assert by_id["known_t"]["enabled"] is True

    assert by_id["disabled_t"]["received"] == 1
    assert by_id["disabled_t"]["enabled"] is False

    assert by_id["unknown_t"]["name"] == "unknown_t"
    assert by_id["unknown_t"]["enabled"] is False
    assert by_id["unknown_t"]["dead"] == 1


def test_overview_history_zero_filled_and_sorted(monkeypatch, tmp_path):
    db_path = tmp_path / "pipeline.db"
    con = reliable_pipeline.open_db(db_path)
    cfg_path = _write_cfg(
        tmp_path,
        [{"name": "群A", "username": "t_a", "enabled": True}],
    )

    cfg = json.loads(cfg_path.read_text())
    monkeypatch.setattr(control_api.reg, "load_config", lambda _path=None, cfg=cfg: cfg)
    monkeypatch.setattr(
        control_api.reliable_worker, "_resolve_db_path", lambda _cfg, _db: db_path
    )

    t0 = control_api._local_day_start() - 6 * 86400  # 7-day window start
    # Day 0 and day 2 have data; day 1 and day 3-5 should be zero-filled.
    _insert_inbound(con, "t_a", t0 + 100)
    _insert_inbound(con, "t_a", t0 + 100)

    day2 = t0 + 2 * 86400
    _insert_inbound(con, "t_a", day2 + 100)
    turn2 = _insert_turn(con, "t_a", day2 + 100)
    job2 = _insert_job(con, turn2, "t_a", "failed", day2 + 100)
    _insert_outbox(con, job2, "t_a", "sent", sent_at=day2 + 100)

    con.commit()
    con.close()

    status, payload = _route("GET", "/overview/history", {"days": ["7"]})
    assert status == 200
    assert payload["ok"] is True
    assert payload["days"] == 7
    assert len(payload["series"]) == 7

    dates = [s["date"] for s in payload["series"]]
    assert dates == sorted(dates)
    assert payload["series"][0]["received"] == 2
    assert payload["series"][0]["replied"] == 0
    assert payload["series"][2]["received"] == 1
    assert payload["series"][2]["replied"] == 1
    assert payload["series"][2]["failed"] == 1
    assert payload["series"][3]["received"] == 0

    by_id = {t["target_id"]: t for t in payload["per_target"]}
    assert by_id["t_a"]["received"] == 3
    assert by_id["t_a"]["replied"] == 1
    assert by_id["t_a"]["failed"] == 1


def test_overview_history_days_fallback_and_clamp(monkeypatch, tmp_path):
    db_path = tmp_path / "pipeline.db"
    reliable_pipeline.open_db(db_path).close()
    cfg_path = _write_cfg(tmp_path, [])

    cfg = json.loads(cfg_path.read_text())
    monkeypatch.setattr(control_api.reg, "load_config", lambda _path=None, cfg=cfg: cfg)
    monkeypatch.setattr(
        control_api.reliable_worker, "_resolve_db_path", lambda _cfg, _db: db_path
    )

    status, payload = _route("GET", "/overview/history", {"days": ["abc"]})
    assert status == 200
    assert payload["days"] == 7

    status, payload = _route("GET", "/overview/history", {"days": ["999"]})
    assert status == 200
    assert payload["days"] == 62


def test_overview_db_unavailable_returns_zeros_and_warning(monkeypatch, tmp_path):
    cfg_path = _write_cfg(
        tmp_path,
        [{"name": "群A", "username": "t_a", "enabled": True}],
    )
    cfg = json.loads(cfg_path.read_text())
    monkeypatch.setattr(control_api.reg, "load_config", lambda _path=None, cfg=cfg: cfg)
    monkeypatch.setattr(
        control_api.reliable_pipeline,
        "open_db",
        lambda _path: (_ for _ in ()).throw(Exception("db boom")),
    )

    status, payload = _route("GET", "/overview/today")
    assert status == 200
    assert payload["ok"] is True
    assert payload["warning"] == "pipeline db unavailable"
    assert payload["totals"] == control_api._overview_zero_totals()
    assert len(payload["targets"]) == 1
    assert payload["targets"][0]["target_id"] == "t_a"
    assert payload["targets"][0]["received"] == 0

    status, payload = _route("GET", "/overview/history", {"days": ["7"]})
    assert status == 200
    assert payload["ok"] is True
    assert payload["warning"] == "pipeline db unavailable"
    assert len(payload["series"]) == 7
    assert payload["per_target"] == []
