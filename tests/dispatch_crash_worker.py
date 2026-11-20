"""Test-only subprocess fault injection. Never import in the worker.

Run by test_dispatch_durability with a private DSN and local socket services.
No production env var, signal handler or fault-injection endpoint.
"""
import os
import signal
import sys
from unittest.mock import patch
from urllib.request import OpenerDirector

from orchestration import lab_worker
from orchestration.dispatch_journal import DispatchJournal
from orchestration.nexus_evidence import EvidenceService
from orchestration.nexus_lab import LabScheduler
from orchestration.nexus_lab_store import LabStore


def die(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


def crash_at(window):
    cls = DispatchJournal
    if window == 'accepted_before_read':
        open_url = OpenerDirector.open

        def headers_only(self, req, *args, **kw):
            response = open_url(self, req, *args, **kw)
            if req.get_method() == 'POST' and req.selector == '/api/v1/app-conversations':
                die()  # provider processed the POST; response body not read
            return response
        return patch.object(OpenerDirector, 'open', headers_only)
    if window == 'id_before_receipt':
        return patch.object(cls, 'receipt', die)
    if window == 'verdict_before_signature':
        return patch.object(EvidenceService, 'record_task_result', die)
    if window == 'terminal_before_acceptance':
        return patch.object(LabScheduler, '_record_run_acceptance', die)
    target, method = {
        'before_intent_commit': (cls, 'prepare'),
        'prepared_before_send': (LabStore, 'claim_task'), 'armed_before_send': (cls, 'arm'),
        'receipt_before_bind': (cls, 'receipt'), 'bind_before_running': (cls, 'consume'),
        'signature_before_ack': (EvidenceService, 'record_task_result'),
    }[window]
    original = getattr(target, method)

    def kill_after(*args, **kwargs):
        original(*args, **kwargs)
        die()
    return patch.object(target, method, staticmethod(kill_after) if method in ('consume', 'prepare') else kill_after)


if __name__ == '__main__':
    with crash_at(sys.argv[1]):
        sys.exit(lab_worker.main(['--worker-id', 'fault', '--poll-interval', '0.04']))
