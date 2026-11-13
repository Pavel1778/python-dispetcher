"""The final report of an autonomous run, and its delivery to the chat.

A run that completes owes the person who asked for it an account of what was
done. This module writes that account and puts it in the conversation the run
was started from - without the person having to go and look for it.

Three properties are enforced here, each by construction rather than by care:

* **Only a verified run reports success.** The report row is written by the
  transaction that moves the run to ``completed`` (``ReportService.on_completed``
  is the ``also`` hook of ``LabStore.set_run_state_internal``), and a run gets
  there only when every task verified and the repositories were confirmed once
  more. There is no other writer, so a report cannot exist for a run that was
  cancelled, failed or is still at work.
* **Exactly one report per run.** ``nexus_run_reports.run_id`` is the primary
  key. Delivery appends the chat message and marks the row ``delivered`` in one
  transaction, on a row locked ``FOR UPDATE SKIP LOCKED``: two workers, a retry
  after an error, or a restart between "written" and "delivered" all end with
  one message.
* **Nothing is claimed that was not observed.** The report separates what
  NEXUS checked itself (the commits are in the branches on GitHub; what GitHub
  lists as changed; what the repository's own checks say) from what the agents
  reported (their summaries, the tests they say they ran), and says which is
  which. Its headline says "done" only when NEXUS saw new commits: a run whose
  tasks were all accepted while no branch moved - an agent that could not push
  names the head it found, and that is accepted - is reported as "no changes
  in the repository; the result is not confirmed" (``build_report``).

The text is Markdown, in Russian like the rest of the chat. ``summary`` holds
the same facts as data.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .cloud_store import NotFound, PostgresStore
from .nexus_conversations import ConversationService
from .nexus_projects import ProjectService

log = logging.getLogger("nexus_reports")

#: How much of the original task the report quotes. A longer task is quoted by
#: its beginning and named by its size and SHA-256; the whole text stays in the
#: specification the run was planned from.
TASK_QUOTE_CHARS = 4_000
#: How much of an agent's own summary is shown per task.
AGENT_SUMMARY_CHARS = 900
#: The most commits and files listed per repository; the counts are always exact.
LISTED_COMMITS = 30
LISTED_FILES = 60
#: The report is a chat message; it stays well inside what one may hold.
MAX_REPORT_CHARS = 60_000
#: Seconds before a failed delivery is tried again: doubles up to ten minutes.
DELIVERY_RETRY_SECONDS = 5.0
DELIVERY_RETRY_MAX_SECONDS = 600.0


def _short(sha: Any) -> str:
    return str(sha or "")[:10]


def _clip(text: Any, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _plural(n: int, one: str, few: str, many: str) -> str:
    mod10, mod100 = n % 10, n % 100
    if mod10 == 1 and mod100 != 11:
        return one
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return few
    return many


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines()) or "> (пусто)"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин"
    return f"{seconds} с"


def _agent_summary(task: dict) -> str:
    """What the agent of the accepted attempt said, as it said it."""
    for item in reversed(task.get("evidence") or []):
        if isinstance(item, dict) and isinstance(item.get("final_text"), str) \
                and item["final_text"].strip():
            return item["final_text"].strip()
    return ""


def _accepted(task: dict) -> dict:
    """The evidence record the task was accepted on (the last one that names
    the branch head), or an empty one."""
    for item in reversed(task.get("evidence") or []):
        if isinstance(item, dict) and item.get("remote_sha"):
            return item
    return {}


def build_report(*, run: dict, task_text: str, task_meta: dict | None,
                 tasks: list[dict], executions: list[dict],
                 confirmation: dict | None, stops: list[dict] | None = None,
                 handoffs: int = 0) -> tuple[str, dict, str]:
    """The report of a completed run: ``(markdown, data, outcome)``.

    Pure: everything it says comes from its arguments, which are the run's own
    rows and what GitHub answered at the final confirmation. *outcome* is

    * ``success`` - NEXUS saw new commits in the repositories and nothing
      stands beside that;
    * ``success_with_notes`` - the same, with something a reader must know (a
      repository's own checks failed or have not finished, a task was accepted
      without the branch moving, a stop signal could not be confirmed);
    * ``no_changes`` - the run completed and **no repository has a new
      commit**: the head of each branch is the commit the work started from.
      Every task was accepted, because the rule of acceptance - the agent named
      the head of the branch - holds for an agent that pushed nothing and named
      the head it found. That is not a result NEXUS can vouch for, so the
      report does not call it done: an agent whose push was refused ends
      exactly like this;
    * ``unconfirmed`` - the same doubt for another reason: the commit the work
      started from is not known (the run was started before it was recorded, or
      GitHub did not answer then), so whether anything changed cannot be said.
    """
    repositories = list((confirmation or {}).get("repositories") or [])
    by_task: dict[str, list[dict]] = {}
    for execution in executions:
        by_task.setdefault(str(execution["task_id"]), []).append(execution)

    notes: list[str] = []
    limits: list[str] = []
    starts = len(executions)
    retried = [t for t in tasks if len(by_task.get(str(t["id"]), [])) > 1]
    cycles = max((int(t.get("cycle") or 0) for t in tasks), default=0)
    if retried:
        limits.append(
            f"{len(retried)} из {len(tasks)} {_plural(len(tasks), 'задачи', 'задач', 'задач')} "
            f"потребовали повторных запусков (всего запусков: {starts}"
            + (f", рабочих циклов до {cycles + 1}" if cycles else "") + ").")
    if handoffs:
        limits.append(f"Исполнитель менялся {handoffs} "
                      f"{_plural(handoffs, 'раз', 'раза', 'раз')} (переключение на другой "
                      "OpenHands с передачей контекста).")
    unconfirmed_stops = [s for s in stops or [] if s.get("state") == "abandoned"]
    if unconfirmed_stops:
        notes.append("остановка не подтверждена")
        limits.append(
            f"Не удалось подтвердить остановку {len(unconfirmed_stops)} "
            f"{_plural(len(unconfirmed_stops), 'разговора', 'разговоров', 'разговоров')} "
            "OpenHands, оставленных при повторах: они могли ещё работать. Проверьте их "
            "в OpenHands.")
    unverifiable = [t for t in tasks if not t.get("repository")]

    # ------------------------------------------------------------- the tasks
    done_lines: list[str] = []
    task_data: list[dict] = []
    for index, task in enumerate(tasks, 1):
        accepted = _accepted(task)
        history = by_task.get(str(task["id"]), [])
        last = history[-1] if history else {}
        executor = (last.get("executor_label") or "").strip() or "серверный ключ OpenHands"
        where = (f"`{task['repository']}`"
                 + (f" · `{task['branch']}`" if task.get("branch") else "")
                 if task.get("repository") else "без репозитория")
        commit = _short(accepted.get("remote_sha"))
        line = (f"{index}. **{_clip(task.get('title') or task['task_key'], 160)}** — {where}"
                + (f", коммит `{commit}`" if commit else "")
                + f" · исполнитель: {executor}"
                + (f" · запусков: {len(history)}" if len(history) > 1 else ""))
        summary = _agent_summary(task)
        if summary:
            line += "\n\n" + _quote(_clip(summary, AGENT_SUMMARY_CHARS))
        done_lines.append(line)
        task_data.append({
            "task_key": task["task_key"], "title": task.get("title"),
            "repository": task.get("repository") or "", "branch": task.get("branch") or "",
            "commit": accepted.get("remote_sha"), "changed": accepted.get("changed"),
            "starts": len(history), "cycle": int(task.get("cycle") or 0),
            "executor": executor,
            "executors": [e.get("executor_label") or "" for e in history],
            "verification": (task.get("verification") or {}).get("reason")})

    # ------------------------------------------------------ the repositories
    # What NEXUS itself saw change. ``moved``: new commits between the start
    # and now; ``still``: the head is the commit the work started from;
    # ``unknown``: the start is not known. Tasks accepted for different commits
    # of one branch show that it moved even when the start is not known.
    moved: list[str] = []
    still: list[str] = []
    unknown: list[str] = []
    #: Notes about whether anything changed. They stand beside a success; a
    #: report that says "no changes" in its headline does not repeat them.
    change_notes: list[str] = []
    accepted_in: dict[tuple[str, str], set[str]] = {}
    for item in task_data:
        if item["repository"] and item["commit"]:
            accepted_in.setdefault((item["repository"], item["branch"]), set()).add(
                item["commit"])
    repo_sections: list[str] = []
    for repo in repositories:
        base, head = repo.get("base_sha"), repo.get("head_sha")
        total = int(repo.get("total_commits") or 0)
        files = list(repo.get("files") or [])
        commits = list(repo.get("commits") or [])
        added = sum(int(f.get("additions") or 0) for f in files)
        removed = sum(int(f.get("deletions") or 0) for f in files)
        lines = [f"#### `{repo['repository']}`"
                 + (f" · ветка `{repo['branch']}`" if repo.get("branch") else "")]
        if base and head and base != head:
            listed = int(repo.get("files_listed") or len(files))
            lines.append(
                f"- Было `{_short(base)}` → стало `{_short(head)}`: {total} "
                f"{_plural(total, 'коммит', 'коммита', 'коммитов')}, {listed} "
                f"{_plural(listed, 'файл', 'файла', 'файлов')} (+{added} −{removed}) — "
                "по данным GitHub.")
            moved.append(repo["repository"])
        elif base and head:
            lines.append(f"- Вершина ветки не изменилась: `{_short(head)}`. Новых коммитов "
                         "за время прогона нет.")
            change_notes.append(f"в {repo['repository']} нет изменений")
            still.append(repo["repository"])
        else:
            lines.append(f"- Вершина ветки сейчас: `{_short(head)}`. С какого коммита прогон "
                         "начал, установить не удалось, поэтому список изменений не построен.")
            commits_accepted = len(accepted_in.get(
                (repo["repository"], repo.get("branch") or ""), ()))
            if commits_accepted > 1:
                lines.append("- Ветка менялась за время прогона: задачи приняты по разным "
                             f"коммитам ({commits_accepted}).")
                notes.append(f"список изменений в {repo['repository']} не построен")
                moved.append(repo["repository"])
            else:
                change_notes.append(f"изменения в {repo['repository']} не подтверждены")
                unknown.append(repo["repository"])
        if repo.get("comparison_error"):
            lines.append("- Список коммитов и файлов GitHub не отдал: "
                         f"{_clip(repo['comparison_error'], 200)}.")
        if commits:
            shown = commits[-LISTED_COMMITS:]
            lines.append("- Коммиты" + (f" (последние {len(shown)} из {total})"
                                        if total > len(shown) else "") + ":")
            lines += [f"  - `{_short(c.get('sha'))}` {_clip(c.get('message'), 140)}"
                      for c in shown]
        if files:
            shown_files = files[:LISTED_FILES]
            listed = int(repo.get("files_listed") or len(files))
            lines.append("- Файлы" + (f" (первые {len(shown_files)} из {listed})"
                                      if listed > len(shown_files) else "") + ":")
            lines += [f"  - `{f['path']}` ({f.get('status') or 'modified'}, "
                      f"+{int(f.get('additions') or 0)} −{int(f.get('deletions') or 0)})"
                      for f in shown_files]
        lines.append(f"- Задачи: {', '.join(repo.get('tasks') or []) or '—'}. Коммиты, по "
                     "которым они приняты, содержатся в ветке — проверено при итоговой сверке.")
        if repo.get("unchanged"):
            # Said as what was seen. Why nothing was pushed - nothing needed
            # changing, or the push was refused - is in the agent's own words.
            lines.append("- Приняты без изменения ветки (за время их работы новых коммитов "
                         "не появилось): " + ", ".join(repo["unchanged"]) + ".")
            change_notes.append("часть задач принята без изменений")
        checks = repo.get("checks")
        if isinstance(checks, dict) and checks.get("total"):
            if checks.get("failed"):
                names = ", ".join(checks.get("names_failed") or [])
                lines.append(f"- Проверки репозитория на `{_short(head)}`: **не пройдены** "
                             f"{checks['failed']} из {checks['total']}"
                             + (f" ({names})" if names else "") + ".")
                notes.append(f"проверки {repo['repository']} не пройдены")
                limits.append(f"Проверки (CI) репозитория `{repo['repository']}` на итоговом "
                              "коммите не пройдены. NEXUS их не перезапускал и по ним прогон "
                              "не останавливал.")
            elif checks.get("pending"):
                lines.append(f"- Проверки репозитория на `{_short(head)}`: ещё идут "
                             f"({checks['pending']} из {checks['total']}).")
                notes.append(f"проверки {repo['repository']} не завершены")
            else:
                lines.append(f"- Проверки репозитория на `{_short(head)}`: пройдены "
                             f"({checks['passed']} из {checks['total']}).")
        elif isinstance(checks, dict):
            lines.append(f"- Проверок (CI) на `{_short(head)}` в репозитории нет или они ещё "
                         "не начались.")
        elif repo.get("checks_error"):
            lines.append("- Состояние проверок (CI) GitHub не отдал: "
                         f"{_clip(repo['checks_error'], 160)}.")
        repo_sections.append("\n".join(lines))

    if unverifiable:
        notes.append("есть задачи без репозитория")
    if not repositories and any(t.get("repository") for t in tasks):
        limits.append("Итоговая сверка с GitHub не выполнялась (у проверяющего нет доступа "
                      "к сверке): список коммитов и файлов не построен.")

    # --------------------------------------------------------------- headline
    # "Done" is said only of what was seen: a run in which no repository has a
    # new commit is not reported as done, whatever its tasks were accepted for.
    beside = list(dict.fromkeys(notes))
    seen = list(dict.fromkeys(notes + change_notes))
    if repositories and not moved:
        outcome = "unconfirmed" if unknown else "no_changes"
        if outcome == "no_changes":
            status = (
                "**Статус:** изменений в репозитории нет — результат не подтверждён.\n\n"
                "Задачи приняты по отчётам агентов, но ни одного нового коммита за время "
                "прогона NEXUS не увидел: вершина ветки та же, что до начала работы. Если "
                "работа должна была изменить код, она не доставлена. Что произошло, сказано "
                "в отчёте агента ниже.")
            limits.insert(0, (
                "Новых коммитов в репозитории нет. Задачи приняты, потому что вершина ветки "
                "названа в отчёте агента, — это правило выполняется и тогда, когда агент "
                "ничего не отправил и назвал прежнюю вершину. Прогон из-за этого задачи не "
                "перезапускал."))
        else:
            status = (
                "**Статус:** завершено, но изменения в репозитории не подтверждены.\n\n"
                "NEXUS не знает, с какого коммита начиналась работа, и не может сказать, "
                "появились ли в ветке новые коммиты. Что произошло, сказано в отчёте агента "
                "ниже.")
            limits.insert(0, (
                "Начальный коммит неизвестен (прогон начат до того, как NEXUS стал его "
                "записывать, или GitHub тогда не ответил): появились ли новые коммиты, "
                "не установлено."))
        if beside:
            status += "\n\nЗамечания: " + "; ".join(beside) + "."
    else:
        outcome = "success_with_notes" if seen else "success"
        status = ("**Статус:** выполнено, все задачи проверены и подтверждены в репозиториях."
                  if outcome == "success" else
                  "**Статус:** выполнено и подтверждено в репозиториях, с замечаниями: "
                  + "; ".join(seen) + ".")
    created, updated = run.get("created_at"), run.get("updated_at")
    took = ""
    if created is not None and updated is not None:
        try:
            took = " · длительность: " + _duration((updated - created).total_seconds())
        except (AttributeError, TypeError):
            took = ""
    head_line = (f"Прогон `{str(run['id'])[:8]}` · задач: {len(tasks)} · запусков: {starts}"
                 + took)

    # -------------------------------------------------------------- the task
    text = task_text or ""
    meta = task_meta or {}
    if len(text) > TASK_QUOTE_CHARS:
        size = meta.get("chars") or len(text)
        digest = str(meta.get("sha256") or "")[:12]
        quoted = _quote(text[:TASK_QUOTE_CHARS].rstrip()) + (
            f"\n\n_Задание длиннее: {size} символов"
            + (f", SHA-256 `{digest}…`" if digest else "")
            + ". Здесь его начало; полный текст сохранён без изменений и доступен по кнопке "
              "копирования исходного сообщения._")
    else:
        quoted = _quote(text) if text.strip() else "_Текст задания не сохранён._"

    verified = sum(1 for t in tasks if t.get("state") == "completed_verified")
    checks_note = (
        "- По каждой задаче: вершина её ветки на GitHub — коммит, названный в отчёте "
        f"агента. Принято {verified} из {len(tasks)}.\n"
        + ("- Итоговая сверка после последней задачи: коммиты, по которым приняты задачи, "
           "по-прежнему содержатся в ветках. Список коммитов и файлов — из GitHub.\n"
           if repositories else "")
        + ("- Новых коммитов нет: подтверждать в репозитории нечего.\n"
           if outcome == "no_changes" else "")
        + "- Содержание изменений и результаты тестов NEXUS сам не проверял: они приведены "
          "со слов агентов (блоки с цитатами в разделе «Что сделано»).")

    parts = [
        "## Отчёт автономного прогона", status, head_line,
        "### Исходное задание", quoted,
        "### Что сделано", "\n\n".join(done_lines) or "_Задач не было._",
    ]
    if repo_sections:
        parts += ["### Репозитории", "\n\n".join(repo_sections)]
    parts += ["### Независимая проверка", checks_note]
    parts += ["### Ограничения и блокеры",
              "\n".join(f"- {item}" for item in limits) if limits else
              "- Блокеров не осталось. Повторов и смен исполнителя не было."]
    body = "\n\n".join(parts)
    if len(body) > MAX_REPORT_CHARS:
        body = (body[:MAX_REPORT_CHARS].rstrip()
                + "\n\n_Отчёт сокращён: полные данные — в записи прогона._")

    data = {
        "outcome": outcome, "notes": seen, "run_id": str(run["id"]),
        "tasks": task_data, "task_count": len(tasks), "starts": starts,
        "handoffs": handoffs, "cycles": cycles,
        "task": {"chars": meta.get("chars") or len(text), "sha256": meta.get("sha256"),
                 "spec_id": meta.get("id")},
        "repositories": [{
            "repository": r["repository"], "branch": r.get("branch") or "",
            "base_sha": r.get("base_sha"), "head_sha": r.get("head_sha"),
            "total_commits": int(r.get("total_commits") or 0),
            "files_listed": int(r.get("files_listed") or len(r.get("files") or [])),
            "commits": (r.get("commits") or [])[-LISTED_COMMITS:],
            "files": (r.get("files") or [])[:LISTED_FILES],
            "tasks": r.get("tasks") or [], "unchanged": r.get("unchanged") or [],
            "checks": r.get("checks")} for r in repositories],
        # Which repositories NEXUS saw new commits in, which it saw none in,
        # and which it cannot say for: what ``outcome`` was decided from.
        "changes": {"changed": moved, "unchanged": still, "unknown": unknown},
        "limits": limits,
    }
    return body, data, outcome


class ReportService:
    """Write a completed run's report and deliver it to the chat it came from."""

    def __init__(self, store: PostgresStore):
        self.store = store

    # ---------------------------------------------------------------- writing
    def on_completed(self, confirmation: dict | None) -> Callable[[Any, dict], None]:
        """The hook that writes the report when the run is completed.

        Meant for ``LabStore.set_run_state_internal(..., also=)``: it runs on
        the cursor of the transaction that makes the run ``completed``, so the
        report exists exactly when the run is completed. *confirmation* is what
        the final look at the repositories found (``GitVerifier.confirm``); it
        was gathered before the transaction, because nothing here may wait on
        a network.
        """
        def also(cur: Any, run: dict) -> None:
            self.write(cur, run, confirmation)
        return also

    def write(self, cur: Any, run: dict, confirmation: dict | None) -> None:
        run_id = str(run["id"])
        cur.execute("SELECT g.text, g.spec_id FROM nexus_goals g WHERE g.id=%s", (run["goal_id"],))
        goal = cur.fetchone() or {}
        text, meta = goal.get("text") or "", None
        if goal.get("spec_id"):
            cur.execute("SELECT id, content, char_count, sha256 FROM nexus_specs WHERE id=%s",
                        (goal["spec_id"],))
            spec = cur.fetchone()
            if spec is not None:
                text = spec["content"]
                meta = {"id": str(spec["id"]), "chars": spec["char_count"],
                        "sha256": spec["sha256"]}
        cur.execute("SELECT * FROM nexus_run_tasks WHERE run_id=%s ORDER BY created_at, id",
                    (run_id,))
        tasks = [_task_view(row) for row in cur.fetchall()]
        cur.execute("SELECT task_id, kind, executor_label, conversation_id, repository, branch, "
                    "  started_at, ended_at, outcome FROM nexus_task_executions "
                    "WHERE run_id=%s ORDER BY started_at, id", (run_id,))
        executions = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM nexus_task_handoffs WHERE run_id=%s", (run_id,))
        handoffs = int(cur.fetchone()["n"])
        cur.execute("SELECT state FROM nexus_executor_stops WHERE run_id=%s", (run_id,))
        stops = [dict(row) for row in cur.fetchall()]
        body, data, outcome = build_report(
            run=run, task_text=text, task_meta=meta, tasks=tasks, executions=executions,
            confirmation=confirmation, stops=stops, handoffs=handoffs)
        cur.execute(
            "INSERT INTO nexus_run_reports(run_id, project_id, conversation_id, state, body, "
            "  summary, outcome) "
            "SELECT %s, %s, c.id, CASE WHEN c.id IS NULL THEN 'undeliverable' ELSE 'pending' END, "
            "  %s, %s::jsonb, %s "
            "FROM (SELECT %s::uuid AS wanted) w "
            "LEFT JOIN nexus_conversations c ON c.id = w.wanted "
            "ON CONFLICT (run_id) DO NOTHING",
            (run_id, run["project_id"], body, json.dumps(data, ensure_ascii=False, default=str),
             outcome, run.get("conversation_id")))
        cur.execute("INSERT INTO nexus_notifications(project_id, kind, title, resource_id) "
                    "VALUES (%s, 'run_report', 'Отчёт автономного прогона готов', %s)",
                    (run["project_id"], run_id))

    # --------------------------------------------------------------- delivery
    def deliver_due(self, *, run_id: str | None = None, limit: int = 20) -> dict[str, int]:
        """Put every report that is due into its conversation.

        Each report is delivered in its own transaction: the row is locked, the
        message is appended and the row is marked ``delivered`` together. A
        delivery that fails changes nothing and is tried again later, with a
        pause that grows; the attempt and the error are recorded.
        """
        summary = {"delivered": 0, "failed": 0, "undeliverable": 0}
        with self.store._tx() as cur:
            cur.execute(
                "SELECT run_id FROM nexus_run_reports WHERE state='pending' "
                "AND next_attempt_at <= now() AND (%s::uuid IS NULL OR run_id=%s) "
                "ORDER BY next_attempt_at, created_at LIMIT %s", (run_id, run_id, limit))
            due = [str(row["run_id"]) for row in cur.fetchall()]
        for report_run in due:
            try:
                outcome = self._deliver(report_run)
            except Exception as e:  # noqa: BLE001 - recorded; delivery is tried again
                log.exception("report of run %s was not delivered", report_run)
                self._failed(report_run, f"{type(e).__name__}: {e}")
                summary["failed"] += 1
                continue
            if outcome in summary:
                summary[outcome] += 1
        return summary

    def _deliver(self, run_id: str) -> str:
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_run_reports WHERE run_id=%s AND state='pending' "
                        "FOR UPDATE SKIP LOCKED", (run_id,))
            report = cur.fetchone()
            if report is None:
                return "skipped"            # delivered meanwhile, or in another's hands
            conversation_id = report["conversation_id"]
            if conversation_id is not None:
                cur.execute("SELECT 1 FROM nexus_conversations WHERE id=%s FOR KEY SHARE",
                            (conversation_id,))
                if cur.fetchone() is None:
                    conversation_id = None
            if conversation_id is None:
                cur.execute("UPDATE nexus_run_reports SET state='undeliverable', "
                            "attempts=attempts+1, last_error=%s WHERE run_id=%s",
                            ("the conversation the run was started from no longer exists",
                             run_id))
                return "undeliverable"
            message = self._post(cur, str(conversation_id), dict(report))
            cur.execute("UPDATE nexus_run_reports SET state='delivered', message_id=%s, "
                        "attempts=attempts+1, last_error=NULL, delivered_at=now() "
                        "WHERE run_id=%s", (message["id"], run_id))
            return "delivered"

    def _post(self, cur: Any, conversation_id: str, report: dict) -> dict:
        """Append the report to the conversation, on the caller's cursor."""
        return ConversationService.append_internal(
            cur, conversation_id, role="assistant", content=report["body"],
            meta={"mode": "autonomous", "run_id": str(report["run_id"]), "report": True,
                  "outcome": report["outcome"]})

    def _failed(self, run_id: str, error: str) -> None:
        with self.store._tx() as cur:
            cur.execute(
                "UPDATE nexus_run_reports SET attempts=attempts+1, last_error=%s, "
                "next_attempt_at = now() + LEAST(%s, %s * power(2, LEAST(attempts, 12))) "
                "  * interval '1 second' WHERE run_id=%s AND state='pending'",
                (error[:1000], DELIVERY_RETRY_MAX_SECONDS, DELIVERY_RETRY_SECONDS, run_id))

    # ---------------------------------------------------------------- reading
    def get(self, principal: str, project_id: str, run_id: str) -> dict:
        """A run's report, for a member of its project."""
        with self.store._tx() as cur:
            ProjectService._project(cur, principal, project_id)
            cur.execute("SELECT * FROM nexus_run_reports WHERE run_id=%s AND project_id=%s",
                        (run_id, project_id))
            row = cur.fetchone()
            if row is None:
                raise NotFound("report not found")
            return _report_view(row)

    def for_run(self, run_id: str) -> dict | None:
        """A run's report as its card shows it, or ``None``. The caller has
        already established that the reader may see the run."""
        with self.store._tx() as cur:
            cur.execute("SELECT * FROM nexus_run_reports WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
            return _report_view(row) if row else None

    def brief(self, run_id: str) -> dict | None:
        """Whether a run has a report and where it stands - no text."""
        with self.store._tx() as cur:
            cur.execute("SELECT run_id, state, outcome, conversation_id, message_id, attempts, "
                        "  last_error, created_at, delivered_at FROM nexus_run_reports "
                        "WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
            return _report_view(row) if row else None


def _report_view(row: Any) -> dict:
    out = dict(row)
    for key in ("run_id", "project_id", "conversation_id", "message_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


def _task_view(row: Any) -> dict:
    out = dict(row)
    for key in ("evidence", "verification", "spec_ref"):
        value = out.get(key)
        if isinstance(value, str):
            out[key] = json.loads(value)
    out["evidence"] = out.get("evidence") or []
    return out
