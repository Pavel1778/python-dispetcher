# Entity lifecycle map

The states each entity can be in, the transitions between them, and the rule
that makes each transition safe. This is the design the CRUD matrix
(`docs/CRUD-MATRIX.md`) implements; it is not applied mechanically — an entity
that cannot be archived without breaking its work (an active run) does not get
an archive state.

## The four generic states

```
         create
           │
           ▼
        ACTIVE ──archive──▶ ARCHIVED ──unarchive──▶ ACTIVE
           │                   │
        trash│            trash│
           ▼                   ▼
        TRASHED ◀──────────────┘
           │  restore → ACTIVE
           │
    permanent delete (second, explicit step)
           ▼
     REMOVED
```

- **ACTIVE** — in the working lists; everything works.
- **ARCHIVED** — hidden from the default list, otherwise untouched: it can still
  be opened and worked in. `archived`/`archived_at`.
- **TRASHED** — a soft delete: hidden from every list, refuses *new* work, and
  reversible. `deleted`/`deleted_at`.
- **REMOVED** — gone. Only reachable from TRASHED, never directly from ACTIVE,
  so one click can never destroy a live entity.

## Which entities use which states

| Entity | Archive | Trash | Permanent delete | Notes |
| --- | --- | --- | --- | --- |
| Project | yes | yes | yes | Trash refused while an active run exists. Permanent delete takes everything project-scoped (cascade). |
| Conversation | yes | yes | yes | Permanent delete keeps any run the chat started (`conversation_id` SET NULL). |
| Message | no | no | with its conversation | History is the record; editing/deleting one message would falsify the context an agent saw. A new message is the way to change course. |
| Run | hide/show | no | remove-from-history | A run has a real state machine (`running`…`completed`); "hide" is the archive. Only a terminal run can be removed. Evidence is kept. |
| Task | no | no | with its run | A task is part of its run. |
| File | no | no | yes (delete) | Immutable once uploaded; a delete is final (no retention policy yet). |
| Notification | no | no | yes (delete) | A message, not history: deleted outright. |
| GitHub account | enable/disable | no | remove | Refused while a project depends on it. |
| OpenHands account | enable/disable | no | remove | Refused while a project depends on it. History of tasks is kept. |
| Memory fact | no | no | no | Append/supersede only; a delete would break provenance. |
| Session | no | no | logout / expiry | Server-side; logout ends it immediately. |

## The safety rules each transition enforces

1. **Trash is required before permanent delete.** A `DELETE` with the
   `permanent` flag (or a bare project `DELETE`) checks `deleted_at` first and
   refuses a live entity with a clear message.
2. **An active run blocks a destructive parent action.** A project with a run in
   `running`/`paused`/`waiting_for_approval`/`blocked` cannot be trashed or
   permanently deleted; the runs are cancelled first (a separate, explicit
   action), so a working agent is never cut off silently.
3. **A dependency blocks an account removal.** Removing a GitHub or OpenHands
   account is refused while a project still depends on it.
4. **Cascade is per-relationship, never blanket.** A project delete cascades
   into project-scoped tables; a run's link to its conversation is `SET NULL`
   (the run survives the chat); a spec's link is `SET NULL`.
5. **Every write is authorized.** Membership is checked before the row is
   touched; a non-member gets 404 (never 403, never a disclosure).
6. **A wrong type is a 400, not a 500.** `archived`/`deleted`/`read` must be
   booleans.

## Restore rules

- **A conversation or project restored from the trash returns to its place.**
  Trashing does not touch `updated_at`, so the list order is preserved.
- **A project restored from the trash is active again** and accepts new work.
- **A restored entity keeps its dependencies.** Nothing is cascaded on trash, so
  a restore is complete.
