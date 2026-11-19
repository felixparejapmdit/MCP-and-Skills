"""Stage immutable native MCP runtimes without modifying an agent profile."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

from platform_support import ensure_node, ensure_uv, find_npm_cli


PACKAGES = {
    'filesystem': '@modelcontextprotocol/server-filesystem',
    'memory': '@modelcontextprotocol/server-memory',
    'playwright': '@playwright/mcp',
    'postgres': '@modelcontextprotocol/server-postgres',
    'rea': 'rea-agents',
    'chrome-devtools': 'chrome-devtools-mcp',
}


def _run(args, *, env=None, timeout=600):
    result = subprocess.run([str(x) for x in args], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=timeout, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        # Package-install commands receive no user credentials. Even so, avoid
        # returning subprocess output or inherited environment values to a UI.
        raise RuntimeError('The native runtime could not be staged. Check connectivity and supported package requirements.')
    return result.stdout.strip()


class InstallRuntime:
    def __init__(self, home, state_dir):
        self.home, self.state_dir = Path(home), Path(state_dir)

    @staticmethod
    def _metadata(package, version):
        if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][\w.]+)?', version):
            raise ValueError('A pinned package version is required')
        url = 'https://registry.npmjs.org/' + package.replace('/', '%2f') + '/' + version
        request = urllib.request.Request(url, headers={'User-Agent': 'MCPandSkills/2.0'})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('Package metadata exceeded its limit')
        result = json.loads(data)
        if result.get('name') != package or result.get('version') != version:
            raise ValueError('Package metadata did not match the pinned catalog item')
        return result

    def stage(self, item, options, target, progress=None):
        key = item.get('id', '').removeprefix('mcp:')
        if target not in {'claude', 'codex', 'gemini'}:
            raise ValueError('Choose a supported target agent')
        recipe = item.get('recipe') or {}
        version = str(recipe.get('version') or item.get('version') or '')
        inputs = (options or {}).get(item.get('id'), {})
        if not isinstance(inputs, dict):
            inputs = {}
        # Accept the nested mcp form too, while keeping all inputs in memory.
        if not inputs and isinstance((options or {}).get('mcp'), dict):
            inputs = options['mcp'].get(item.get('id'), options['mcp'].get(key, {}))
        if key == 'serena':
            if not re.fullmatch(r'\d+\.\d+\.\d+', version):
                raise ValueError('A pinned Serena version is required')
            root = self.state_dir / 'runtimes/python/serena-agent' / version
            binary_dir, tool_dir = root / 'bin', root / 'tools'
            executable = binary_dir / ('serena.exe' if os.name == 'nt' else 'serena')
            if not executable.is_file():
                uv = ensure_uv(self.state_dir, progress=progress)
                env = dict(os.environ, UV_TOOL_DIR=str(tool_dir), UV_TOOL_BIN_DIR=str(binary_dir))
                _run([uv, 'tool', 'install', '--python', '3.13', 'serena-agent==' + version], env=env)
            if not executable.is_file():
                raise RuntimeError('The staged Serena launcher is missing')
            _run([executable, 'start-mcp-server', '--help'], timeout=45)
            context = {'claude': 'claude-code', 'codex': 'codex', 'gemini': 'ide'}[target]
            return {'command': str(executable), 'args': ['start-mcp-server', '--context', context,
                '--project-from-cwd', '--open-web-dashboard', 'false', '--enable-web-dashboard', 'false',
                '--enable-gui-log-window', 'false'], 'version': version, 'runtimeRoot': str(root)}
        if key not in PACKAGES or recipe.get('package', PACKAGES[key]) != PACKAGES[key]:
            raise ValueError('This catalog entry has no registered native MCP runtime')
        package = PACKAGES[key]
        metadata = self._metadata(package, version)
        node = Path(ensure_node(metadata.get('engines', {}).get('node', '>=20'), self.state_dir, progress=progress))
        npm = find_npm_cli(node)
        if not npm:
            raise RuntimeError('The managed Node installation does not contain npm')
        root = self.state_dir / 'runtimes/npm' / key / version
        package_dir = root / 'node_modules' / package
        manifest = package_dir / 'package.json'
        try:
            installed = json.loads(manifest.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            installed = {}
        if installed.get('name') != package or installed.get('version') != version:
            root.mkdir(parents=True, exist_ok=True)
            env = dict(os.environ)
            env['PATH'] = str(node.parent) + os.pathsep + env.get('PATH', '')
            _run([node, npm, 'install', '--prefix', root, '--ignore-scripts',
                  '--engine-strict', '--no-audit', '--no-fund', package + '@' + version], env=env)
            installed = json.loads(manifest.read_text(encoding='utf-8'))
        if installed.get('name') != package or installed.get('version') != version:
            raise RuntimeError('The staged MCP identity does not match its catalog entry')
        bins = installed.get('bin', {})
        relative = bins if isinstance(bins, str) else next(iter(bins.values()), None)
        if not relative:
            raise RuntimeError('This package does not expose an MCP launcher')
        entry = (package_dir / relative).resolve()
        if not entry.is_relative_to(package_dir.resolve()) or not entry.is_file():
            raise RuntimeError('The MCP package declares an unsafe launcher')
        _run([node, '--check', entry], timeout=45)
        args, env = [str(entry)], {}
        if key == 'filesystem':
            roots = inputs.get('roots', [])
            if not isinstance(roots, list) or not roots or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
                raise ValueError('Choose at least one absolute filesystem access root')
            args += ['--confine-roots', *roots]
        elif key == 'memory':
            memory = self.state_dir / 'memory/knowledge-graph.jsonl'
            memory.parent.mkdir(parents=True, exist_ok=True)
            env['MEMORY_FILE_PATH'] = str(memory)
        elif key == 'postgres':
            connection = inputs.get('connectionString')
            if not isinstance(connection, str) or not connection.startswith(('postgresql://', 'postgres://')):
                raise ValueError('Enter a PostgreSQL connection string')
            args.append(connection)
        elif key == 'rea':
            args.append('mcp')
        return {'command': str(node), 'args': args, **({'env': env} if env else {}),
                'version': version, 'runtimeRoot': str(root)}
