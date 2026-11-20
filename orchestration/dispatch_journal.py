"""The lab's durable send gate. No network I/O inside these transactions.

A lease authorizes a *mutation*, not a conclusion about what a provider did.
A worker may leave a receipt on its original intent after revocation; it may
never use that receipt to bind/supersede a subsequent grant.
"""
from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from .cloud_store import PostgresStore

# Prepared never sent anything. The four other open states hold the branch
# even if an operator cancelled/requeued the task before a response arrived.
IN_DOUBT = "('sending','uncertain','accepted','identified')"
OPEN = "('prepared','sending','uncertain','accepted','identified')"


class DispatchHeld(Exception):
    """No permission to POST (or no proof of the outcome). Leave it to recovery."""


class DispatchJournal:
    def __init__(self, store: PostgresStore):
        self.store = store

    @staticmethod
    def prepare(cur: Any, task: dict) -> str:
        """Called inside the claim transaction (run, budget and task already locked)."""
        cur.execute("INSERT INTO nexus_dispatch_intents(task_id,run_id,lease_token,repository,branch) "
                    "VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (task['id'], task['run_id'], task['lease_token'], task['repository'], task['branch']))
        return str(cur.fetchone()['id'])

    def arm(self, task: dict, context: dict) -> None:
        """Commit the one send permit. Never put a sender back in prepared."""
        with self.store._tx() as cur:
            cur.execute("SELECT id FROM nexus_runs WHERE id=%s AND state='running' FOR UPDATE",
                        (task['run_id'],))
            if not cur.fetchone():
                raise DispatchHeld('run stopped before send')
            cur.execute("SELECT id FROM nexus_run_tasks WHERE id=%s AND lease_token=%s AND "
                        "state='dispatched' AND lease_expires>clock_timestamp() FOR UPDATE",
                        (task['id'], task['lease_token']))
            if not cur.fetchone():
                raise DispatchHeld('claim lost before send')
            cur.execute("UPDATE nexus_dispatch_intents SET state='sending',context=%s,updated_at=now() "
                        "WHERE id=%s AND lease_token=%s AND state='prepared'",
                        (Jsonb(context), task['_intent_id'], task['lease_token']))
            if not cur.rowcount:
                raise DispatchHeld('send permit already used')

    def uncertain(self, intent_id: str) -> None:
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_dispatch_intents SET state='uncertain',updated_at=now() "
                        "WHERE id=%s AND state IN ('sending','accepted')", (intent_id,))

    def receipt(self, task: dict, *, start_id: str | None = None, conversation: str | None = None,
                delivery: dict | None = None) -> None:
        """Monotone provider facts (including from a late sender), not a grant."""
        if not start_id and not conversation:
            raise DispatchHeld('provider returned no receipt')
        with self.store._tx() as cur:
            # Keep the legacy pending_start view and journal in one commit.
            cur.execute('SELECT lease_token FROM nexus_run_tasks WHERE id=%s FOR UPDATE', (task['id'],))
            cur.execute("UPDATE nexus_dispatch_intents SET state=%s, start_id=COALESCE(start_id,%s), "
                        "conversation_id=COALESCE(conversation_id,%s), context=context || %s, updated_at=now() "
                        "WHERE id=%s AND state IN ('sending','uncertain','accepted') "
                        "AND (start_id IS NULL OR %s::text IS NULL OR start_id=%s) RETURNING *",
                        ('identified' if conversation else 'accepted', start_id, conversation,
                         Jsonb({'delivery': delivery} if delivery else {}), task['_intent_id'], start_id, start_id))
            intent = cur.fetchone()
            if intent and start_id and not conversation:
                ctx = intent['context']
                cur.execute("UPDATE nexus_run_tasks SET pending_start=%s WHERE id=%s AND "
                            "lease_token=%s AND state='dispatched'", (Jsonb({
                                **{k: ctx.get(k) for k in ('account_id', 'kind', 'base_sha', 'delivery',
                                                          'previous_account')}, 'start_task_id': start_id,
                                'lease_token': intent['lease_token']}), task['id'], intent['lease_token']))

    def rejected(self, task: dict) -> bool:
        """Only for proven nonacceptance/terminal start ERROR. One reducer wins.

        A crash after this commit is safe: ordinary lease reclaim can now run.
        """
        with self.store._tx() as cur:
            cur.execute("SELECT lease_token,state FROM nexus_run_tasks WHERE id=%s FOR UPDATE", (task['id'],))
            row = cur.fetchone()
            cur.execute("UPDATE nexus_dispatch_intents SET state='rejected',updated_at=now() "
                        "WHERE id=%s AND lease_token=%s AND state IN ('prepared','sending','accepted','uncertain') "
                        "RETURNING lease_token", (task['_intent_id'], task['lease_token']))
            intent = cur.fetchone()
            if not intent or row['state'] != 'dispatched' or row['lease_token'] != intent['lease_token']:
                return False
            cur.execute('UPDATE nexus_run_tasks SET pending_start=NULL WHERE id=%s', (task['id'],))
            return row['lease_token'] == task['lease_token']

    def recoverable(self, run_id: str) -> list[dict]:
        with self.store._tx() as cur:
            cur.execute(f"SELECT i.*, t.lease_expires<=now() AS expired, "
                        "i.updated_at<now()-interval '5 seconds' AS probe_due FROM nexus_dispatch_intents i "
                        f"JOIN nexus_run_tasks t ON t.id=i.task_id WHERE i.run_id=%s AND (i.state IN {IN_DOUBT} "
                        "OR (i.state='prepared' AND t.state='dispatched' AND t.lease_token=i.lease_token "
                        "AND t.lease_expires<=now())) ORDER BY i.created_at", (run_id,))
            return list(cur.fetchall())

    def take_over(self, intent: dict, worker: str, seconds: int) -> bool:
        """Transfer an expired grant, not the right to repeat an armed send."""
        with self.store._tx() as cur:
            cur.execute("UPDATE nexus_run_tasks SET lease_owner=%s,lease_token=lease_token+1, "
                        "lease_expires=now()+%s*interval '1 second', pending_start=CASE WHEN pending_start "
                        "IS NULL THEN NULL ELSE pending_start || jsonb_build_object('lease_token',lease_token+1) END "
                        "WHERE id=%s AND state='dispatched' AND lease_token=%s AND lease_expires<=now() "
                        "RETURNING lease_token", (worker, seconds, intent['task_id'], intent['lease_token']))
            row = cur.fetchone()
            if not row:
                return False
            cur.execute(f"UPDATE nexus_dispatch_intents SET lease_token=%s,updated_at=now() "
                        f"WHERE id=%s AND lease_token=%s AND state IN {OPEN}",
                        (row['lease_token'], intent['id'], intent['lease_token']))
            if not cur.rowcount:
                raise DispatchHeld('intent already settled')  # roll back the task rotation too
            return True

    @staticmethod
    def consume(cur: Any, task: dict, intent_id: str, token: int, conversation: str) -> bool:
        """Task row already locked. Bind + history, or a stop debt, atomically."""
        cur.execute("SELECT * FROM nexus_dispatch_intents WHERE id=%s AND task_id=%s "
                    "AND state='identified' AND conversation_id=%s FOR UPDATE", (intent_id, task['id'], conversation))
        intent = cur.fetchone()
        if not intent:
            return False
        if task['state'] != 'dispatched' or task['lease_token'] != intent['lease_token']:
            cur.execute("INSERT INTO nexus_executor_stops(run_id,task_id,project_id,conversation_id, "
                        "openhands_account_id,repository,branch,reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (conversation_id) DO NOTHING", (task['run_id'], task['id'], task['project_id'],
                        conversation, intent['context'].get('account_id'), intent['repository'], intent['branch'],
                        'provider receipt for a revoked dispatch intent'))
            state = 'stop_owed'
        elif token != intent['lease_token'] or not task['_live']:
            return False  # a replacement will consume the same receipt
        else:
            state = 'bound'
        cur.execute('UPDATE nexus_dispatch_intents SET state=%s,updated_at=now() WHERE id=%s', (state, intent_id))
        return state == 'bound'
