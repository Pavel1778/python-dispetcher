"""The run's final report, and its delivery to the chat that asked for it.

A person who starts an autonomous run from a chat should not have to open the
«Агент» tab, hunt for the result and copy it: when the run is *really* done the
report is posted back into that chat. This module builds the report from durable
rows and delivers it, with the two properties the requirement names:

* **One run, one report.** ``nexus_run_reports.run_id`` is UNIQUE, so a second
  finish (a retried pass, two workers finishing at once) cannot write a second
  report. The row is written in the transaction that settles the run.
* **Delivery is durable.** A report left ``pending`` after a restart is picked up
  by the next pass; a delivery that fails is retried with a backoff; a report is
  never sent twice because the status only moves forward from ``pending``.

A report is only built for a **successful** finish. A blocked or failed run
writes no "report": its outcome is the notification the run already posts, and
the chat is told by the scheduler's own message when the run blocks. Reporting
success for a run that did not verify would be the one thing the requirement
forbids.
"""
from __future__ import annotations

import logging
from typing import Any

from .cloud_store import PostgresStore
from .nexus_lab_store import LabStore

log = logging.getLogger("nexus_reports")

#: How long to wait before retrying a failed delivery, and the ceiling.
DELIVERY_RETRY_SECONDS = 30.0
DELIVERY_RETRY_MAX_SECONDS = 1800.0
#: Give up (leave the report ``failed``) after this many delivery attempts. The
#: report itself is kept; only its delivery stops, so the content is never lost.
MAX_DELIVERY_ATTEMPTS = 12


class ReportBuilder:
    """Build a run's report from its durable rows. Never fabricates a result."""

    def __init__(self, store: PostgresStore, lab: LabStore):
        self.store = store
        self.lab = lab

    def build(self, run_id: str) -> tuple[str, dict[str, Any]]:
        """The report text and its structured meta, from the run's own rows.

        Every claim in the text is read from the database: the original task,
        the tasks and their verifier verdicts, the commits the verifier observed
        on each repository's branch, and the tests the agent reported. A task
        that did not verify is named as such - the report says what is true, not
        what the agent hoped.
        """
        run = self.lab.run_row(run_id)
        if run is None:
            raise ValueError("run not found")
        with self.store._tx() as cur:
            cur.execute("SELECT text FROM nexus_goals WHERE id=%s", (run["goal_id"],))
            goal = cur.fetchone()
            cur.execute(
                "SELECT task_key, title, state, repository, branch, attempt, cycle, "
                "  verification, evidence, last_error "
                "FROM nexus_run_tasks WHERE run_id=%s ORDER BY created_at, id", (run_id,))
            tasks = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT kind, title, body, created_at FROM nexus_notifications "
                        "WHERE resource_id=%s ORDER BY created_at", (run_id,))
            events = [dict(r) for r in cur.fetchall()]

        goal_text = (goal or {}).get("text") or ""
        verified = [t for t in tasks if t["state"] == "completed_verified"]
        unverified = [t for t in tasks if t["state"] != "completed_verified"]
        repositories: dict[str, dict[str, Any]] = {}
        for task in tasks:
            repo = task.get("repository") or ""
            if not repo:
                continue
            entry = repositories.setdefault(repo, {
                "repository": repo, "branch": task.get("branch") or "",
                "commits": [], "tasks": []})
            entry["tasks"].append(task["task_key"])
            for item in (task.get("evidence") or []):
                if isinstance(item, dict) and item.get("remote_sha"):
                    sha = str(item["remote_sha"])
                    if sha not in entry["commits"]:
                        entry["commits"].append(sha)

        lines: list[str] = []
        lines.append(f"# Отчёт по автономному прогону {str(run['id'])[:8]}")
        lines.append("")
        lines.append("## Исходное задание")
        lines.append(goal_text[:4000] if goal_text else "(задание не сохранено)")
        lines.append("")
        lines.append("## Итог")
        lines.append(f"- Статус: **{run['state']}**")
        lines.append(f"- Задач всего: {len(tasks)}; подтверждено: {len(verified)}; "
                     f"не подтверждено: {len(unverified)}")
        lines.append(f"- Циклов выполнения: {run['retry_attempt'] + 1} "
                     f"(запусков задач: {run['total_iterations']})")
        if run.get("last_error"):
            lines.append(f"- Последняя ошибка: {run['last_error'][:500]}")
        lines.append("")
        lines.append("## Что сделано (по задачам)")
        if not tasks:
            lines.append("(в плане не было задач)")
        for task in tasks:
            mark = {"completed_verified": "[x]", "completed_unverified": "[~]",
                    "partial": "[~]", "failed": "[ ]", "blocked": "[ ]"}.get(task["state"], "[?]")
            lines.append(f"- {mark} **{task['title']}** (`{task['task_key']}`) — "
                         f"{task['state']}, попытка {task['attempt']}, цикл {task['cycle']}")
            verification = task.get("verification") or {}
            if verification.get("reason"):
                lines.append(f"    - проверка: {verification['reason']}")
            if task.get("last_error"):
                lines.append(f"    - ошибка: {task['last_error'][:300]}")
        lines.append("")
        lines.append("## Изменённые репозитории и коммиты")
        if not repositories:
            lines.append("(задачи не были привязаны к репозиторию)")
        for entry in repositories.values():
            lines.append(f"- `{entry['repository']}` (ветка `{entry['branch'] or 'по умолчанию'}`)")
            lines.append(f"    - задачи: {', '.join(entry['tasks'])}")
            if entry["commits"]:
                for sha in entry["commits"]:
                    lines.append(f"    - commit: `{sha}`")
            else:
                lines.append("    - commit: не подтверждён проверкой")
        lines.append("")
        lines.append("## Проверка")
        lines.append("- Проверка независимая: ветка и коммит читаются на GitHub, "
                     "а не из ответа агента.")
        if unverified:
            lines.append("- **Не подтверждено:** " + ", ".join(
                f"{t['task_key']} ({t['state']})" for t in unverified))
        else:
            lines.append("- Все задачи подтверждены.")
        if events:
            lines.append("")
            lines.append("## События прогона")
            for event in events[-20:]:
                lines.append(f"- {event['kind']}: {event['title']}")
        text = "\n".join(lines)
        meta = {
            "run_id": str(run["id"]),
            "state": run["state"],
            "task_count": len(tasks),
            "verified": len(verified),
            "unverified": len(unverified),
            "repositories": [{"repository": e["repository"], "branch": e["branch"],
                              "commits": e["commits"]} for e in repositories.values()],
            "cycles": run["retry_attempt"] + 1,
            "total_iterations": run["total_iterations"],
        }
        return text, meta


class ReportDeliverer:
    """Deliver pending run reports into their chat, exactly once each."""

    def __init__(self, store: PostgresStore, lab: LabStore, conversations: Any):
        self.store = store
        self.lab = lab
        self.conversations = conversations

    def deliver_pending(self, *, limit: int = 50) -> dict[str, int]:
        """Try to deliver every report that is still pending. Returns a tally.

        A report with no conversation (its chat was deleted) is marked sent: it
        can never be delivered, and leaving it pending would make every pass try
        again for ever.
        """
        delivered = failed = 0
        for report in self.lab.pending_reports(limit=limit):
            conversation_id = report.get("conversation_id")
            if not conversation_id:
                self.lab.mark_report_sent(report["id"])
                continue
            try:
                self.conversations.post_internal(
                    conversation_id,
                    [{"role": "assistant", "content": report["content"],
                      "meta": {**report["meta"], "kind": "run_report",
                               "run_id": report["run_id"]}}])
            except Exception as e:  # noqa: BLE001 - a delivery failure is retried
                attempts = int(report.get("attempts") or 0) + 1
                backoff = min(DELIVERY_RETRY_MAX_SECONDS,
                              DELIVERY_RETRY_SECONDS * (2 ** min(attempts - 1, 20)))
                give_up = attempts >= MAX_DELIVERY_ATTEMPTS
                self.lab.mark_report_failed(
                    report["id"], error=f"{type(e).__name__}: {e}",
                    retry_in=backoff, give_up=give_up)
                if give_up:
                    log.warning("report %s: giving up after %s attempts",
                                report["id"], attempts)
                failed += 1
                continue
            self.lab.mark_report_sent(report["id"])
            delivered += 1
        return {"delivered": delivered, "failed": failed}
