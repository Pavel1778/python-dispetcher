# NEXUS CRUD matrix

A complete audit of every entity NEXUS manages, the operations that exist for
it, and where each is enforced. Statuses:

- **DONE** — the operation exists, is enforced on the server, and is tested.
- **API ONLY** — the operation exists over the API but has no screen control.
- **N/A** — the operation has no meaning for the entity (and why).
- **GAP** — the operation should exist and does not (with the plan).

`Create`/`Read`/`Update`/`Delete` are the plain operations; the *Lifecycle*
column names the softer states (archive / trash / restore / permanent) the
entity supports. `Project-scoped` means every route is under
`/api/v1/projects/{id}/...` and re-checks membership (a non-member gets 404).

## 1. Users & access

| Operation | Status | Where |
| --- | --- | --- |
| Create a user | N/A | Single-owner model: `AUTH_USERNAME` + `AUTH_PASSWORD_HASH` from the environment. There is no user table to create rows in. |
| Read the current user | DONE | `GET /api/session` (`username`, `csrf_token`) |
| Update the password | N/A (by design) | `python -m orchestration.web_auth hash` writes a new `AUTH_PASSWORD_HASH`; a change ends every open session. Not an API operation, deliberately. |
| List sessions / revoke one | GAP | `sessions` rows exist and expire; there is no `GET/DELETE /api/sessions`. Logout ends the caller's own session. |
| Project membership | DONE (read) | `nexus_project_members`; role read through `require_scope`. |
| Add/remove a member | GAP | The owner is the only member; a multi-user surface does not exist. |
| API credentials (tokens) | N/A | No token table; the machine bearer is `WEB_AUTH_TOKEN` from the environment. |
| Secrets (GitHub/OpenHands keys) | DONE | Encrypted at rest (`secrets.py`); never returned; `verify`/`connection` re-check them. |

## 2. Projects

| Operation | Status | Where |
| --- | --- | --- |
| Create | DONE | `POST /api/v1/projects` |
| Read (list) | DONE | `GET /api/v1/projects[?archived=1&deleted=1]` |
| Read (detail) | DONE | `GET /api/v1/projects/{id}` |
| Update (name/description) | DONE | `PATCH /api/v1/projects/{id}` |
| Archive / unarchive | DONE | `PATCH /api/v1/projects/{id}` `{archived}` |
| Trash / restore | DONE | `PATCH /api/v1/projects/{id}` `{deleted}` |
| Permanent delete | DONE | `DELETE /api/v1/projects/{id}` (trash first; refused while a run is active) |
| Dependents census | DONE | `GET /api/v1/projects/{id}/dependents` |
| Repository binding | DONE | `POST /api/v1/projects/{id}/repository`, `/binding`, `/binding/branch` |
| Executors (list) | DONE | `POST/GET /api/v1/projects/{id}/executors` |
| Search / filter / sort | DONE (client) | The list is small; the studio filters and groups client-side. |
| Pagination | GAP (bounded) | The list is capped at 200 rows server-side, not paginated. |
| Bulk actions | GAP | No multi-select; each action is per-project. |

## 3. Conversations

| Operation | Status | Where |
| --- | --- | --- |
| Create | DONE | `POST /api/v1/projects/{id}/conversations` |
| Read (list, search) | DONE | `GET .../conversations[?q=&archived=1&deleted=1]` (title and message text) |
| Read (detail) | DONE | `GET .../conversations/{cid}` |
| Update (title, mode) | DONE | `PATCH .../conversations/{cid}` |
| Pin / unpin | DONE | `PATCH .../conversations/{cid}` `{pinned}` |
| Archive / restore | DONE | `PATCH .../conversations/{cid}` `{archived}` |
| Trash / restore | DONE | `PATCH .../conversations/{cid}` `{deleted}` |
| Permanent delete | DONE | `DELETE .../conversations/{cid}?permanent=1` (trash first) |
| Export | DONE | `GET .../conversations/{cid}/export` |
| Messages | DONE | `GET/POST .../conversations/{cid}/messages` |
| Stop generation | DONE | `POST .../conversations/{cid}/cancel` |
| Move to another project | GAP | A conversation is scoped to its project; moving it would rewrite `project_id` and re-authorize. Not offered. |
| Bulk actions | GAP | No multi-select. |

## 4. Messages

| Operation | Status | Where |
| --- | --- | --- |
| Create (send) | DONE | `POST .../conversations/{cid}/messages` |
| Read | DONE | `GET .../conversations/{cid}/messages` |
| Edit a user message | GAP | A message that an agent already acted on is not rewritten (that would falsify the context the agent saw). The safe path is a new message; a "resend" control re-sends the last user turn. |
| Delete a message | GAP | Same reasoning: history is the record. Permanent delete removes the whole conversation. |
| Regenerate / resend | DONE (client) | The retry control re-sends the last user turn. |
| Copy | DONE (client) | Copy button on every message. |
| Attachments | DONE | `nexus_conversation_attachments`; listed with the messages. |

## 5. Tasks & runs

| Operation | Status | Where |
| --- | --- | --- |
| Create (start a run) | DONE | `POST .../goals/{gid}/run`, chat `/autorun` |
| Read (list, detail, report) | DONE | `GET .../runs[?archived=1]`, `.../runs/{id}`, `.../runs/{id}/report` |
| Cancel | DONE | `POST .../runs/{id}/cancel` |
| Pause / resume | DONE | `POST .../runs/{id}/{pause,resume}` |
| Hide / show | DONE | `POST .../runs/{id}/{hide,show}` |
| Retry failed tasks | DONE | `POST .../runs/{id}/retry-failed` |
| Remove from history | DONE | `DELETE .../runs/{id}` (terminal runs only) |
| Switch executor | DONE | `POST .../runs/{r}/tasks/{t}/switch` |
| Evidence / executions / switches / events | DONE | part of `GET .../runs/{id}` |
| Permanent delete of a run | DONE (as remove-from-history) | `DELETE .../runs/{id}`; evidence is kept for audit |
| Bulk actions | GAP | No multi-select. |

## 6. Agents & OpenHands accounts

| Operation | Status | Where |
| --- | --- | --- |
| Create | DONE | `POST /api/openhands/accounts` |
| Read (list/detail/capabilities/errors) | DONE | `GET /api/openhands/accounts[...]` |
| Update (label, base_url, model, key, limits) | DONE | `POST /api/openhands/accounts/{id}/edit` |
| Enable/disable, priority, default | DONE | `POST .../{id}/{enable,disable,priority,default}` |
| Connection check / verify | DONE | `POST .../{id}/connection`, `/verify` |
| Delete | DONE | `POST .../{id}/remove` (refused while a project depends on it) |
| Secrets | DONE | Encrypted; `api_key_hint` only; never returned |
| Bulk actions | GAP | Per-account only. |

## 7. LLM providers & models

| Operation | Status | Where |
| --- | --- | --- |
| Read (cascade, presence) | DONE | `GET /api/providers[?live=1]` |
| Probe (real, read-only) | DONE | `GET /api/providers?live=1` |
| Add / change a provider | N/A (env) | Providers are the built-in cascade; a key is an environment variable. |
| Model selection | DONE | per account `default_model`, `OPENHANDS_DEFAULT_MODEL` |
| Priority / fallback | DONE | the cascade order in `providers.py` |
| Delete a provider | N/A | Removing its key from the environment removes it from the cascade. |

## 8. GitHub integration

| Operation | Status | Where |
| --- | --- | --- |
| Connect an account | DONE | `POST /api/github/accounts` (verified via `GET /user`) |
| Read (accounts, repositories, branches, tree, contents) | DONE | `GET /api/github/accounts[...]`, project-scoped GitHub reads |
| Bind a repository to a project | DONE | `POST /api/v1/projects/{id}/repository` |
| Unbind / change branch | DONE | `POST .../binding`, `/binding/branch`, `.../repositories/{owner}/{name}/{action}` |
| Remove an account | DONE | `POST /api/github/accounts/{id}/remove` |
| Create a repository | DONE (confirmed) | `POST .../github/repositories` then `/actions/{id}/confirm` |
| Change visibility | DONE (confirmed) | `POST .../github/repositories/{owner}/{name}/visibility` then confirm; a public change needs `acknowledge_public` |
| Delete a repository | N/A (by design) | NEXUS never deletes a GitHub repository; the guarded tools delete Cloud *conversations*, not repos. |

## 9. Files & File Laboratory

| Operation | Status | Where |
| --- | --- | --- |
| Upload (opaque + multipart) | DONE | `POST /api/v1/projects/{id}/files` |
| Read (list, preview, download) | DONE | `GET .../files`, `.../files/{id}`, `.../files/{id}/download` |
| Delete | DONE | `DELETE .../files/{id}` |
| Rename / move / copy | GAP | A file is immutable once uploaded; workspaces offer edit + diff. |
| Workspaces (create, files, diff) | DONE | `POST .../workspaces`, `.../workspaces/{ws}/files`, `.../diff` |
| Archives (safe unpack) | DONE | `archive_safety.py`; never written to disk |
| Restore / trash | GAP | A file delete is final; the retention policy is not implemented |
| Bulk actions | GAP | Per-file only |

## 10. MCP servers & tools

| Operation | Status | Where |
| --- | --- | --- |
| Read (inventory, scopes) | DONE | `GET /api/openhands/tools[?live=1]`, `GET /api/security/policy` |
| Configure an MCP server | N/A (account) | The MCP servers an agent has are the account's own configuration, inherited, never written by NEXUS |
| Test a connection | DONE | `GET /api/openhands/tools?live=1` (`mcp_configuration` check) |
| Per-tool permissions | DONE | `MCP_TOOL_SCOPES` + the role's scopes; re-checked per write |

## 11. Notifications

| Operation | Status | Where |
| --- | --- | --- |
| Read (list) | DONE | `GET .../notifications` |
| Mark read / unread | GAP | `nexus_notifications.read_at` exists; there is no route to set it |
| Archive / delete | GAP | No route |
| Bulk mark-as-read | GAP | No route |
| Preferences | GAP | No per-category settings |

## 12. Memory & context

| Operation | Status | Where |
| --- | --- | --- |
| Create (remember) | DONE | `MemoryService.remember` (internal + chat) |
| Read (retrieve, by project) | DONE | `GET .../memory[?q=]` |
| Supersede / update | DONE | `MemoryService.supersede` (a verified fact cannot be downgraded) |
| Delete | GAP | Memory is append/supersede only; a delete would break provenance |
| Project isolation | DONE | every row is `project_id`-scoped; a non-member gets 404 |
| Context (refresh, read, history) | DONE | `POST .../context/refresh`, `GET .../context` |

## 13. Settings

| Operation | Status | Where |
| --- | --- | --- |
| Read (studio, system, config, security policy) | DONE | `GET /api/studio`, `/api/system`, `/api/config`, `/api/security/policy` |
| Update (per-account, per-project) | DONE | the account/project routes above |
| Reset to default | GAP | No explicit "reset" route; a value is set to its default by writing it |
| Interface preferences (theme, density) | DONE (client) | `localStorage`; no server round trip |

## Summary of gaps (with priority)

1. **Notifications: mark read / unread / delete / bulk** — a real, low-risk gap;
   the `read_at` column already exists.
2. **Sessions: list / revoke** — useful for security; `sessions` rows exist.
3. **Pagination and bulk actions** on the large lists (projects, conversations,
   runs) — a scale gap, not a correctness gap; every list is already bounded.
4. **File trash/restore** — a retention policy is needed first.
5. **Message edit/delete, memory delete** — deliberately not offered (they would
   falsify a context an agent acted on); the safe path is a new message.

Items 1 and 2 are implemented in this increment; the rest are recorded here and
in `docs/KNOWN_LIMITATIONS.md` with the reason.
