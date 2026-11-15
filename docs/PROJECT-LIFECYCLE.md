# Project and conversation lifecycle

Projects and conversations can be created, renamed, archived, moved to the
trash, restored, and purged. This document is the contract; the studio is one
client of it.

## Two independent states

| state | column | what it means |
|---|---|---|
| archived | `archived_at` (projects), `archived` (conversations) | out of the active list, kept in full; fully usable |
| deleted | `deleted_at` | in the trash; hidden from every read; reversible |

They are separate on purpose: restoring from the trash must not restore from the
archive, and a project can be archived and later deleted without one undoing the
other.

**Nothing is removed by a delete.** A soft-deleted project or conversation stays
in the database, its data intact. **Purge** is the separate, explicit,
irreversible step.

## Projects

| action | route | effect |
|---|---|---|
| list | `GET /api/v1/projects[?archived=1][&deleted=1]` | live by default; archived and trash on request |
| read | `GET /api/v1/projects/{id}` | 404 for a project in the trash |
| rename | `PATCH /api/v1/projects/{id}` | name and/or description |
| archive | `POST /api/v1/projects/{id}/archive` | sets `archived_at` |
| unarchive | `POST /api/v1/projects/{id}/unarchive` | clears `archived_at` |
| delete | `POST /api/v1/projects/{id}/delete` or `DELETE /api/v1/projects/{id}` | sets `deleted_at` |
| restore | `POST /api/v1/projects/{id}/restore` | clears `deleted_at` |
| stats | `POST /api/v1/projects/{id}/stats` | what it holds, for the confirmation |
| purge | `POST /api/v1/projects/{id}/purge` `{"confirm": true}` | removes it and its contents |

`archive`/`restore`/`delete`/`purge` require the `owner` role
(`SCOPE_PROJECT_ADMIN`). A non-member gets 404, never 403 - the existence of a
project is not disclosed.

### Safe deletion

`stats` reports conversations, runs, active runs, tasks, goals, members and the
bound repositories. `delete` and `purge` **refuse (409)** while any run of the
project is active: a run that is at work must be stopped first, so its
conversations are not left running against a project nobody can see.

### What a purge removes, and what it does not

`purge` deletes rows that belong to the project. The foreign keys are
`ON DELETE CASCADE` for the project's own data (conversations, runs, tasks,
goals, specs, files, memory) and `ON DELETE SET NULL` for records that outlive a
project (evidence, legacy tasks). Therefore:

- **The external GitHub repository is never deleted.** Only the project↔repo
  link (`nexus_project_repositories`) goes; the `repositories` row - the
  allowlist entry - stays.
- **No other project is affected.** Its conversations, runs, repository links
  and settings are untouched. `tests/test_lifecycle.py` asserts this.

## Conversations

| action | route | effect |
|---|---|---|
| list | `GET /api/v1/projects/{id}/conversations[?q=&archived=1][&deleted=1]` | live by default |
| read | `GET /api/v1/projects/{id}/conversations/{cid}` | 404 when trashed |
| rename | `PATCH …/conversations/{cid}` `{"title": …}` | |
| mode | `PATCH …/conversations/{cid}` `{"mode": …}` | |
| archive | `PATCH …/conversations/{cid}` `{"archived": true|false}` | reversible |
| pin | `PATCH …/conversations/{cid}` `{"pinned": true|false}` | sorts it first |
| delete | `DELETE …/conversations/{cid}` or `POST …/conversations/{cid}/delete` or `PATCH … {"deleted": true}` | sets `deleted_at` |
| restore | `POST …/conversations/{cid}/restore` or `PATCH … {"deleted": false}` | clears `deleted_at` |
| stats | `GET`/`POST …/conversations/{cid}/stats` | message/attachment/session counts |
| purge | `POST …/conversations/{cid}/purge` `{"confirm": true}` | removes it and its messages |

**Pin** is ordering only: a pinned conversation sorts to the top of the list
(`ORDER BY pinned DESC, updated_at DESC`); it changes nothing else. It was added
by `0039_conversation_pin.sql` when the two independent lifecycle lines were
merged (see [../CHANGELOG.md](../CHANGELOG.md)).

`delete` and `purge` refuse (409) while a run the conversation started is still
at work - the active execution would otherwise be stranded with no chat to
report to. Stop the run first (`POST /api/v1/projects/{id}/runs/{run}/cancel`).

A conversation's messages, attachments and agent sessions are kept through a
delete and come back with a restore. A **purge** removes the conversation and
its messages; the **run** it started is not removed - a run is a record of work,
and removing a chat must not remove it.

## The studio

Each project row in the project menu has a context menu (Open, Rename, Settings,
Archive, Delete; and Restore / Delete forever for one in the trash). Each chat
row has one too (Rename, Archive, Delete; and Restore / Delete forever for one
in the trash). A delete opens a confirmation that names what is inside, says
that the external repository is not touched, and warns when an active run will
block it. After any action the list is reloaded from the server, so a deleted
item can never linger from a stale cache. The **Показать архив и корзину** item
in the project menu and the **Архив**/**Корзина** folds in the chat list are how
archived and trashed items are reached.

## Migration

`migrations/0038_lifecycle.sql` is additive: three nullable columns
(`nexus_projects.archived_at`, `nexus_projects.deleted_at`,
`nexus_conversations.deleted_at`) and two partial indexes. No existing row is
rewritten. Rolling back is dropping the columns and indexes; no data is lost by
applying it.
