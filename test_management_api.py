"""Mutation contracts use explicit plans, target scopes and an in-memory token."""
import copy
import http.client
import json
import threading
import time

import server
from test_manager import IsolatedManagerCase


class FakeInstaller:
    def __init__(self):
        self.calls = []
        self.plans = {}
        self.events = []

    def catalog(self, app='all'):
        return {'items': [{'id': 'mcp:context7', 'name': 'Context7', 'kind': 'mcp',
                          'installedAgents': [], 'supportedAgents': ['claude', 'codex', 'gemini']}],
                'selectedApp': app, 'capabilities': {'sharedMigration': False}}

    def plan(self, ids, targets, operation='install', options=None):
        if not ids or not targets or set(targets) - {'claude', 'codex', 'gemini'}:
            raise ValueError('Choose explicit targets')
        identifier = 'plan_0123456789abcdef'
        self.plans[identifier] = (list(ids), list(targets), operation)
        return {'id': identifier, 'items': ids, 'targets': targets,
                'operation': operation, 'changes': [], 'dependencies': [],
                'conflicts': [], 'requiresInput': [], 'canExecute': True}

    def validate_plan(self, identifier):
        if identifier not in self.plans:
            raise ValueError('Unknown plan')

    def execute(self, identifier, progress=None):
        self.validate_plan(identifier)
        ids, targets, operation = self.plans.pop(identifier)
        self.calls.append((ids, targets, operation))
        self.events.append({'at': '2026-01-01', 'operation': operation,
                            'items': ids, 'targets': targets, 'status': 'done'})
        return {'status': 'done', 'targets': targets, 'items': ids}

    def history(self):
        return copy.deepcopy(self.events)

    def check_updates(self, progress=None, targets=None):
        return []


class FakeDocker:
    def check(self):
        return {'available': False, 'composeAvailable': False, 'running': False,
                'engineRunning': False, 'mode': 'sandbox', 'generated': False}
    def generate(self):
        return {'mode': 'sandbox', 'url': 'http://127.0.0.1:8877', 'content': 'services: {}'}
    def start(self):
        return {'running': True, 'mode': 'sandbox'}
    def stop(self):
        return {'running': False, 'mode': 'sandbox'}
    def logs(self):
        return {'logs': 'Owned sandbox output', 'mode': 'sandbox'}


class ManagementAPITests(IsolatedManagerCase):
    def setUp(self):
        super().setUp()
        self.engine = FakeInstaller()
        self.manager._install_engine = self.engine
        self.manager._docker = FakeDocker()
        self.http = server.create_server(self.manager, 0)
        self.port = self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def request(self, path, payload=None, token=True, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            headers['Origin'] = origin or f'http://127.0.0.1:{self.port}'
            if token:
                headers['X-Inventory-Token'] = self.manager.token
        conn.request('POST' if payload is not None else 'GET', path,
                     json.dumps(payload) if payload is not None else None, headers)
        response = conn.getresponse()
        status, cache, body = response.status, response.getheader('Cache-Control'), json.loads(response.read())
        conn.close()
        return status, cache, body

    def test_catalog_scope_is_allowlisted_and_not_cached(self):
        status, cache, body = self.request('/api/catalog?app=gemini')
        self.assertEqual(200, status)
        self.assertEqual('no-store', cache)
        self.assertEqual('gemini', body['selectedApp'])
        for query in ['app=unknown', 'app=claude&app=gemini', 'path=../../']:
            self.assertEqual(400, self.request('/api/catalog?' + query)[0])

    def test_selective_install_executes_only_reviewed_targets(self):
        status, _, plan = self.request('/api/install/plan',
            {'ids': ['mcp:context7'], 'targets': ['gemini'], 'operation': 'install'})
        self.assertEqual(200, status)
        self.assertEqual(['gemini'], plan['targets'])
        status, _, result = self.request('/api/install/execute', {'planId': plan['id']})
        self.assertEqual(202, status)
        for _ in range(100):
            if self.manager.operations.status('install')['job']['status'] != 'running':
                break
            time.sleep(.01)
        self.assertEqual([(['mcp:context7'], ['gemini'], 'install')], self.engine.calls)
        self.assertEqual(['gemini'], self.request('/api/install/history')[2]['items'][0]['targets'])

    def test_unknown_plan_is_rejected_before_work(self):
        self.assertEqual(400, self.request('/api/install/execute', {'planId': 'unknown_0123456789'})[0])
        self.assertEqual([], self.engine.calls)
        self.assertIsNone(self.manager.operations.jobs['install'])

    def test_install_requires_token_and_same_origin(self):
        payload = {'ids': ['mcp:context7'], 'targets': ['gemini']}
        self.assertEqual(403, self.request('/api/install/plan', payload, token=False)[0])
        self.assertEqual(403, self.request('/api/install/plan', payload, origin='https://other.example')[0])
        self.assertEqual({}, self.engine.plans)

    def test_plan_does_not_accept_arbitrary_commands_or_missing_targets(self):
        self.assertEqual(400, self.request('/api/install/plan',
            {'ids': ['mcp:context7'], 'targets': ['gemini'], 'command': 'anything'})[0])
        self.assertEqual(400, self.request('/api/install/plan', {'ids': ['mcp:context7']})[0])

    def test_container_actions_cannot_accept_arbitrary_compose_commands(self):
        self.assertFalse(self.request('/api/docker')[2]['available'])
        self.assertEqual(400, self.request('/api/docker/generate', {'mode': 'host'})[0])
        self.assertEqual(400, self.request('/api/docker/start', {'command': 'anything'})[0])
        self.assertEqual(200, self.request('/api/docker/generate', {'mode': 'sandbox'})[0])
        self.assertEqual('Owned sandbox output', self.request('/api/docker/logs')[2]['logs'])

    def test_shutdown_does_not_interrupt_an_active_install(self):
        self.manager.operations.jobs['install'] = {'status': 'running'}
        self.assertEqual(409, self.request('/api/helper/stop', {})[0])
        self.manager.operations.jobs['install'] = None

    def test_startup_does_not_touch_a_different_profile_home(self):
        self.assertFalse(self.request('/api/runtime')[2]['startup']['supported'])
        self.assertEqual(400, self.request('/api/runtime/startup', {'enabled': True})[0])
