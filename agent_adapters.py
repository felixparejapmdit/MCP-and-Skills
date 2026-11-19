"""Agent-specific user scopes. Shared discovery directories are never written."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib

from catalog import AGENTS, NAME


class ConfigConflict(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def binding_hash(config):
    # Auth headers, custom environment variables, and unrelated settings remain
    # client-owned. Launch arguments may contain secrets: hash, never serialize.
    fields = {k: config[k] for k in ['command', 'args', 'url', 'httpUrl', 'type', 'transport'] if k in config}
    return digest(json.dumps(fields, sort_keys=True, ensure_ascii=True).encode())


def is_link(path):
    if path.is_symlink():
        return True
    try:
        return bool(path.lstat().st_file_attributes & 0x400)
    except (AttributeError, FileNotFoundError):
        return False


def tree_hashes(root):
    root = Path(root)
    if is_link(root):
        raise ConfigConflict('A linked skill is externally managed and was preserved.')
    result = {}
    if not root.exists():
        return result
    for directory, names, files in os.walk(root, followlinks=False):
        folder = Path(directory)
        if any(is_link(folder / name) for name in names):
            raise ConfigConflict('A skill contains a directory link; it was preserved.')
        for name in files:
            path = folder / name
            if is_link(path):
                raise ConfigConflict('A skill contains a file link; it was preserved.')
            result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return result


def _toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(_toml_value(x) for x in value) + ']'
    raise ConfigConflict('An MCP setting cannot be represented in TOML.')


def _toml_key(value):
    return value if re.fullmatch(r'[A-Za-z0-9_-]+', value) else json.dumps(value)


def _toml_table(path, values):
    text = '[' + '.'.join(_toml_key(x) for x in path) + ']\n'
    children = []
    for key, value in values.items():
        if isinstance(value, dict):
            children.append((key, value))
        else:
            text += _toml_key(key) + ' = ' + _toml_value(value) + '\n'
    for key, value in children:
        text += '\n' + _toml_table([*path, key], value)
    return text


def _header_path(line):
    match = re.match(r'^\s*(\[[^\r\n]+\])\s*(?:#.*)?$', line)
    if not match or match[1].startswith('[['):
        return None
    try:
        value = tomllib.loads(match[1] + '\n__mcp_probe__ = true\n')
        parts = []
        while isinstance(value, dict) and '__mcp_probe__' not in value and len(value) == 1:
            key, value = next(iter(value.items()))
            parts.append(key)
        return parts if isinstance(value, dict) and '__mcp_probe__' in value else None
    except tomllib.TOMLDecodeError:
        return None


def _assignment(body, field, value):
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
                lines[start:end] = [field + ' = ' + _toml_value(value) + '\n']
                return ''.join(lines)
        raise ConfigConflict('An existing multiline TOML setting could not be read.')
    return field + ' = ' + _toml_value(value) + '\n' + body


class AgentAdapter:
    def __init__(self, home, agent, environment=None):
        if agent not in AGENTS:
            raise ValueError('Unknown agent target')
        self.home, self.agent = Path(home).resolve(), agent
        # Tests and explicit imported profiles stay isolated from process homes.
        env = environment if environment is not None else (os.environ if self.home == Path.home().resolve() else {})
        if agent == 'claude':
            self.profile = Path(env.get('CLAUDE_CONFIG_DIR', self.home / '.claude'))
            self.config_path = self.profile / '.claude.json' if env.get('CLAUDE_CONFIG_DIR') else self.home / '.claude.json'
        elif agent == 'codex':
            self.profile = Path(env.get('CODEX_HOME', self.home / '.codex'))
            self.config_path = self.profile / 'config.toml'
        else:
            self.profile = self.home / '.gemini'
            self.config_path = self.profile / 'settings.json'
        if not self.profile.resolve().is_relative_to(self.home):
            raise ConfigConflict('A custom agent profile outside the selected home requires manual review.')
        for parent in [self.profile, *self.profile.parents]:
            if parent == self.home.parent:
                break
            if is_link(parent):
                raise ConfigConflict('A linked agent profile requires manual review.')
        self.skill_root = self.profile / 'skills'
        self.field = 'mcp_servers' if agent == 'codex' else 'mcpServers'

    def skill_path(self, name):
        if not NAME.fullmatch(name):
            raise ValueError('Invalid skill name')
        root = self.skill_root
        for parent in [root, *root.parents]:
            if parent == self.home.parent:
                break
            if parent.exists() and is_link(parent):
                raise ConfigConflict('A linked agent skill root requires manual review.')
        return root / name

    def read(self):
        if is_link(self.config_path):
            raise ConfigConflict('A linked agent configuration requires manual review.')
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            return None, {}, {}
        try:
            data = tomllib.loads(raw.decode('utf-8-sig')) if self.agent == 'codex' else json.loads(raw.decode('utf-8-sig'))
            servers = data.get(self.field, {})
            if not isinstance(data, dict) or not isinstance(servers, dict) or any(not isinstance(x, dict) for x in servers.values()):
                raise ValueError()
            return raw, data, servers
        except (ValueError, UnicodeError, TypeError, AttributeError):
            raise ConfigConflict('The agent configuration is unreadable; it was preserved.') from None

    def payload(self, changes):
        """Return validated replacement bytes without modifying any files."""
        raw, original, servers = self.read()
        after = copy.deepcopy(original)
        after_servers = after.setdefault(self.field, {})
        for name, config in changes.items():
            if not NAME.fullmatch(name):
                raise ValueError('Invalid MCP identity')
            if config is None:
                after_servers.pop(name, None)
            else:
                after_servers[name] = copy.deepcopy(config)
        if self.agent != 'codex':
            return raw, (json.dumps(after, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
        text = (raw or b'').decode('utf-8-sig')
        for name, config in changes.items():
            if name not in servers:
                if config is not None:
                    text = text.rstrip() + '\n\n' + _toml_table(['mcp_servers', name], config)
                continue
            lines, blocks = text.splitlines(keepends=True), []
            start, path = 0, None
            for index, line in enumerate(lines):
                header = _header_path(line)
                if header is not None:
                    blocks.append((path, ''.join(lines[start:index])))
                    start, path = index, header
            blocks.append((path, ''.join(lines[start:])))
            wanted = ['mcp_servers', name]
            if not any(path == wanted for path, _ in blocks):
                raise ConfigConflict('The existing Codex MCP layout requires manual review.')
            rebuilt = []
            for path, block in blocks:
                owned = path is not None and path[:2] == wanted
                if not owned:
                    rebuilt.append(block)
                elif config is not None:
                    if path == wanted:
                        header, body = block.split('\n', 1)
                        before = servers[name]
                        for field, value in config.items():
                            if not isinstance(value, dict) and before.get(field) != value:
                                body = _assignment(body, field, value)
                        if set(before) - set(config):
                            raise ConfigConflict('Removing an owned setting requires manual review.')
                        rebuilt.append(header + '\n' + body)
                    else:
                        rebuilt.append(block)
            text = ''.join(rebuilt)
        # Removing the final child table also removes TOML's implicit parent.
        # Preserve the explicitly modelled empty MCP map for parse equality.
        if not after_servers and self.field in after and self.field not in tomllib.loads(text):
            text = text.rstrip() + '\n\n[mcp_servers]\n'
        try:
            if tomllib.loads(text) != after:
                raise ValueError()
        except (ValueError, tomllib.TOMLDecodeError):
            raise ConfigConflict('The Codex configuration could not be changed safely.') from None
        return raw, text.encode('utf-8')

    def display_config(self):
        try:
            return '~/' + self.config_path.relative_to(self.home).as_posix()
        except ValueError:
            return 'Selected agent user configuration'
