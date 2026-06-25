"""Tests for the dedicated-agent-instance binding on agent_jobs.

The contract under test:
    * ``enqueue_job(..., dedicated_agent_instance_id='hermes-a')`` stores the
      binding inside the job's ``payload_json`` under the key
      ``"dedicated_agent_instance_id"``.
    * ``claim_dispatchable(..., instance_id='hermes-a')`` skips any candidate
      row whose payload carries a non-empty ``dedicated_agent_instance_id``
      that does not match the caller's ``instance_id``.
    * Unbound jobs (key missing, ``None``, or empty string) are claimable by
      any caller.

A small helper resets a claimed job back to ``queued`` so we can re-attempt
the claim with a different ``instance_id`` (the public API only moves the
state machine forward).
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_jobs as jobs


def _reset_to_queued(db_path: Path, job_id: int) -> None:
    """Force a previously-claimed job back to ``queued`` for re-claiming."""
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "UPDATE agent_jobs SET status=?, dispatch_owner=NULL, "
            "dispatch_locked_until=NULL WHERE id=?",
            (jobs.STATUS_QUEUED, int(job_id)),
        )
        con.commit()
    finally:
        con.close()


def test_dedicated_instance_only_claimable_by_bound_instance():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        bound = jobs.enqueue_job(
            job_key="dedicated_hermes_a_001",
            group_key="target_A",
            task_type="wechat_reply",
            payload={"prompt": "hello from A"},
            target_name="target_A",
            sender="alice",
            message_local_id=1,
            dedicated_agent_instance_id="hermes-a",
            db_path=db,
        )
        assert bound is not None
        assert bound["id"] is not None

        # The binding lives inside payload_json, not as a top-level column.
        fetched = jobs.get_job(job_id=bound["id"], db_path=db)
        assert fetched is not None
        payload = fetched.get("payload") or {}
        assert payload.get("dedicated_agent_instance_id") == "hermes-a"

        # The bound instance can claim the job.
        claimed = jobs.claim_dispatchable(
            worker_id="worker_hermes_a",
            instance_id="hermes-a",
            db_path=db,
        )
        assert claimed is not None
        assert claimed["id"] == bound["id"]
        assert claimed["status"] == jobs.STATUS_DISPATCHING

        # Reset and try with the wrong instance - it must NOT see the job.
        _reset_to_queued(db, bound["id"])
        miss = jobs.claim_dispatchable(
            worker_id="worker_hermes_b",
            instance_id="hermes-b",
            db_path=db,
        )
        assert miss is None

        # And instance_id=None also misses bound jobs (the spec treats a None
        # caller as "no binding override" which still requires an exact match
        # for bound rows - therefore the bound row is skipped).
        _reset_to_queued(db, bound["id"])
        miss_none = jobs.claim_dispatchable(
            worker_id="worker_generic",
            instance_id=None,
            db_path=db,
        )
        assert miss_none is None

        # Bound instance can still claim after the misses.
        claimed_again = jobs.claim_dispatchable(
            worker_id="worker_hermes_a",
            instance_id="hermes-a",
            db_path=db,
        )
        assert claimed_again is not None
        assert claimed_again["id"] == bound["id"]


def test_unbound_job_claimable_by_any_instance():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Path(tmpdir) / "agent_jobs.sqlite"
        unbound = jobs.enqueue_job(
            job_key="unbound_001",
            group_key="target_B",
            task_type="wechat_reply",
            payload={"prompt": "hello from B"},
            target_name="target_B",
            sender="bob",
            message_local_id=2,
            db_path=db,
        )
        assert unbound is not None

        # Verify the payload truly has no dedicated binding.
        fetched = jobs.get_job(job_id=unbound["id"], db_path=db)
        assert fetched is not None
        payload = fetched.get("payload") or {}
        assert not payload.get("dedicated_agent_instance_id")

        # Any instance - including a named one not equal to a real binding -
        # can claim an unbound job.
        for instance in ("hermes-a", "hermes-b", None):
            _reset_to_queued(db, unbound["id"])
            claimed = jobs.claim_dispatchable(
                worker_id=f"worker_{instance or 'none'}",
                instance_id=instance,
                db_path=db,
            )
            assert claimed is not None, f"unbound job not claimable by {instance!r}"
            assert claimed["id"] == unbound["id"]
