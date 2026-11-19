"""Verify a native bundle or a new container using its actual HTTP API."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request


def check(base):
    def request(path, payload=None):
        headers = {'Origin': base}
        if payload is not None:
            headers.update({'Content-Type': 'application/json', 'X-Inventory-Token': token})
        data = None if payload is None else json.dumps(payload).encode()
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=30) as response:
            return json.loads(response.read())
    health = request('/health')
    assert health['version'] == '2.0.1', health
    token = request('/api/session')['token']
    catalog = request('/api/catalog')
    assert len(catalog['items']) == 355
    assert not any(x['installedAgents'] for x in catalog['items']), 'Smoke checks require an empty, isolated profile'
    for operation in ['install', 'uninstall']:
        plan = request('/api/install/plan', {'ids': ['mcp:context7'], 'targets': ['gemini'], 'operation': operation})
        assert plan['canExecute'], plan
        request('/api/install/execute', {'planId': plan['id']})
        for _ in range(120):
            job = request('/api/install/status')['job']
            if job['status'] != 'running':
                break
            time.sleep(.25)
        assert job['status'] == 'done', job
        row = next(x for x in request('/api/catalog')['items'] if x['id'] == 'mcp:context7')
        assert row['installedAgents'] == (['gemini'] if operation == 'install' else []), row
    assert len(request('/api/install/history')['items']) == 2
    print('PASS: packaged API, default 355-item catalog, Gemini-only install/uninstall and history')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--binary', type=Path)
    group.add_argument('--url')
    args = parser.parse_args()
    if args.url:
        return check(args.url.rstrip('/'))
    binary = args.binary.resolve()
    with tempfile.TemporaryDirectory(prefix='mcp-native-smoke-') as tmp:
        root = Path(tmp)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        arguments = ['--state-dir', str(root / 'state'), '--home', str(root / 'home'), '--port', str(port), '--no-browser']
        output = root / 'launch.json'
        try:
            subprocess.run([str(binary), 'start', *arguments, '--no-scheduler', '--output', str(output)],
                           cwd=root, check=True, timeout=60)
            result = json.loads(output.read_text())
            assert result.get('running'), result
            check(f'http://127.0.0.1:{port}')
            assert not (root / 'home/.claude.json').exists()
            assert not (root / 'home/.codex').exists()
            assert not (root / 'home/.agents').exists()
        finally:
            subprocess.run([str(binary), 'stop', *arguments], cwd=root, timeout=30)
            assert root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())


if __name__ == '__main__':
    main()
