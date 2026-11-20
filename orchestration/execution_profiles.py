"""Durable desired OpenHands model profiles. No model key enters NEXUS.

A key_ref is the id of an OpenHands *provider connection*, not an env-var name,
URL or file. The runtime resolves its current key and Base URL at launch. A
NEXUS profile is a private, content-addressed LLM profile, never an activation
or a patch to a user's global settings. Nothing here starts a conversation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from pathlib import PurePosixPath
import shlex
import uuid
from typing import Any, Callable

from .cloud_client import CloudError, CloudRefused, CloudUnavailable
from .cloud_store import NotFound
from .secrets import SecretsError
from .net_guard import AddressRefused, check_url
from .nexus_projects import ProjectService
from .openhands_client import ApiUnavailable


class ProfileBlocked(CloudRefused):
    """A pre-create barrier failure. No conversation POST has been sent."""


class _RetryRead(Exception):
    pass


def _uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(value))
    except (AttributeError, ValueError, TypeError):
        raise ValueError('invalid profile scope or identifier') from None


def scope_key(project_id=None, task_id=None):
    if task_id is not None:
        if project_id is None:
            raise ValueError('a task scope requires its project')
        return 'task:' + _uuid(task_id)
    return 'project:' + _uuid(project_id) if project_id is not None else 'deployment'


def validate(desired: dict) -> dict:
    fields = {'provider', 'model', 'base_url', 'key_ref', 'agent_profile_id', 'connection_id', 'params'}
    if not isinstance(desired, dict) or set(desired) != fields:
        raise ValueError('supply exactly provider, model, base_url, key_ref, agent_profile_id, connection_id and params')
    provider, model = desired['provider'], desired['model']
    if not isinstance(provider, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', provider):
        raise ValueError('invalid provider identifier')
    if (not isinstance(model, str) or not re.fullmatch(r'[a-z0-9_-]+/[A-Za-z0-9._:/-]{1,160}', model)
            or not model.startswith(provider + '/')):
        raise ValueError('model must include the explicit provider prefix')
    try:
        url = check_url(desired['base_url'], resolve=False)
    except AddressRefused:
        raise ValueError('invalid provider Base URL') from None
    if not url.startswith('https://'):
        raise ValueError('provider Base URL requires HTTPS')
    key_ref = desired['key_ref']
    if not isinstance(key_ref, str) or not re.fullmatch('[0-9a-f]{32}', key_ref):
        raise ValueError('key_ref must be a runtime provider-connection id, not a credential')
    params = desired['params']
    if not isinstance(params, dict) or set(params) - {'temperature', 'top_p', 'max_output_tokens'}:
        raise ValueError('unsupported provider parameters')
    for k, v in params.items():
        if k == 'max_output_tokens':
            if type(v) is not int or not 1 <= v <= 131072:
                raise ValueError('invalid max_output_tokens')
        elif type(v) not in (int, float) or not 0 <= v <= (2 if k == 'temperature' else 1) or not math.isfinite(v):
            raise ValueError('invalid sampling parameter')
    return {**desired, 'base_url': url, 'agent_profile_id': _uuid(desired['agent_profile_id']),
            'connection_id': _uuid(desired['connection_id']) if desired['connection_id'] is not None else None}


class ExecutionProfiles:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _authorize(cur, principal, project_id, task_id, *, write, operator):
        if project_id is None:
            if not operator or task_id is not None:
                raise NotFound('profile not found')
            return
        project = ProjectService._project(cur, principal, _uuid(project_id))
        if write:
            from .nexus_policy import SCOPE_PROJECT_ADMIN
            ProjectService.require_scope(cur, principal, project_id, SCOPE_PROJECT_ADMIN)
            if project['deleted_at'] or project['archived_at']:
                raise NotFound('profile not found')
        if task_id is not None:
            cur.execute('SELECT state FROM nexus_run_tasks WHERE project_id=%s AND id=%s FOR SHARE',
                        (project_id, _uuid(task_id)))
            row = cur.fetchone()
            if not row:
                raise NotFound('task not found')
            if write and row['state'] != 'pending':
                raise ValueError('only a pending task can acquire a session override')

    def save(self, principal, desired, *, project_id=None, task_id=None, operator=False):
        scope = scope_key(project_id, task_id)
        d = validate(desired)
        with self.store._tx() as cur:
            self._authorize(cur, principal, project_id, task_id, write=True, operator=operator)
            if d['connection_id'] is not None:
                cur.execute('SELECT 1 FROM nexus_openhands_accounts WHERE id=%s AND principal=%s FOR SHARE',
                            (d['connection_id'], principal))
                if not cur.fetchone():
                    raise NotFound('connection not found')
            elif not operator:
                raise NotFound('deployment connection not available')
            cur.execute("INSERT INTO nexus_execution_profiles AS p(scope,project_id,task_id,desired) "
                        "VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT (scope) DO UPDATE SET desired=excluded.desired, "
                        "revision=p.revision+1, updated_at=now(), last_sync=NULL, observation=jsonb_build_object('state','saved') "
                        "WHERE p.desired<>excluded.desired", (scope, project_id, task_id, json.dumps(d)))
            return self._effective(cur, project_id, task_id)

    @staticmethod
    def _effective(cur, project_id=None, task_id=None):
        cur.execute('SELECT * FROM nexus_execution_profiles WHERE scope=%s OR '
                    '(project_id=%s AND (task_id IS NULL OR task_id=%s)) '
                    'ORDER BY task_id NULLS LAST, project_id NULLS LAST LIMIT 1',
                    ('deployment', project_id, task_id))
        return cur.fetchone()

    def effective(self, project_id=None, task_id=None):
        with self.store._tx() as cur:
            return self._effective(cur, project_id, task_id)

    def view(self, principal, *, project_id=None, task_id=None, operator=False, write=False):
        with self.store._tx() as cur:
            self._authorize(cur, principal, project_id, task_id, write=write, operator=operator)
            return self._effective(cur, project_id, task_id)

    def observe(self, row, state, reason=None, **detail):
        observation = {'state': state, 'reason': reason, **detail}
        with self.store._tx() as cur:
            cur.execute('UPDATE nexus_execution_profiles SET observation=%s::jsonb,last_sync=clock_timestamp() '
                        'WHERE scope=%s AND revision=%s', (json.dumps(observation), row['scope'], row['revision']))
            if not cur.rowcount:
                raise ProfileBlocked('desired revision changed during reconciliation')
        return observation

    def select(self, scope, account, transport):
        row = self.effective(*scope)
        if not row:
            if os.environ.get('NEXUS_REQUIRE_EXECUTION_PROFILE', '').lower() in ('1', 'true', 'yes'):
                raise ProfileBlocked('required execution profile not saved')
            return None  # Explicit compatibility mode; never fallback from a saved profile.
        if (row['desired']['connection_id'] or '') != (account or ''):
            self.block(row, 'profile is pinned to a different OpenHands connection')
        if transport == 'app':
            self.block(row, 'Cloud App cannot attest a per-session provider connection and Base URL')
        return row

    def checkout(self, row, scope, permit, repository, branch, send):
        """Bind an operator-staged checkout on the *selected runtime*, never an HTTP caller's path.

        This fixed read-only preflight does not clone, push, install a token, or run project code.
        A sandbox/volume is an isolation boundary supplied by the operator, not by cwd.
        """
        from .nexus_binding import AuthorizationError, authorize_run_task
        root = PurePosixPath(os.getenv('NEXUS_MANAGED_CHECKOUT_ROOT', ''))
        if (not root.is_absolute() or root == PurePosixPath('/') or '..' in root.parts or not all(scope)
                or not permit or not isinstance(branch, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,160}', branch)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository)):
            self.block(row, 'managed repository requires a scoped lab grant and staged checkout')
        project, task = map(_uuid, scope)
        permit = _uuid(permit)
        cwd = str(root / project / task)
        try:
            authorize_run_task(self.store, project_id=project, repository=repository, branch=branch)
        except AuthorizationError:
            self.block(row, 'repository or branch is no longer authorized')
        subject = (permit, task, project, repository, branch)
        where = (" WHERE i.id=%s AND i.task_id=t.id AND t.id=%s AND t.project_id=%s "
                 "AND t.repository=%s AND t.branch=%s AND t.state='dispatched' AND i.state='sending' "
                 "AND i.lease_token=t.lease_token AND i.context->>'armed_lease'=t.lease_token::text "
                 "AND t.lease_expires>clock_timestamp()")
        with self.store._tx() as cur:
            cur.execute('SELECT i.context FROM nexus_dispatch_intents i, nexus_run_tasks t' + where, subject)
            bound = cur.fetchone()
        if not bound:
            self.block(row, 'no live grant for this checkout')
        base_sha = bound['context'].get('base_sha')
        if not isinstance(base_sha, str) or not re.fullmatch('[0-9a-f]{40}', base_sha):
            self.block(row, 'remote branch head not established for this grant')
        origin = shlex.quote(f'https://github.com/{repository}.git')
        # Never emit the actual remote URL (it may contain a misconfigured credential).
        command = (f'test "$(pwd -P)" = {shlex.quote(cwd)} && test "$(git rev-parse --show-toplevel)" = "$(pwd -P)" '
                   f'&& test "$(git remote get-url --all origin)" = {origin} '
                   f'&& test "$(git remote get-url --all --push origin)" = {origin} '
                   f'&& test "$(git symbolic-ref --short HEAD)" = {shlex.quote(branch)} '
                   '&& changes=$(git -c core.fsmonitor=false status --porcelain=v1 --untracked-files=all --ignored) '
                   '&& test -z "$changes" && git rev-parse --verify HEAD')
        try:
            code, reply = send('POST', '/api/bash/execute_bash_command', {'cwd': cwd, 'command': command, 'timeout': 8})
        except (CloudError, ApiUnavailable, OSError, ValueError):
            self.block(row, 'checkout could not be established on this runtime')
        sha = reply.get('stdout') if isinstance(reply, dict) else None
        if (code != 200 or not isinstance(sha, str) or type(reply.get('exit_code')) is not int or reply['exit_code'] != 0
                or not re.fullmatch('[0-9a-f]{40}', sha.strip())
                or base_sha != sha.strip()):
            self.block(row, 'staged checkout origin, branch or HEAD not verified')
        receipt = dict(repository=repository, branch=branch, head=sha.strip(), working_dir=cwd,
                       profile_scope=row['scope'], revision=row['revision'])
        with self.store._tx() as cur:
            cur.execute('UPDATE nexus_dispatch_intents i SET context=context || %s::jsonb FROM nexus_run_tasks t' + where,
                        (json.dumps({'checkout': receipt}), *subject))
            live = cur.rowcount == 1
        if not live:
            self.block(row, 'grant changed before checkout was recorded')
        return cwd

    def session_expectation(self, row, scope, permit, cwd):
        """Commit what this launch must read back. Later profile edits must not change recovery."""
        d = row['desired']
        expected = {'model': d['model'], 'base_url': d['base_url'], 'params': d['params'], 'working_dir': cwd,
                    'scope': row['scope'], 'revision': row['revision']}
        with self.store._tx() as cur:
            effective = self._effective(cur, *scope)
            if not effective or (row['scope'], row['revision']) != (effective['scope'], effective['revision']):
                raise ProfileBlocked('profile selection changed before create')
            if permit:
                cur.execute("UPDATE nexus_dispatch_intents i SET context=i.context || %s::jsonb FROM nexus_run_tasks t "
                            "WHERE i.id=%s AND i.task_id=t.id AND t.project_id=%s AND t.id=%s AND t.state='dispatched' "
                            "AND i.state='sending' AND i.lease_token=t.lease_token AND t.lease_expires>clock_timestamp() "
                            "AND i.context->>'armed_lease'=t.lease_token::text AND NOT i.context ? 'expected_session'",
                            (json.dumps({'expected_session': expected}), _uuid(permit), *scope))
                if not cur.rowcount:
                    raise ProfileBlocked('no unused live launch grant')
        return expected

    @staticmethod
    def check_session(reply, cid, expected, tags):
        # This is AFTER the conversation POST: a rejection or a failed read is
        # ambiguous, never ProfileBlocked (which would release the send permit).
        try:
            status, data = reply
            llm = data['agent']['llm']
            ok = (status == 200 and _uuid(data['id']) == _uuid(cid)
                  and data['workspace']['working_dir'] == expected['working_dir']
                  and llm['model'] == expected['model'] and llm['base_url'] == expected['base_url']
                  and all(llm.get(k) == v for k, v in expected['params'].items())
                  and all(data['tags'].get(k) == v for k, v in (tags or {}).items()))
        except (KeyError, AttributeError, TypeError, ValueError):
            ok = False
        if not ok:
            raise CloudUnavailable('created session does not confirm the original launch receipt')

    def block(self, row, reason):
        self.observe(row, 'blocked', reason)
        raise ProfileBlocked(reason)

    def reconcile(self, row: dict, send: Callable, *, test_model=False, target='local', requested_agent=None):
        """Return only a launch source; never return the peer's response to the UI.

        Only reads retry inline. On a lost profile write ACK, the next invocation
        reads first. A lost conversation ACK is the dispatch journal's concern.
        """
        d = row['desired']
        name = 'nexus-' + hashlib.sha256(json.dumps(d, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:40]
        path = '/api/profiles/' + name
        llm = {'model': d['model'], 'provider_connection_id': d['key_ref'], 'num_retries': 0,
               'fallback_strategy': None, **d['params']}
        def call(method, url, body=None, *, allow_missing=False):
            for attempt in range(3 if method == 'GET' else 1):
                try:
                    code, data = send(method, url, body)
                    if code == 429 or 500 <= code < 600:
                        raise _RetryRead('rate limited' if code == 429 else 'runtime unavailable')
                    if allow_missing and code == 404:
                        return None
                    if code not in (200, 201) or data is None:
                        raise ProfileBlocked(f'profile API rejected {method} ({code})')
                    return data
                except (ApiUnavailable, CloudUnavailable, OSError, _RetryRead) as exc:
                    why = str(exc) if isinstance(exc, _RetryRead) else 'runtime unavailable or timed out'
                    if method != 'GET' or attempt == 2:
                        raise ProfileBlocked(why) from None
                    time.sleep(0.1 * (2 ** attempt))
                except ProfileBlocked:
                    raise
                except CloudError:
                    raise ProfileBlocked('profile API refused request') from None

        def matches(data):
            cfg = data.get('config') if isinstance(data, dict) else None
            return (isinstance(cfg, dict) and data.get('name') == name and data.get('api_key_set') is True
                    and all(cfg.get(k) == v for k, v in llm.items())
                    and all(cfg.get(k) == d['params'].get(k) for k in ('temperature', 'max_output_tokens', 'top_p'))
                    and not any(cfg.get(k) for k in ('base_url', 'api_key', 'extra_headers', 'litellm_extra_body'))
                    and cfg.get('auth_type', 'api_key') == 'api_key')

        try:
            if requested_agent and str(requested_agent) != d['agent_profile_id']:
                raise ProfileBlocked('conflicting agent profile selections')
            spec = call('GET', '/openapi.json')
            paths = spec.get('paths', {}) if isinstance(spec, dict) else {}
            required = {'/api/profiles/{name}': ('get', 'post'), '/api/llm/provider-connections': ('get',),
                        '/api/agent-profiles': ('get',), '/api/conversations': ('post',)}
            if (not isinstance(spec, dict) or (spec.get('info') or {}).get('version') != '1.60.1'
                    or any(not all(m in paths.get(p, {}) for m in methods) for p, methods in required.items())):
                raise ProfileBlocked('unsupported runtime version or profile capability')
            agents = call('GET', '/api/agent-profiles')
            if not any(a.get('id') == d['agent_profile_id'] and a.get('agent_kind') == 'openhands'
                       for a in agents.get('profiles', [])):
                raise ProfileBlocked('selected native agent profile unavailable')
            conns = call('GET', '/api/llm/provider-connections')
            links = [c for c in conns if c.get('id') == d['key_ref']]
            if len(links) != 1 or links[0].get('api_key_set') is not True:
                raise ProfileBlocked('provider credential missing on selected runtime')
            if links[0].get('provider') != d['provider'] or links[0].get('base_url') != d['base_url']:
                raise ProfileBlocked('provider connection or Base URL drift; repair at credential owner')
            previous = call('GET', path, allow_missing=True)
            drift = not matches(previous)
            if drift:
                self.observe(row, 'drift', 'managed profile missing or differs', target=target)
                call('POST', path, {'llm': llm, 'include_secrets': False})
                self.observe(row, 'applied', target=target)
                if not matches(call('GET', path)):
                    raise ProfileBlocked('managed profile readback differs')
            detail = {'target': target, 'profile': name, 'applied': drift, 'model_test': 'not tested'}
            if test_model:
                if 'post' not in paths.get('/api/profiles/{name}/validate', {}):
                    raise ProfileBlocked('runtime has no model preflight')
                reply = call('POST', path + '/validate', {'llm': llm})
                if not isinstance(reply, dict) or reply.get('valid') is not True or reply.get('error'):
                    raise ProfileBlocked('model preflight rejected (check model, key, quota or endpoint)')
                # Upstream 1.60.1 returns valid=true ALSO on timeout/transient 429.
                detail['model_test'] = 'inconclusive: preflight did not reject; execution unproven'
            self.observe(row, 'verified', **detail)
        except (ProfileBlocked, KeyError, AttributeError, TypeError, ValueError) as e:
            reason = str(e) if isinstance(e, ProfileBlocked) else 'malformed profile API response'
            self.observe(row, 'blocked', reason, target=target)
            raise ProfileBlocked(reason) from None
        return {'agent_profile_id': d['agent_profile_id'], 'agent_launch_additions': {'llm_profile_ref': name}}


def sync_existing(profiles, row, client, account_client, *, test_model=False):
    """Startup or operator sync; route using the *saved* connection, never a client URL."""
    account = row['desired']['connection_id']
    if account:
        try:
            client = account_client(account)
        except (NotFound, SecretsError, CloudError, ValueError):
            profiles.block(row, 'selected OpenHands connection unavailable')
    source = getattr(client, 'profile_source', None)
    if source is None:
        profiles.block(row, 'transport does not support verified per-session profiles')
    source(row, test_model=test_model)


def startup_sync(store, client, account_client):
    """Only the deployment default. Scoped overrides reconcile on their actual target."""
    profiles = ExecutionProfiles(store)
    row = profiles.effective()
    if row:
        try:
            sync_existing(profiles, row, client, account_client)
        except ProfileBlocked:
            # The diagnostic is durable. Do not take down reads or the polling of
            # existing conversations. Every new create goes through the barrier.
            return 'blocked'
        return 'verified'
    return 'not configured'
