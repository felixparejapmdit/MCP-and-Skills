"""Guided Docker sandbox; it never mounts or changes the host agent profiles."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

IMAGE = 'mcp-and-skills-sandbox:2.0.1'
SOURCE = 'https://github.com/felixparejapmdit/MCP-and-Skills.git#v2.0.1'
DOCKER_URLS = {
    'Windows': 'https://docs.docker.com/desktop/setup/install/windows-install/',
    'Darwin': 'https://docs.docker.com/desktop/setup/install/mac-install/',
    'Linux': 'https://docs.docker.com/engine/install/',
}


def _safe(value):
    value = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', str(value))
    value = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', value)
    value = re.sub(r'(?i)((?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', value)
    value = re.sub(r'(://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', value)
    return value[-12000:]


class DockerSupport:
    def __init__(self, state_dir, app_dir):
        self.state_dir, self.app_dir = Path(state_dir).resolve(), Path(app_dir).resolve()
        self.directory = self.state_dir / 'docker'
        self.compose = self.directory / 'compose.yaml'
        self.ownership = self.directory / 'ownership.json'
        self.project = 'mcp-skills-' + hashlib.sha256(str(self.state_dir).encode()).hexdigest()[:10]

    def _docker(self):
        value = shutil.which('docker')
        if not value or Path(value).suffix.lower() in {'.cmd', '.bat'}:
            return None
        return value

    def _call(self, args, timeout=30, required=True):
        command = self._docker()
        if not command:
            if required:
                raise RuntimeError('Docker is not installed. Install Docker using the official link, then check again.')
            return None
        try:
            result = subprocess.run([command, *args], stdin=subprocess.DEVNULL, capture_output=True,
                text=True, encoding='utf-8', errors='replace', timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if required:
                raise RuntimeError('Docker did not respond. Start Docker Desktop or the Docker engine, then retry.') from exc
            return None
        if result.returncode and required:
            raise RuntimeError(_safe(result.stderr or result.stdout or 'Docker could not complete the sandbox action.'))
        return result

    def check(self):
        available = bool(self._docker())
        compose = self._call(['compose', 'version', '--short'], required=False) if available else None
        engine = self._call(['version', '--format', '{{.Server.Version}}'], required=False) if available else None
        engine_running = bool(engine and engine.returncode == 0)
        compose_available = bool(compose and compose.returncode == 0)
        sandbox_running, valid = False, not self.compose.exists()
        if self.compose.is_file():
            try:
                owned = self._owned()
                valid = True
                if engine_running and compose_available:
                    result = self._call([*owned, 'ps', '--format', 'json'], required=False)
                    if result and result.returncode == 0 and result.stdout.strip():
                        raw = result.stdout.strip()
                        rows = json.loads(raw) if raw.startswith('[') else [json.loads(line) for line in raw.splitlines() if line.strip()]
                        if isinstance(rows, dict):
                            rows = [rows]
                        sandbox_running = any(str(row.get('State', '')).lower() == 'running' for row in rows)
            except (RuntimeError, ValueError, TypeError):
                valid = False
        result = {'available': available, 'composeAvailable': compose_available, 'engineRunning': engine_running,
            'running': sandbox_running, 'configurationValid': valid,
            'mode': 'sandbox', 'hostManagement': False, 'url': 'http://127.0.0.1:8877',
            'installUrl': DOCKER_URLS.get(platform.system(), DOCKER_URLS['Linux']),
            'generated': self.compose.is_file(),
            'message': ('The owned Docker sandbox is running separately from this computer.' if sandbox_running else
                        'Docker is ready. Start the optional sandbox to use isolated agent profiles.'
                        if engine_running and compose_available else
                        'Docker is installed but not running, or Docker Compose is unavailable.'
                        if available else 'Install Docker to use the optional developer sandbox. Native mode does not require it.')}
        if engine_running:
            result['engineVersion'] = _safe(engine.stdout.strip())[:100]
        if compose_available:
            result['composeVersion'] = _safe(compose.stdout.strip())[:100]
        return result

    def _content(self):
        return (
            '# Generated MCP & Skills developer sandbox. No host profile mounts.\n'
            'services:\n'
            '  dashboard:\n'
            f'    image: {IMAGE}\n'
            '    build:\n'
            f'      context: {SOURCE}\n'
            '    pull_policy: never\n'
            '    init: true\n'
            '    ports:\n'
            '      - "127.0.0.1:8877:8766"\n'
            '    environment:\n'
            '      MCP_SKILLS_SANDBOX: "1"\n'
            '    volumes:\n'
            '      - sandbox-data:/data\n'
            '    restart: unless-stopped\n'
            '    security_opt:\n'
            '      - no-new-privileges:true\n'
            '    cap_drop:\n'
            '      - ALL\n'
            'volumes:\n'
            '  sandbox-data:\n')

    def generate(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        content = self._content()
        self.compose.write_bytes(content.encode('utf-8'))
        self.ownership.write_text(json.dumps({'project': self.project,
            'sha256': hashlib.sha256(content.encode()).hexdigest(), 'mode': 'sandbox'}, indent=2) + '\n', encoding='utf-8')
        return {'path': str(self.compose), 'content': content, 'mode': 'sandbox',
                'url': 'http://127.0.0.1:8877', 'project': self.project,
                'message': 'Sandbox configuration generated. It uses a separate Docker volume and cannot change host agent profiles.'}

    def _owned(self):
        try:
            record = json.loads(self.ownership.read_text(encoding='utf-8'))
            digest = hashlib.sha256(self.compose.read_bytes()).hexdigest()
        except (OSError, ValueError):
            raise RuntimeError('Generate the managed Docker configuration first.') from None
        if (record.get('project') != self.project or record.get('mode') != 'sandbox' or
            record.get('sha256') != digest or not self.compose.resolve().is_relative_to(self.directory)):
            raise RuntimeError('The managed Docker configuration was changed. Generate a fresh configuration before using these controls.')
        return ['compose', '--project-name', self.project, '--file', str(self.compose)]

    def start(self):
        if not self.compose.is_file():
            self.generate()
        args = self._owned()
        status = self.check()
        if not status['available'] or not status['composeAvailable'] or not status['engineRunning']:
            raise RuntimeError(status['message'])
        result = self._call([*args, 'up', '-d', '--build'], timeout=600)
        return {'mode': 'sandbox', 'url': 'http://127.0.0.1:8877', 'running': True,
                'message': 'Developer sandbox started. Host Claude, Codex, and Gemini installations were not modified.',
                'output': _safe(result.stdout)}

    def stop(self):
        self._call([*self._owned(), 'down'], timeout=120)
        return {'mode': 'sandbox', 'running': False, 'message': 'Developer sandbox stopped. Its saved data volume was retained.'}

    def logs(self):
        result = self._call([*self._owned(), 'logs', '--no-color', '--tail', '150'], timeout=30)
        return {'mode': 'sandbox', 'logs': _safe(result.stdout + '\n' + result.stderr)}
