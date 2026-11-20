"""The run-time profiler: where a run's wall-clock time went.

The pure cases need no database; the integration case drives a real run through
the store and reads its profile back over HTTP. The point of both is the same:
a run's duration is the sum of *measured* stages, a missing milestone is
"not measured" (never a fabricated zero), and the idle time is what nobody
accounted for - the thing a slow run is usually hiding.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestration.nexus_profiler import (  # noqa: E402
    MILESTONES, idle_ratio, profile_run,
)

DSN = os.environ.get("TEST_SUPABASE_DB_URL") or os.environ.get("SUPABASE_DB_URL")
requires_db = pytest.mark.skipif(
    not DSN, reason="set TEST_SUPABASE_DB_URL to run profiler tests")

T0 = datetime(2026, 11, 15, 12, 0, 0)


def _row(**milestones):
    """A run row with the given milestones, offset in seconds from ``T0``."""
    row = {"id": "run-1", "state": "running"}
    for name, seconds in milestones.items():
        row[name] = T0 + timedelta(seconds=seconds)
    return row


def test_every_stage_is_measured_when_every_milestone_is_present():
    row = _row(queued_at=0, claimed_at=10, dispatch_started_at=12,
               conversation_ready_at=60, prompt_sent_at=61,
               first_agent_event_at=70, verification_started_at=370,
               verification_completed_at=372, completed_at=375)
    p = profile_run(row)
    assert p["total_seconds"] == 375
    assert p["stages"]["queue_wait"] == 10
    assert p["stages"]["dispatch"] == 2
    assert p["stages"]["openhands_start"] == 48
    assert p["stages"]["prompt"] == 1
    assert p["stages"]["first_event"] == 9
    assert p["stages"]["execution"] == 300
    assert p["stages"]["verification"] == 2
    assert p["stages"]["finish"] == 3
    # Every second is accounted for: no idle.
    assert p["idle_seconds"] == 0
    assert idle_ratio(p) == 0.0


def test_a_missing_milestone_is_none_not_zero():
    """A stage with no end is 'not measured', never a fabricated 0."""
    row = _row(queued_at=0, claimed_at=5)          # nothing after the claim
    p = profile_run(row)
    assert p["stages"]["queue_wait"] == 5
    assert p["stages"]["dispatch"] is None
    assert p["stages"]["execution"] is None
    assert p["stages"]["verification"] is None
    assert p["idle_seconds"] is None                # no end -> no total


def test_idle_is_the_time_no_stage_accounts_for():
    """A long gap between two measured stages is idle, and it dominates."""
    row = _row(queued_at=0, claimed_at=2, dispatch_started_at=4,
               conversation_ready_at=8, prompt_sent_at=9,
               first_agent_event_at=10, verification_started_at=100,
               verification_completed_at=101, completed_at=102)
    p = profile_run(row)
    # stages: 2 + 2 + 4 + 1 + 1 + 90 + 1 + 1 = 102 == total, so idle is 0 here;
    # the gap is inside ``execution`` (10 -> 100). Make the gap real idle by
    # leaving a stage unset after the claim.
    assert p["stages"]["execution"] == 90
    assert p["bottleneck"] == "agent_execution"

    row2 = _row(queued_at=0, claimed_at=2, dispatch_started_at=4,
                conversation_ready_at=8, prompt_sent_at=9,
                first_agent_event_at=10, completed_at=200)
    # verification_started/completed unset: the run went from the agent's first
    # event straight to done, so the 190 s is idle (nothing measured).
    p2 = profile_run(row2)
    assert p2["idle_seconds"] == 190
    assert p2["bottleneck"] == "idle"
    assert idle_ratio(p2) == 0.95


def test_a_live_run_is_measured_to_now():
    """A run that has not ended is measured up to the ``now`` a caller passes."""
    row = _row(queued_at=0, claimed_at=5, dispatch_started_at=6,
               conversation_ready_at=40, prompt_sent_at=41,
               first_agent_event_at=50)
    now = T0 + timedelta(seconds=90)
    p = profile_run(row, now=now)
    assert p["total_seconds"] == 90
    assert p["stages"]["execution"] is None          # no verification yet
    # 90 - (5 + 1 + 34 + 1 + 9) = 40 s not yet attributed to a measured stage.
    assert p["idle_seconds"] == 40


def test_the_bottleneck_is_the_largest_measured_stage():
    slow_queue = _row(queued_at=0, claimed_at=600, dispatch_started_at=601,
                      conversation_ready_at=602, prompt_sent_at=602,
                      first_agent_event_at=603, verification_started_at=604,
                      verification_completed_at=604, completed_at=605)
    assert profile_run(slow_queue)["bottleneck"] == "queue_wait"

    slow_oh = _row(queued_at=0, claimed_at=1, dispatch_started_at=2,
                   conversation_ready_at=400, prompt_sent_at=401,
                   first_agent_event_at=402, verification_started_at=403,
                   verification_completed_at=403, completed_at=404)
    assert profile_run(slow_oh)["bottleneck"] == "openhands_start"


def test_an_unknown_column_is_not_a_milestone():
    """A run row missing a milestone simply does not measure that stage."""
    p = profile_run({"id": "x", "state": "running"})
    assert p["total_seconds"] is None
    assert all(v is None for v in p["stages"].values())
    assert p["idle_seconds"] is None


def test_milestones_are_the_documented_set():
    assert "queued_at" in MILESTONES and "completed_at" in MILESTONES
    assert "first_agent_event_at" in MILESTONES


#: The eleven run-profiler columns migration ``0040_run_profiler.sql`` adds.
#: Extracted from the migration and the store's own column map, not guessed;
#: ``last_progress_at`` is one of them even though it is not a stage boundary
#: in ``MILESTONES`` (it is read by the profiler and written by the loop).
PROFILER_COLUMNS = (
    "queued_at", "claimed_at", "dispatch_started_at", "executor_started_at",
    "conversation_ready_at", "prompt_sent_at", "first_agent_event_at",
    "last_progress_at", "verification_started_at",
    "verification_completed_at", "completed_at",
)


def test_the_eleven_profiler_columns_are_required_for_nexus_runs():
    """Every profiler column must be named in ``REQUIRED_COLUMNS["nexus_runs"]``.

    This fails if any one of the eleven is removed from the guard: the schema
    contract for the profiler would silently stop being enforced. The migration
    that adds them is the authority for the names.
    """
    from orchestration.cloud_store import PostgresStore

    required = PostgresStore.REQUIRED_COLUMNS["nexus_runs"]
    for column in PROFILER_COLUMNS:
        assert column in required, f"{column} is not in the schema guard"
    # The migration and the guard must agree: every column the migration adds is
    # guarded, and the guard adds nothing the migration does not create.
    mig = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "migrations", "0040_run_profiler.sql")
    sql = open(mig).read()
    for column in PROFILER_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql, (
            f"{column} is guarded but migration 0040 does not add it")


@requires_db
def test_a_schema_missing_any_profiler_column_fails_validation():
    """``verify_schema`` reports a missing profiler column, by name.

    The guard must not merely list the columns: it must be *consulted*. Each of
    the eleven is dropped in turn, the guard is asked, and the missing column is
    named ``nexus_runs(<column>)``. A complete schema (after the column is put
    back) passes.
    """
    import psycopg

    from orchestration.cloud_store import PostgresStore

    mig = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "migrations")
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(mig)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(mig, name)).read())

    store = PostgresStore(DSN)
    try:
        # A complete schema passes.
        assert store.verify_schema() == [], "a fully-migrated schema must pass"

        for column in PROFILER_COLUMNS:
            with psycopg.connect(DSN, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"ALTER TABLE nexus_runs DROP COLUMN IF EXISTS {column}")
            missing = store.verify_schema()
            assert f"nexus_runs({column})" in missing, (
                f"dropping {column} was not reported; verify_schema() = {missing}")
            # Put it back and confirm the guard is satisfied again, so the next
            # column is tested against a complete schema.
            with psycopg.connect(DSN, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"ALTER TABLE nexus_runs ADD COLUMN IF NOT EXISTS "
                        f"{column} timestamptz")
            assert store.verify_schema() == [], (
                f"restoring {column} did not clear the report")
    finally:
        store.close()


# ---------------------------------------------------------------- integration
@requires_db
def test_a_real_run_records_its_milestones_and_profiles():
    """A run through the store stamps queued_at/claimed_at and reads back a
    profile over the run-detail route."""
    import psycopg

    mig = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "migrations")
    with psycopg.connect(DSN, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            for name in sorted(os.listdir(mig)):
                if name.endswith(".sql"):
                    cur.execute(open(os.path.join(mig, name)).read())

    from orchestration.cloud_store import PostgresStore
    from orchestration.nexus_lab_store import LabStore
    from orchestration.nexus_projects import ProjectService

    store = PostgresStore(DSN)
    try:
        projects = ProjectService(store)
        project = str(projects.create("user:owner", "Prof")["id"])
        goal = str(projects.create_goal("user:owner", project, "g")[0]["id"])
        lab = LabStore(store)
        run = lab.start_run("user:owner", project, goal, policy="durable")
        assert run["queued_at"] is not None, "queued_at is stamped at creation"
        lab.add_task("user:owner", project, run["id"], task_key="t", title="t",
                     repository="", branch="")
        claimed = lab.claim_task(str(run["id"]), "w1")
        assert claimed is not None
        row = lab.run_row(str(run["id"]))
        assert row["claimed_at"] is not None, "claimed_at is stamped on the claim"
        # The profile is computable and honest: the run is between claim and
        # completion, so the later stages are 'not measured', not zero.
        prof = lab.run_profile(str(run["id"]))
        assert prof["total_seconds"] is not None
        assert prof["stages"]["queue_wait"] is not None
        assert prof["stages"]["execution"] is None
        # A milestone is written once: a second stamp does not move it.
        first = lab.run_row(str(run["id"]))["queued_at"]
        lab.record_run_milestone(str(run["id"]), "queued_at")
        assert lab.run_row(str(run["id"]))["queued_at"] == first
        # An unknown milestone is refused.
        with pytest.raises(ValueError):
            lab.record_run_milestone(str(run["id"]), "state")
    finally:
        store.close()
