"""A live manual fire must not produce a failed execution on every due tick."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    (home / "cron" / "output").mkdir(parents=True)
    (home / "scripts").mkdir()
    (home / "scripts" / "probe.py").write_text("print('ok')\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    from cron import executions as E
    from cron import jobs as J
    from cron import scheduler as S

    monkeypatch.setattr(J, "HERMES_DIR", home)
    monkeypatch.setattr(J, "CRON_DIR", home / "cron")
    monkeypatch.setattr(J, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(J, "OUTPUT_DIR", home / "cron" / "output")
    monkeypatch.setattr(E, "EXECUTIONS_FILE", home / "cron" / "executions.db")
    monkeypatch.setattr(S, "_hermes_home", home)
    return J, S, E


def test_live_manual_claim_skips_recurring_due_slot_without_false_ledger(cron_env):
    J, S, E = cron_env
    job = J.create_job(
        prompt="probe", schedule="every 10m", no_agent=True, script="probe.py"
    )
    now = datetime.now(timezone.utc)
    due = (now - timedelta(seconds=2)).isoformat()
    J.update_job(
        job["id"],
        {
            "next_run_at": due,
            "fire_claim": {"at": now.isoformat(), "by": "other-host:manual-run"},
        },
    )
    # A manual run is still in flight when the scheduled occurrence becomes due.
    # Ticking repeatedly must leave that slot pending and write no executions.
    for _ in range(3):
        assert S.tick(verbose=False, sync=True) == 0
        assert J.get_job(job["id"])["next_run_at"] == due
        assert E.latest_execution(job["id"]) is None

    # Once the manual run releases its claim, exactly one scheduled fire is allowed.
    J.update_job(job["id"], {"fire_claim": None})
    assert S.tick(verbose=False, sync=True) == 1
    with sqlite3.connect(E.EXECUTIONS_FILE) as db:
        rows = db.execute(
            "SELECT status, error FROM executions WHERE job_id = ?", (job["id"],)
        ).fetchall()
    assert rows == [("completed", None)]


def test_expired_claim_allows_recurring_due_slot_to_fire(cron_env):
    J, S, E = cron_env
    job = J.create_job(
        prompt="probe", schedule="every 10m", no_agent=True, script="probe.py"
    )
    now = datetime.now(timezone.utc)
    J.update_job(
        job["id"],
        {
            "next_run_at": (now - timedelta(seconds=2)).isoformat(),
            "fire_claim": {
                "at": (now - timedelta(seconds=J.FIRE_CLAIM_TTL_SECONDS + 1)).isoformat(),
                "by": "other-host:dead-run",
            },
        },
    )

    assert S.tick(verbose=False, sync=True) == 1
    assert E.latest_execution(job["id"])["status"] == "completed"
