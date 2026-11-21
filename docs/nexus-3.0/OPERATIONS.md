# Managed OpenHands sessions: operator contract

This is an opt-in Lab worker path, not a new deployment, not a Rust worker and not a guarantee that a model can do a task. See [acceptance and release decision](ACCEPTANCE.md). Keep the existing account/transport configuration for sessions that do not have a managed profile. Set `NEXUS_REQUIRE_EXECUTION_PROFILE=1` on all dispatching processes to refuse that compatibility path. A saved but unusable profile is **never** permission to fall back to another model or account.

## Three identities, not one API key

| Identity | Credential owner | Where it is used |
|---|---|---|
| NEXUS principal | NEXUS session + CSRF or operator machine bearer | Project membership, project admin/control scopes; never send the operator bearer to an agent |
| OpenHands connection | Server configuration or a NEXUS-owned account | Which OpenHands service/sandbox may receive the request. This URL is **not** the model Base URL. |
| Model provider connection | The selected **OpenHands agent-server** | Native provider connection holds the model key and the Base URL. NEXUS refers to its opaque ID (`key_ref`); it neither imports nor rotates the key. |

On the supported **agent-server 1.60.1**, provision the provider connection and a native `openhands` agent profile first. Preserve the runtime's own volume **and its encryption key** on restart. The same identifier on a different service is not a transferable credential. The adapter checks the runtime version and capabilities on the target, writes a named LLM profile if needed, and reads it back. Its name is derived from the desired configuration. It does not activate a global profile. A request contains the native `agent_profile_id` and `agent_launch_additions.llm_profile_ref`, not a raw LLM key.

## Configure a future launch

In Studio open Settings → Connections → **Модель исполнителя OpenHands**, or use the project settings for an override. The form shows the saved scope, revision, last sync and observation. Supply the runtime connection ID, not a provider secret. Do not put credentials in a model name, URL path or JSON. The HTTP/CLI can also set an override on a **pending Lab run task** (not an arbitrary OpenHands conversation).

```sh
# Environment NEXUS_URL and NEXUS_TOKEN are provided by the operator's secret manager.
nexus openhands profile show --project "$PROJECT_UUID" --json
nexus openhands profile save --project "$PROJECT_UUID" <desired.json
nexus openhands profile sync --project "$PROJECT_UUID" --json
nexus openhands profile test-model --project "$PROJECT_UUID" --json
```

`desired.json` has exactly seven fields:

| Field | Meaning |
|---|---|
| `provider`, `model` | Explicit provider prefix, e.g. `openai`, `openai/gpt-4o-mini` |
| `base_url` | Credential-free HTTPS model endpoint; must agree with the native connection |
| `key_ref` | Native provider-connection ID: 32 lowercase hex characters |
| `agent_profile_id` | UUID of the selected native OpenHands agent profile |
| `connection_id` | UUID of the caller-owned NEXUS OpenHands connection, or `null` for the deployment connection (operator only) |
| `params` | Object with optional `temperature`, `top_p`, `max_output_tokens`; arbitrary extra headers, keys and launch arguments are refused |

`GET`/`POST` paths: `/api/openhands/execution-profile` (deployment), `/api/v1/projects/{p}/execution-profile`, `/api/v1/projects/{p}/tasks/{t}/execution-profile`. For an explicitly saved scope, `POST …/sync` and `POST …/test-model` take `{}`. The latter calls a native preflight; even `valid=true` on 1.60.1 can mean a transient provider failure. **Verified configuration is not verified inference.** Resolve a blocked observation at the named connection; see the persisted reason. Errors do not include the raw peer response.

Precedence is task → project → deployment. Saving the same normalized JSON is idempotent. Changes increment a revision; a late sync cannot overwrite a newer revision. Startup checks the deployment default; scoped overrides are reconciled on the **actual target before create**. Editing a default is not a hot-switch of an existing conversation. For rollback save a known-good configuration as a **new** revision; do not edit table rows to reset a revision. There is no delete/reset-to-inheritance UI in this candidate.

## Execution and recovery boundaries

* **Local:** managed repository tasks require an operator-staged, clean checkout on the runtime at `NEXUS_MANAGED_CHECKOUT_ROOT/<project UUID>/<run-task UUID>`. Use an absolute root other than `/`, on a volume not writable by another tenant. Fetch **and push** `origin` must be exactly `https://github.com/owner/name.git`. The symbolic branch, worktree root and HEAD must match the authorized task and the fresh remote head read by the verifier. Missing head, dirty checkout, missing grant, or a rotated/expired fence blocks create. NEXUS does not provision that volume or clone the repo for you.
* **Direct:** checks against the assigned sandbox rather than modifying whichever sandbox was most recently used. With no assigned target, manual/startup sync reports blocked. Workspace reuse is partitioned by scope and repository/branch. Provisioning may incur sandbox costs **before** conversation create. This path is not accepted against hosted Direct on this revision.
* **Cloud App:** management is refused: the interface cannot attest a per-session native model connection and endpoint. Do not translate `blocked` into an unconfigured App create. Unmanaged sessions retain the previous account behaviour.
* **Lab journal:** one send permit; record the expected model/endpoint/parameters/cwd and original tags for a managed create. Check the created session by readback. If the reply or confirmation disappears, preserve uncertainty, read on the original route, and check against the **original** expectation even if the desired profile has since changed. No search miss, `404`, timeout or conflicting session is proof that a POST did not happen. Pre-journal sends lacking authoritative evidence stay held rather than consuming another start. A permanent hold needs independent operator reconciliation; no blind reset/retry switch is supplied.
* A `working_dir` is not OS isolation. For mutually untrusted users require separate sandbox identities, filesystems, network/credential boundaries and resource limits outside NEXUS. Do not operate a shared local agent-server as a multi-tenant security boundary.

## QA, evidence and an independent reader

The worker's `auto` quality gate may skip only on a confirmed absence of `.nexus-qa.json` at the verified revision. `off` is an explicit policy skip; neither is a check pass. `on` requires successful evidence. Unknown declaration, inconsistent check, missing required tool, skipped required command, wrong commit, stale invocation or checker exception cannot promote a task. When using the built-in host runner, the operator must separately authorize each reviewed `owner/name@<full-commit-sha>` in `NEXUS_SELFQA_TRUSTED_REVISIONS` (space-separated). A scratch clone and an environment filter are **not** a sandbox: never allowlist unreviewed code. For untrusted revisions use an external isolated verification service; this branch does not provide one. The local `nexus qa run` CLI is a trusted operator operation, not this allowlist.

For an actually run check, the signed result includes the checkout SHA, project/run/task/fence/invocation, times, required flags, argv, exit codes, status and SHA-256 of the pair of redacted/truncated output strings. That hash is not a hash of an unlimited raw terminal log. A valid signature attests **what the verifier recorded**, including a failed or policy-skipped result; it does not mean all code is correct. `nexus evidence verify … --expected-commit <sha>` re-verifies signature, indexed subject, body, entry link and expected commit. `nexus qa verify` re-verifies the returned project window; zero evidence, a full 100-row window (completeness unknown), or any non-VERIFIED record exits nonzero. Not a global atomic archive audit.

To delegate *reading* without handing an agent the owner bearer, set these server-side values together, then restart the web service:

| Environment | Required value |
|---|---|
| `NEXUS_MCP_VERIFY_TOKEN` | Independently generated high-entropy ASCII secret of at least 32 characters, distinct from `WEB_AUTH_TOKEN` |
| `NEXUS_MCP_VERIFY_PRINCIPAL` | Principal with membership in the target project |
| `NEXUS_MCP_VERIFY_PROJECT` | Exact project UUID |
| `NEXUS_MCP_VERIFY_EXPIRES` | Expiry as Unix seconds UTC |

Allow only HTTPS to the service. This optional bearer is accepted **on `POST /mcp` only**. It can list a reduced evidence view, re-verify one receipt (`verify_evidence`, optionally with `expected_commit`, no persistence), and read the effective model selection (`get_execution_profile`). It cannot dispatch, assign an executor, cancel a run, sign results, see full transcripts or change a profile. Expiry and current membership are checked; remove membership to revoke project access, or remove/replace the grant and restart. This is one statically configured grant, not a multi-user token issuer. Never give an untrusted agent the ordinary CLI owner token, direct PG access or the stdio server's configuration.

## Upgrade, rollback, prerequisites

1. Use a disposable restore first. Preserve the NEXUS database, encryption/signing keys, trusted public keyring, runtime volumes and native credentials by their respective owners. The private test backup is **not** a backup of a deployment.
2. Stop intake/starts and let the original worker finish or reconcile armed sends, leases, late stops and signing obligations. Do not use old and new workers concurrently to migrate live queues. An old reader can read the additive schema; an old dispatcher ignores profile/receipt barriers. **Read compatibility is not safe worker downgrade.**
3. Apply only checksum-tracked migrations; `0044_execution_profiles.sql` adds a private RLS table. On `verify_schema` error stop, do not drop/recreate tables. Keep one authority for grants (Python).
4. Test readback, permission refusals and a disposable authorized task; use real model output, remote branch head and independent QA/signatures before considering rollout. Stop on unresolved receipts, key-binding drift or untrusted evidence. A rollback for ongoing work must keep the new reconciliation semantics or drain first; restore to a separate database before any data recovery decision.
