"""Local install and developer-container jobs; no shell command API is exposed."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import platform
import secrets
import threading
from datetime import datetime, timezone
from urllib.parse import urlsplit


def now():
    return datetime.now(timezone.utc).isoformat()


class ManagementJobs:
    def __init__(self, manager):
        self.manager = manager
        self.jobs = {'install': None, 'docker': None}

    def status(self, kind):
        with self.manager.lock:
            return {'job': self.manager.sanitize(copy.deepcopy(self.jobs[kind]))}

    def running(self):
        return any(job and job.get('status') == 'running' for job in self.jobs.values())

    def start(self, kind, action, label):
        if kind not in self.jobs:
            raise ValueError('Unknown operation')
        with self.manager.lock:
            if self.running() or (self.manager.job and self.manager.job.get('status') == 'running'):
                raise RuntimeError('Another management operation is already running')
            job = {'id': secrets.token_hex(8), 'type': kind, 'status': 'running',
                   'progress': 0, 'message': label, 'startedAt': now()}
            self.jobs[kind] = job
        threading.Thread(target=self._worker, args=(kind, job['id'], action), daemon=True).start()
        return copy.deepcopy(job)

    def _worker(self, kind, identifier, action):
        def progress(value):
            message = value.get('message', '') if isinstance(value, dict) else str(value)
            with self.manager.lock:
                job = self.jobs[kind]
                if not job or job['id'] != identifier:
                    return
                if message:
                    job['message'] = self.manager.sanitize(message)[:500]
                if isinstance(value, dict) and isinstance(value.get('progress'), (int, float)):
                    job['progress'] = max(0, min(99, int(value['progress'])))
        try:
            result = action(progress)
            with self.manager.lock:
                self.jobs[kind].update(status='done', progress=100,
                    message='Operation completed.', result=self.manager.sanitize(result), finishedAt=now())
            self.manager.log('Completed ' + kind + ' operation.')
            if kind == 'install':
                try:
                    self.manager.export_snapshot()
                except Exception:
                    self.manager.log('Installation completed; the saved HTML could not be refreshed.', 'warning')
        except Exception as exc:
            safe = self.manager.sanitize(str(exc))[:1000]
            with self.manager.lock:
                self.jobs[kind].update(status='error', error=safe, message=safe, finishedAt=now())
            self.manager.log(kind + ': ' + safe, 'error')


def remember_private_inputs(manager, value, key=''):
    """Keep secret input values only in the process redaction set, never logs."""
    if isinstance(value, dict):
        for field, item in value.items():
            remember_private_inputs(manager, item, str(field))
    elif isinstance(value, list):
        for item in value:
            remember_private_inputs(manager, item, key)
    elif isinstance(value, str) and any(word in key.lower() for word in ['secret', 'password', 'token', 'connection', 'key']):
        manager._secrets.add(value)
        try:
            parts = urlsplit(value)
            if parts.password:
                from urllib.parse import unquote
                manager._secrets.add(unquote(parts.password))
        except ValueError:
            pass


def runtime_summary(manager):
    from platform_support import find_node, find_python
    from launcher import startup_path
    agents = manager.availability().get('applications', [])
    native = __import__('os').environ.get('MCP_SKILLS_CONTAINER') != '1'
    current_user = manager.home.resolve() == Path.home().resolve()
    startup_supported = native and current_user and platform.system() in ['Windows', 'Darwin', 'Linux']
    return {'platform': platform.system(), 'architecture': platform.machine(),
            'environment': 'container' if __import__('os').environ.get('MCP_SKILLS_CONTAINER') == '1' else 'native',
            'stateDir': str(manager.state_dir), 'exportDir': str(manager.export_dir),
            'agents': agents, 'python': {'available': bool(find_python())},
            'node': {'available': bool(find_node())},
            'startup': {'supported': startup_supported, 'enabled': startup_supported and startup_path().is_file()},
            'url': 'http://127.0.0.1:8766/'}
