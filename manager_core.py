"""Local inventory and conservative update orchestration. No web framework required."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

APP_DIR = Path(__file__).resolve().parent
HOME = Path.home()
DEFAULT_STATE = Path(os.environ.get('LOCALAPPDATA', HOME / 'AppData/Local')) / 'AgentInventoryManager'
VERSION = '1.0.0'


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return copy.deepcopy(default)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(5) + '.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(temp, path)


def version_tuple(value):
    match = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', str(value))
    return tuple(int(x or 0) for x in match.groups()) if match else (0, 0, 0)


def safe_text(value, known_secrets=()):
    text = str(value)
    for secret in sorted(set(known_secrets), key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, '[redacted]')
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', text)
    text = re.sub(r'(?i)([?&](?:[^\s=&#]+)=)[^\s&#]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|token|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[redacted]', text)
    text = re.sub(r'(://)[^\s/@]+:[^\s/@]+@', r'\1[redacted]@', text)
    return text


def replace_toml_assignment(body, field, value):
    """Replace a complete TOML value, including multiline arrays or strings."""
    lines = body.splitlines(keepends=True)
    for start, line in enumerate(lines):
        if not re.match(r'^[ \t]*' + re.escape(field) + r'\s*=', line):
            continue
        for end in range(start + 1, len(lines) + 1):
            try:
                parsed = tomllib.loads(''.join(lines[start:end]))
            except tomllib.TOMLDecodeError:
                continue
            if field in parsed:
                lines[start:end] = [field + ' = ' + json.dumps(value, ensure_ascii=True) + '\n']
                return ''.join(lines)
        raise RuntimeError('The existing TOML value could not be parsed')
    return field + ' = ' + json.dumps(value, ensure_ascii=True) + '\n' + body


def run(args, *, timeout=180, env=None, cwd=None):
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout,
                            env=env, cwd=cwd, creationflags=flags)
    if result.returncode:
        # Only update-package commands use this helper; never invoke mcp get or dump config.
        message = (result.stderr or result.stdout or 'Command failed').strip()
        message = re.sub(r'(?i)(bearer\s+)\S+', r'\1[redacted]', message)
        raise RuntimeError(message[-1800:])
    return result.stdout.strip()


class MetadataClient:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / 'metadata-cache.json'
        self.cache = read_json(self.path, {})

    def get(self, url, ttl=21600):
        if not (url.startswith('https://registry.npmjs.org/') or
                url.startswith('https://pypi.org/pypi/')):
            raise ValueError('Unregistered metadata host')
        saved = self.cache.get(url)
        if saved and time.time() - saved.get('time', 0) < ttl:
            return saved['value']
        request = urllib.request.Request(url, headers={'User-Agent': 'AgentInventoryManager/1.0'})
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError('Metadata response too large')
        value = json.loads(raw)
        self.cache[url] = {'time': time.time(), 'value': value}
        write_json(self.path, self.cache)
        return value


MCP_PACKAGES = {
    'filesystem': '@modelcontextprotocol/server-filesystem',
    'memory': '@modelcontextprotocol/server-memory',
    'playwright': '@playwright/mcp',
    'postgres': '@modelcontextprotocol/server-postgres',
    'rea': 'rea-agents',
    'chrome-devtools': 'chrome-devtools-mcp',
}
MCP_LABELS = {
    'filesystem': 'Filesystem Official', 'memory': 'Memory Knowledge Graph',
    'playwright': 'Playwright', 'postgres': 'PostgreSQL', 'rea': 'REA',
    'chrome-devtools': 'Chrome DevTools', 'github': 'GitHub Official',
    'context7': 'Context7', 'supabase': 'Supabase Official', 'exa': 'Exa',
    'sentry': 'Sentry', 'notion': 'Notion Official', 'figma': 'Figma',
    'serena': 'Serena', 'perplexity': 'Perplexity', 'posthog': 'PostHog',
    'composio': 'Composio', 'firecrawl': 'Firecrawl',
}


class MCPUpdater:
    def __init__(self, home, state_dir, log=None):
        self.home = Path(home)
        self.state_dir = Path(state_dir)
        self.log = log or (lambda *a: None)
        self.metadata = MetadataClient(state_dir)
        self.managed_path = self.state_dir / 'mcp-managed.json'
        self.managed = read_json(self.managed_path, {})
        self.checks = {}

    def configs(self):
        home = self.home
        claude_path = home / '.claude.json'
        codex_path = home / '.codex/config.toml'
        gemini_path = home / '.gemini/settings.json'
        claude = read_json(claude_path, {})
        try:
            codex = tomllib.loads(codex_path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            codex = {}
        gemini = read_json(gemini_path, {})
        result = [('claude', claude_path, claude, claude.get('mcpServers', {})),
                  ('codex', codex_path, codex, codex.get('mcp_servers', {})),
                  ('gemini', gemini_path, gemini, gemini.get('mcpServers', {}))]
        plugins = read_json(home / '.claude/plugins/installed_plugins.json', {}).get('plugins', {})
        enabled = read_json(home / '.claude/settings.json', {}).get('enabledPlugins', {})
        for plugin_name, entries in plugins.items():
            if not enabled.get(plugin_name, False):
                continue
            for entry in entries:
                p = Path(entry.get('installPath', '')) / '.mcp.json'
                data = read_json(p, {})
                if data.get('mcpServers'):
                    result.append(('claude-plugin', p, data, data['mcpServers']))
        return result

    def inventory(self):
        grouped = {}
        health = read_json(self.home / '.agents/install-records/current-mcp-health-2026-11-19.json', {})
        statuses = {x['name'].replace('plugin:ecc:', ''): x['status'] for x in health.get('servers', [])}
        for app, _, _, servers in self.configs():
            for name, server in servers.items():
                key = name.replace('plugin:ecc:', '')
                row = grouped.setdefault(key, {
                    'id': key, 'name': MCP_LABELS.get(key, key), 'apps': [],
                    'status': statuses.get(key, 'Configured'), 'kind': 'mcp',
                    'sourceId': 'mcp:' + key, 'description': '',
                })
                app = 'claude' if app == 'claude-plugin' else app
                if app not in row['apps']:
                    row['apps'].append(app)
                if server.get('url') or server.get('httpUrl'):
                    # Do not expose query strings, userinfo, headers, env, or launch args.
                    from urllib.parse import urlsplit, urlunsplit
                    parts = urlsplit(server.get('url') or server.get('httpUrl'))
                    row['description'] = urlunsplit((parts.scheme, parts.hostname or '', parts.path, '', ''))
                elif key in MCP_PACKAGES:
                    row['description'] = MCP_PACKAGES[key]
                elif key == 'serena':
                    row['description'] = 'serena-agent (Python tool)'
        return sorted(grouped.values(), key=lambda x: x['name'].lower())

    def installed_version(self, key):
        if key == 'serena':
            managed = self.managed.get(key)
            if managed:
                commands = [config.get('command', '') for _, _, _, servers in self.configs()
                            for name, config in servers.items() if name == key]
                root = Path(managed.get('runtimeRoot', self.state_dir / 'missing-runtime')).resolve()
                if commands and all(Path(command).resolve().is_relative_to(root) for command in commands):
                    return managed.get('version', 'Unknown')
            try:
                path = self.home / 'AppData/Roaming/uv/tools/serena-agent/Lib/site-packages'
                for meta in path.glob('serena_agent-*.dist-info/METADATA'):
                    match = re.search(r'^Version: (.+)$', meta.read_text(encoding='utf-8'), re.M)
                    if match:
                        return match.group(1)
            except OSError:
                pass
            return 'Unknown'
        package = MCP_PACKAGES[key]
        versions = []
        # Resolve direct package launchers and explicit npx pins without exposing other args.
        for _, _, _, servers in self.configs():
            for name, config in servers.items():
                if name != key:
                    continue
                for arg in config.get('args', []):
                    if isinstance(arg, str) and arg.startswith(package + '@'):
                        candidate = arg[len(package) + 1:]
                        if re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.]+)?', candidate):
                            versions.append(candidate)
                    candidate = Path(arg) if isinstance(arg, str) else None
                    if candidate and candidate.is_file() and candidate.suffix in ('.js', '.mjs', '.cjs'):
                        for parent in candidate.parents:
                            meta = read_json(parent / 'package.json', {})
                            if meta.get('name') == package:
                                versions.append(meta.get('version'))
                                break
        if not versions:
            cache = Path(os.environ.get('LOCALAPPDATA', self.home / 'AppData/Local')) / 'npm-cache/_npx'
            if cache.exists():
                for root in cache.iterdir():
                    meta = read_json(root / 'node_modules' / package / 'package.json', {})
                    if meta.get('version'):
                        versions.append(meta['version'])
        return min(versions, key=version_tuple) if versions else 'Unknown'

    def check_all(self, progress=None):
        result = []
        for row in self.inventory():
            key = row['id']
            unit = {'id': 'mcp:' + key, 'name': row['name'], 'kind': 'mcp',
                    'itemCount': 1, 'targetNames': [key], 'sourceUrl': row['description']}
            if key not in MCP_PACKAGES and key != 'serena':
                unit.update(installedVersion='Hosted', latestVersion='Provider managed',
                            status='host-managed', detail='This hosted MCP service is updated by its provider. Account authorization is retained by each client.')
            else:
                try:
                    installed = self.installed_version(key)
                    if key == 'serena':
                        meta = self.metadata.get('https://pypi.org/pypi/serena-agent/json', ttl=0)
                        latest = meta['info']['version']
                        source_url = 'https://pypi.org/project/serena-agent/'
                    else:
                        package = MCP_PACKAGES[key]
                        meta = self.metadata.get('https://registry.npmjs.org/' + package.replace('/', '%2f') + '/latest', ttl=0)
                        latest = meta['version']
                        source_url = 'https://www.npmjs.com/package/' + package
                    available = installed == 'Unknown' or version_tuple(latest) > version_tuple(installed)
                    unit.update(installedVersion=installed, latestVersion=latest,
                                status='update-available' if available else 'up-to-date',
                                sourceUrl=source_url, detail='A staged global runtime is validated and backed up before configuration changes.')
                    self.checks[key] = {'version': latest, 'metadata': meta}
                except Exception as exc:
                    unit.update(installedVersion=self.installed_version(key), latestVersion='Unavailable',
                                status='error', detail=str(exc)[:500])
            result.append(unit)
            if progress:
                progress({'message': 'Checked ' + row['name']})
        return result

    def _npm(self):
        exe = shutil.which('npm.cmd') or shutil.which('npm')
        if not exe:
            candidate = Path('C:/Program Files/nodejs/npm.cmd')
            exe = str(candidate) if candidate.is_file() else None
        if not exe:
            raise RuntimeError('npm is required to update this MCP runtime')
        return exe

    def _node(self):
        return shutil.which('node') or 'C:/Program Files/nodejs/node.exe'

    def _npm_cli(self):
        candidates = [Path(self._npm()).parent / 'node_modules/npm/bin/npm-cli.js',
                      Path(self._node()).parent / 'node_modules/npm/bin/npm-cli.js']
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise RuntimeError('The installed npm JavaScript launcher was not found')

    def _engine_matches(self, versions, engine):
        semver = self._npm_cli().parent.parent / 'node_modules/semver'
        program = ('const s=require(process.argv[1]);const r=process.argv[2];'
                   'console.log(JSON.stringify(process.argv.slice(3).filter(v=>s.satisfies(v,r))));')
        return json.loads(run([self._node(), '-e', program, str(semver), engine, *versions], timeout=30))

    @staticmethod
    def _node_download(url, limit):
        if not url.startswith('https://nodejs.org/dist/'):
            raise ValueError('Unregistered Node distribution host')
        request = urllib.request.Request(url, headers={'User-Agent': 'AgentInventoryManager/1.0'})
        with urllib.request.urlopen(request, timeout=60) as response:
            value = response.read(limit + 1)
        if len(value) > limit:
            raise RuntimeError('Node distribution exceeded the download limit')
        return value

    def _node_for_engine(self, engine, progress=None):
        current = run([self._node(), '--version'], timeout=15).lstrip('v')
        if not engine or current in self._engine_matches([current], engine):
            return self._node()
        if os.name != 'nt' or __import__('platform').machine().lower() not in ['amd64', 'x86_64']:
            raise RuntimeError('A compatible Node runtime must be installed for this platform')
        index = json.loads(self._node_download('https://nodejs.org/dist/index.json', 5 * 1024 * 1024))
        entries = [row for row in index if re.fullmatch(r'v\d+\.\d+\.\d+', str(row.get('version', '')))
                   and 'win-x64-zip' in row.get('files', [])]
        eligible = set(self._engine_matches([row['version'][1:] for row in entries], engine))
        candidates = [row for row in entries if row['version'][1:] in eligible]
        if not candidates:
            raise RuntimeError('The official Node releases do not satisfy this package requirement')
        major = version_tuple(current)[0]
        # Prefer the current major, then a supported LTS branch, before other stable releases.
        selected = max(candidates, key=lambda row: (version_tuple(row['version'])[0] == major,
                       bool(row.get('lts')), version_tuple(row['version'])))
        version = selected['version']
        root = self.state_dir / 'runtimes/node' / version
        executable = root / 'node.exe'
        if executable.is_file() and run([executable, '--version'], timeout=15) == version:
            return str(executable)
        if progress:
            progress({'message': 'Staging separate Node ' + version + ' for compatible MCP updates'})
        filename = 'node-' + version + '-win-x64.zip'
        base = 'https://nodejs.org/dist/' + version + '/'
        manifest = self._node_download(base + 'SHASUMS256.txt', 512 * 1024).decode('utf-8')
        checksums = [line.split() for line in manifest.splitlines()]
        digest = next((parts[0] for parts in checksums if len(parts) == 2 and parts[1] == filename), '')
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RuntimeError('The official Node archive checksum was not found')
        payload = self._node_download(base + filename, 150 * 1024 * 1024)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise RuntimeError('The Node archive failed checksum verification')
        prefix = filename.removesuffix('.zip')
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            seen = set()
            for item in archive.infolist():
                parts = item.filename.replace('\\', '/').split('/')
                if (not parts or parts[0] != prefix or any(part in ['.', '..'] or ':' in part for part in parts)
                        or item.filename.startswith(('/', '\\')) or item.filename in seen
                        or ((item.external_attr >> 16) & 0o170000) == 0o120000):
                    raise RuntimeError('The Node archive contains an unsafe path')
                seen.add(item.filename)
            entry = archive.getinfo(prefix + '/node.exe')
            if entry.file_size > 120 * 1024 * 1024 or entry.is_dir():
                raise RuntimeError('The Node executable exceeds its size limit')
            data = archive.read(entry)
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / ('node-' + secrets.token_hex(5) + '.exe')
        temporary.write_bytes(data)
        try:
            if run([temporary, '--version'], timeout=15) != version:
                raise RuntimeError('The staged Node version did not match its release')
            os.replace(temporary, executable)
        finally:
            temporary.unlink(missing_ok=True)
        return str(executable)

    def _runtime_tail(self, key, old):
        args = list(old.get('args', []))
        package = MCP_PACKAGES[key]
        command = Path(old.get('command', '')).name.lower()
        for i, value in enumerate(args):
            if value == package or value.startswith(package + '@'):
                if command not in ['cmd', 'cmd.exe', 'npx', 'npx.cmd', 'npx.exe']:
                    raise RuntimeError('A custom MCP wrapper is configured; keep it for review: ' + key)
                allowed_prefix = {'/c', '/d', 'npx', 'npx.cmd', '-y', '--yes'}
                if any(str(x).lower() not in allowed_prefix for x in args[:i]):
                    raise RuntimeError('Unrecognized npm launch options; keep them for review: ' + key)
                return args[i + 1:]
        if args and Path(args[0]).suffix in ('.js', '.cjs', '.mjs'):
            entry = Path(args[0]).resolve()
            if command not in ['node', 'node.exe']:
                raise RuntimeError('A custom MCP wrapper is configured; keep it for review: ' + key)
            for parent in entry.parents:
                meta = read_json(parent / 'package.json', {})
                if meta.get('name') != package:
                    continue
                bins = meta.get('bin', {})
                launchers = [bins] if isinstance(bins, str) else list(bins.values())
                if any((parent / path).resolve() == entry for path in launchers):
                    return args[1:]
                break
            raise RuntimeError('The existing launcher is customized or its package identity is unknown: ' + key)
        raise RuntimeError('Unrecognized existing launcher; keep it for review: ' + key)

    def _rewrite(self, key, command, entrypoint=None):
        backups = self.state_dir / 'backups' / ('mcp-' + key + '-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3))
        backups.mkdir(parents=True, exist_ok=False)
        plans = []
        for app, path, data, servers in self.configs():
            if key not in servers:
                continue
            raw = path.read_bytes()
            before = copy.deepcopy(servers[key])
            after = copy.deepcopy(before)
            after['command'] = command
            if entrypoint is not None:
                after['args'] = [str(entrypoint), *self._runtime_tail(key, before)]
            if app == 'codex':
                text = raw.decode('utf-8-sig')
                pattern = r'(?ms)(^\[mcp_servers\.' + re.escape(key) + r'\]\s*\n)(.*?)(?=^\[|\Z)'
                def replace(match):
                    body = match.group(2)
                    for field in ['command', 'args']:
                        if field not in after:
                            continue
                        body = replace_toml_assignment(body, field, after[field])
                    return match.group(1) + body
                updated, count = re.subn(pattern, replace, text)
                if count != 1:
                    raise RuntimeError('Could not locate Codex server section')
                parsed = tomllib.loads(updated)
                assert parsed['mcp_servers'][key] == after
                expected = copy.deepcopy(data)
                expected['mcp_servers'][key] = after
                assert parsed == expected
                payload = updated.encode('utf-8')
            else:
                data = copy.deepcopy(data)
                data['mcpServers'][key] = after
                payload = (json.dumps(data, indent=2) + '\n').encode('utf-8')
            target = backups / (app + '-' + str(len(plans)) + path.suffix)
            target.write_bytes(raw)
            plans.append((path, raw, payload))
        if not plans:
            raise RuntimeError('Server is no longer installed')
        # Compare-and-swap: do not trample a concurrent app/user configuration save.
        if any(path.read_bytes() != old for path, old, _ in plans):
            raise RuntimeError('Configuration changed during update; retry after it settles')
        written = []
        try:
            for path, old, payload in plans:
                if path.read_bytes() != old:
                    raise RuntimeError('Configuration changed concurrently')
                tmp = path.with_name(path.name + '.inventory-' + secrets.token_hex(4))
                tmp.write_bytes(payload)
                os.replace(tmp, path)
                written.append((path, old, payload))
        except Exception:
            for path, old, payload in reversed(written):
                if path.read_bytes() == payload:
                    path.write_bytes(old)
            raise
        return len(plans)

    def apply(self, unit_id, progress=None):
        key = unit_id.removeprefix('mcp:')
        if unit_id != 'mcp:' + key or (key not in MCP_PACKAGES and key != 'serena'):
            raise ValueError('This MCP source is not locally updatable')
        if key not in {r['id'] for r in self.inventory()}:
            raise ValueError('Only installed MCP servers can be updated')
        if key not in self.checks:
            self.check_all()
        check = self.checks.get(key)
        if not check:
            raise RuntimeError('No verified version metadata available')
        version = check['version']
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.]+)?', version):
            raise ValueError('Invalid package version')
        if progress:
            progress({'message': 'Staging ' + MCP_LABELS.get(key, key) + ' ' + version})
        if key == 'serena':
            root = self.state_dir / 'runtimes/python/serena-agent' / version
            tool_dir, bin_dir = root / 'tools', root / 'bin'
            env = dict(os.environ, UV_TOOL_DIR=str(tool_dir), UV_TOOL_BIN_DIR=str(bin_dir))
            run([str(Path('C:/Program Files/Python311/python.exe')), '-m', 'uv', 'tool', 'install', '--python', '3.13', 'serena-agent==' + version], env=env, timeout=600)
            command = str(bin_dir / ('serena.exe' if os.name == 'nt' else 'serena'))
            if not Path(command).is_file():
                raise RuntimeError('The staged Serena executable is missing')
            run([command, 'start-mcp-server', '--help'], timeout=45)
            configs = self._rewrite(key, command)
        else:
            package = MCP_PACKAGES[key]
            node = self._node_for_engine(check.get('metadata', {}).get('engines', {}).get('node', ''), progress)
            root = self.state_dir / 'runtimes/npm' / key / version
            root.mkdir(parents=True, exist_ok=True)
            if progress:
                progress({'message': 'Installing and validating ' + MCP_LABELS.get(key, key) + ' ' + version})
            env = dict(os.environ)
            env['PATH'] = str(Path(node).parent) + os.pathsep + env.get('PATH', '')
            run([node, self._npm_cli(), 'install', '--prefix', str(root), '--engine-strict', '--ignore-scripts',
                 '--no-audit', '--no-fund', package + '@' + version], timeout=600, env=env)
            package_dir = root / 'node_modules' / package
            meta = read_json(package_dir / 'package.json', {})
            if meta.get('name') != package or meta.get('version') != version:
                raise RuntimeError('Staged package identity did not match')
            bins = meta.get('bin')
            relative = bins if isinstance(bins, str) else next(iter((bins or {}).values()), None)
            if not relative:
                raise RuntimeError('Package does not expose a server launcher')
            entry = (package_dir / relative).resolve()
            if not entry.is_relative_to(package_dir.resolve()) or not entry.is_file():
                raise RuntimeError('Invalid package launcher')
            run([node, '--check', str(entry)], timeout=45)
            configs = self._rewrite(key, node, entry)
        self.managed[key] = {'version': version, 'installedAt': now(), 'runtimeRoot': str(root)}
        write_json(self.managed_path, self.managed)
        return {'id': unit_id, 'status': 'updated', 'version': version,
                'message': f'{MCP_LABELS.get(key, key)} updated in {configs} global configurations. Restart active clients to use the new runtime.'}


class Manager:
    def __init__(self, home=HOME, state_dir=DEFAULT_STATE, app_dir=APP_DIR):
        self.home, self.state_dir, self.app_dir = Path(home), Path(state_dir), Path(app_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.state_dir / 'settings.json'
        self.settings = {'autoUpdate': True, 'intervalHours': 24, **read_json(self.settings_path, {})}
        self.state_path = self.state_dir / 'state.json'
        self.state = read_json(self.state_path, {'updates': [], 'lastCheck': None, 'lastUpdate': None, 'activity': []})
        self.job = None
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.mcp = MCPUpdater(home, state_dir, self.log)
        self._secrets = set()
        self.refresh_secrets()
        from skill_updates import SkillUpdater
        self.skills = SkillUpdater(Path(home), Path(state_dir), self.log)

    def refresh_secrets(self):
        from urllib.parse import parse_qsl, unquote, urlsplit
        try:
            for _, _, _, servers in self.mcp.configs():
                for config in servers.values():
                    for field in ['env', 'headers', 'http_headers']:
                        for value in config.get(field, {}).values():
                            if isinstance(value, str):
                                self._secrets.add(value)
                                if value.lower().startswith('bearer '):
                                    self._secrets.add(value[7:])
                    for value in [config.get('url'), config.get('httpUrl'), *config.get('args', [])]:
                        if not isinstance(value, str) or '://' not in value:
                            continue
                        parts = urlsplit(value)
                        if parts.password:
                            self._secrets.add(unquote(parts.password))
                        for _, secret in parse_qsl(parts.query):
                            self._secrets.add(secret)
        except (OSError, ValueError, TypeError):
            pass

    def sanitize(self, value):
        if isinstance(value, str):
            return safe_text(value, self._secrets)
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.sanitize(item) for item in value]
        return value

    def persist(self):
        with self.lock:
            write_json(self.state_path, self.state)

    def log(self, message, level='info', **kwargs):
        if isinstance(message, dict):
            level = message.get('level', level)
            message = message.get('message', json.dumps(message))
        self.refresh_secrets()
        safe = self.sanitize(str(message))
        with self.lock:
            self.state.setdefault('activity', []).insert(0, {'at': now(), 'message': safe[:1500], 'level': str(level)})
            self.state['activity'] = self.state['activity'][:120]
            if self.job and self.job['status'] == 'running':
                self.job['message'] = safe[:500]
        self.persist()

    def progress(self, item=None, *args, **kwargs):
        if isinstance(item, dict):
            message = item.get('message') or item.get('detail') or ''
        else:
            message = str(item or '')
        if message:
            self.log(message)

    def inventory(self):
        mcp = self.mcp.inventory()
        rows = self.skills.discover()
        groups = {}
        for row in rows:
            source_id = row.get('sourceId') or 'skill:unmanaged'
            app_values = row.get('apps') or ['Codex', 'Gemini']
            apps = sorted({{'claude code': 'claude', 'claude': 'claude', 'codex': 'codex',
                            'gemini': 'gemini', 'gemini cli': 'gemini', 'work': 'codex'}.get(str(a).lower(), str(a).lower()) for a in app_values})
            group_key = source_id + '|' + ','.join(apps)
            group = groups.setdefault(group_key, {'id': re.sub(r'[^\w-]', '-', group_key),
                'title': row.get('sourceName') or source_id.removeprefix('skill:'),
                'state': row.get('state', 'installed'), 'apps': apps, 'names': [], 'count': 0,
                'kind': 'skill', 'sourceId': source_id})
            name = row.get('name')
            if name and name not in group['names']:
                group['names'].append(name)
        for group in groups.values():
            group['names'].sort()
            group['count'] = len(group['names'])
        baseline = read_json(self.home / '.agents/install-records/current-agent-tools-data-2026-11-19.json', {})
        # App-managed session workflows and legacy commands are informational, never transplant them.
        for old in baseline.get('skill_groups', []):
            if old.get('kind') == 'command' or old.get('id') in ['work-session', 'gemini-builtin']:
                g = copy.deepcopy(old)
                g['apps'] = ['codex' if a == 'Work' else str(a).lower() for a in g.get('apps', [])]
                g['sourceId'] = 'provider:' + g['id']
                groups[g['id']] = g
        shared = self.home / '.agents/skills'
        shared_count = sum(1 for p in shared.iterdir() if (p / 'SKILL.md').is_file()) if shared.exists() else 0
        return {'mcp': mcp, 'skillGroups': list(groups.values()), 'generatedAt': now(),
                'stats': {'mcpCount': len(mcp), 'connectedCount': sum(x['status'] == 'Connected' for x in mcp),
                          'authCount': sum(x['status'] == 'Needs authentication' for x in mcp),
                          'sharedSkills': shared_count, 'managedSources': len(self.state.get('updates', []))},
                'applications': [{'id': 'claude', 'name': 'Claude Code'}, {'id': 'codex', 'name': 'Codex'},
                                 {'id': 'gemini', 'name': 'Gemini CLI'}],
                'plugins': baseline.get('account_plugins', [])}

    def suggestions(self):
        data = read_json(self.app_dir / 'suggestions.json', [])
        if isinstance(data, dict):
            data = data.get('items', [])
        inv = self.inventory()
        normalize = lambda x: re.sub(r'[^a-z0-9]', '', str(x).lower())
        installed_mcp = {normalize(x['id']) for x in inv['mcp']} | {normalize(x['name']) for x in inv['mcp']}
        installed_skills = {normalize(name) for group in inv['skillGroups'] for name in group['names']}
        items = []
        for item in data:
            names = {normalize(item.get('name', '')), normalize(item.get('id', ''))}
            names |= {normalize(x) for x in item.get('matchNames', [])}
            existing = installed_mcp if item.get('kind') == 'mcp' else installed_skills
            if names & existing:
                continue
            items.append(item)
        return {'items': items, 'checkedAt': now()}

    def set_settings(self, payload):
        if set(payload) - {'autoUpdate', 'intervalHours'}:
            raise ValueError('Unknown setting')
        if 'autoUpdate' in payload and type(payload['autoUpdate']) is not bool:
            raise ValueError('autoUpdate must be a boolean')
        if 'intervalHours' in payload and (type(payload['intervalHours']) is not int or payload['intervalHours'] not in [6, 12, 24, 48, 168]):
            raise ValueError('Unsupported update interval')
        with self.lock:
            self.settings.update(payload)
            write_json(self.settings_path, self.settings)
        self.log('Automatic update settings changed.')
        return self.settings

    def start_job(self, kind='check', ids=None):
        if kind not in ('check', 'update'):
            raise ValueError('Unknown job')
        if ids is not None and (not isinstance(ids, list) or not ids or len(ids) > 1000 or any(not isinstance(x, str) for x in ids)):
            raise ValueError('Invalid update IDs')
        known = {x['id'] for x in self.state.get('updates', [])}
        if ids and (not known or set(ids) - known):
            raise ValueError('Only checked installed sources can be updated')
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise RuntimeError('An update job is already running')
            self.job = {'id': secrets.token_hex(8), 'type': kind, 'status': 'running',
                        'progress': 0, 'message': 'Starting checks', 'startedAt': now()}
        threading.Thread(target=self._worker, args=(kind, ids), daemon=True).start()
        return self.job

    def _worker(self, kind, ids):
        try:
            self.refresh_secrets()
            self.log('Checking installed MCP and skill source versions.')
            units = self.mcp.check_all(self.progress)
            with self.lock:
                self.job['progress'] = 25
            units += self.skills.check_all(self.progress)
            units = self.sanitize(units)
            with self.lock:
                self.state['updates'] = units
                self.state['lastCheck'] = now()
                self.job['progress'] = 55
            self.persist()
            automatic = kind == 'check' and self.settings['autoUpdate']
            apply_now = kind == 'update' or automatic
            candidates = [x for x in units if x.get('status') == 'update-available' and (not ids or x['id'] in ids)]
            changed = False
            if apply_now:
                for i, unit in enumerate(candidates):
                    if automatic and not self.settings['autoUpdate']:
                        self.log('Automatic updates paused; the current completed replacement was retained.')
                        break
                    try:
                        provider = self.mcp if unit['id'].startswith('mcp:') else self.skills
                        result = provider.apply(unit['id'], self.progress)
                        previous_version = unit.get('installedVersion')
                        if result.get('installedVersion'):
                            unit['installedVersion'] = result['installedVersion']
                        if result.get('status') in ['local-changes', 'error', 'partial']:
                            unit['status'] = result['status']
                            unit['detail'] = result.get('message') or result.get('detail', 'Review required')
                            changed = changed or (result.get('status') == 'partial' and unit.get('installedVersion') != previous_version)
                        elif result.get('status') in ['host-managed', 'up-to-date']:
                            unit['status'] = result['status']
                            unit['detail'] = result.get('detail', 'The source did not need a replacement.')
                        else:
                            unit['status'] = 'updated'
                            unit['installedVersion'] = unit.get('latestVersion', unit.get('installedVersion'))
                            unit['detail'] = result.get('message') or result.get('detail', 'Updated with a backup.')
                            changed = True
                        self.log(unit['name'] + ': ' + unit['detail'])
                    except Exception as exc:
                        unit.update(status='error', detail=self.sanitize(str(exc))[:1000])
                        self.log(unit['name'] + ': ' + str(exc), 'error')
                    with self.lock:
                        self.job['progress'] = 55 + int(40 * (i + 1) / max(len(candidates), 1))
                    self.persist()
            if changed:
                self.state['lastUpdate'] = now()
            errors = sum(x.get('status') == 'error' for x in units)
            message = ('Completed. ' + str(len(candidates)) + ' update sources processed.' if apply_now else
                       'Version checks completed; automatic updates are paused.')
            if errors:
                message += f' {errors} sources need attention; their details are listed.'
            with self.lock:
                self.job.update(status='done', progress=100, message=message, finishedAt=now())
            self.log(message, 'warning' if errors else 'info')
            self.export_snapshot()
        except Exception as exc:
            with self.lock:
                self.job.update(status='error', error=self.sanitize(str(exc))[:1200], message='The job could not finish.', finishedAt=now())
            self.log(str(exc), 'error')

    def export_snapshot(self):
        inv = self.sanitize(self.inventory())
        web = self.app_dir / 'web'
        web.mkdir(exist_ok=True)
        write_json(web / 'snapshot.json', inv)
        text = 'window.INVENTORY_FALLBACK=' + json.dumps(inv, ensure_ascii=True).replace('</', '<\\/') + ';\n'
        (web / 'snapshot.js').write_text(text, encoding='utf-8')
        index = web / 'index.html'
        css, script = web / 'styles.css', web / 'app.js'
        if index.is_file() and css.is_file() and script.is_file():
            standalone = index.read_text(encoding='utf-8')
            standalone = standalone.replace('<link rel="stylesheet" href="styles.css">',
                '<style>' + css.read_text(encoding='utf-8') + '</style>')
            standalone = standalone.replace('<!-- INVENTORY_BOOTSTRAP -->',
                '<script>' + text + '</script>')
            standalone = standalone.replace('<script src="app.js" defer></script>',
                '<script>' + script.read_text(encoding='utf-8').replace('</script', '<\\/script') + '</script>')
            output = self.app_dir / 'index.html'
            output.write_text(standalone, encoding='utf-8')
            # Keep the user's original Desktop entrypoint, with its original saved once.
            desktop = self.home / 'Desktop'
            if self.app_dir.resolve() == (desktop / 'MCP and Agent Skills').resolve():
                destination = desktop / 'MCP and Agent Skills.html'
                backup = self.state_dir / 'backups/original-desktop-inventory.html'
                if destination.is_file() and not backup.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup)
                temp = destination.with_suffix('.html.tmp')
                temp.write_text(standalone, encoding='utf-8')
                os.replace(temp, destination)

    def status(self):
        with self.lock:
            return {'job': copy.deepcopy(self.job), 'settings': dict(self.settings),
                    'lastCheck': self.state.get('lastCheck'), 'lastUpdate': self.state.get('lastUpdate'),
                    'helperVersion': VERSION}

    def scheduler(self, stop_event):
        if stop_event.wait(8):
            return
        while not stop_event.is_set():
            last = self.state.get('lastCheck')
            try:
                age = time.time() - datetime.fromisoformat(last).timestamp() if last else float('inf')
            except (ValueError, TypeError):
                age = float('inf')
            running = self.job and self.job['status'] == 'running'
            if self.settings['autoUpdate'] and not running and age >= self.settings['intervalHours'] * 3600:
                try:
                    self.start_job('check')
                except RuntimeError:
                    pass
            stop_event.wait(60)
