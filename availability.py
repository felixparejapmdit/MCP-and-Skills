"""Read-only installation detection with sanitized, durable removal history.

No configured command is executed. A configuration entry is distinct from an
available local launcher, and npm's on-demand package cache is not an install
requirement. History stores labels and app identifiers, never configurations.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import shutil
import threading
import tomllib
from urllib.parse import urlsplit, urlunsplit


APPS = ('claude', 'codex', 'gemini')
APP_NAMES = {'claude': 'Claude Code', 'codex': 'Codex', 'gemini': 'Gemini CLI'}
PORTABLE_SOURCES = {'skill:ecc', 'skill:ponytail', 'skill:awesome-design-md',
                    'skill:impeccable', 'skill:hallmark', 'skill:superpowers',
                    'skill:code-review', 'skill:chrome-devtools',
                    'skill:anthropic-examples', 'skill:claude-synced'}
NATIVE_SOURCES = {'skill:codex-system': ['codex'], 'skill:claude-native': ['claude'],
                  'provider:work-session': ['codex'], 'provider:gemini-builtin': ['gemini']}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _normalize(value):
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def _app(value):
    value = str(value).lower()
    return {'claude code': 'claude', 'gemini cli': 'gemini', 'work': 'codex'}.get(value, value)


def _label(value, limit=250):
    return re.sub(r'[\x00-\x1f\x7f]', '', str(value or ''))[:limit]


def _apps(values):
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {_app(value) for value in values} & set(APPS)


def _url(value):
    try:
        parts = urlsplit(str(value or ''))
        if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
            return ''
        return urlunsplit(('https', parts.hostname, parts.path, '', ''))
    except ValueError:
        return ''


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return copy.deepcopy(default)


def _load_config(path, field, *, toml=False):
    """A deleted file is authoritative; an unreadable/invalid file is not."""
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return {}, True, False
    except (OSError, UnicodeError):
        return {}, False, True
    try:
        data = tomllib.loads(raw) if toml else json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get(field, {}), dict):
            return {}, False, True
        if any(not isinstance(value, dict) for value in data.get(field, {}).values()):
            return {}, False, True
        return data, True, True
    except (ValueError, TypeError):
        return {}, False, True


def _path_is_absolute(value):
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def runtime_missing(config, *, home, which=shutil.which):
    """Inspect local launch paths without running a server or exposing args."""
    if config.get('url') or config.get('httpUrl'):
        return False
    command = config.get('command')
    if not isinstance(command, str) or not command.strip():
        return True
    command = command.strip()
    # Variable-based launchers cannot be resolved reliably without the client.
    if re.search(r'\$\{|%[^%]+%', command):
        return False
    if command.startswith('~/') or command.startswith('~\\'):
        command = str(Path(home) / command[2:])
    if _path_is_absolute(command):
        if not Path(command).is_file():
            return True
    elif not which(command):
        return True
    args = config.get('args', [])
    if not isinstance(args, list):
        return True
    base = Path(command).name.lower()
    if base in {'cmd', 'cmd.exe'} and any(str(x).lower() in {'npx', 'npx.cmd'} for x in args):
        return not (which('npx') or which('npx.cmd'))
    if base in {'node', 'node.exe', 'python', 'python.exe', 'python3', 'python3.exe', 'pythonw.exe'}:
        for arg in args:
            if not isinstance(arg, str) or arg.startswith('-'):
                continue
            if _path_is_absolute(arg) and Path(arg).suffix.lower() in {'.js', '.mjs', '.cjs', '.py'}:
                return not Path(arg).is_file()
            break
    return False


def _portable_names(home):
    root = home / '.agents/skills'
    if not root.is_dir():
        return set()
    return {path.parent.name for path in root.glob('*/SKILL.md') if not path.parent.name.startswith('.')}


def inventory_items(inventory, portable_names=()):
    """Collapse per-source copies into capability identities for comparison."""
    items = {}
    for item in inventory.get('mcp', []):
        key = 'mcp:' + str(item.get('id', item.get('name', ''))).removeprefix('plugin:ecc:')
        apps = sorted({_app(app) for app in item.get('apps', [])} & set(APPS))
        if key == 'mcp:' or not apps:
            continue
        items[key] = {'id': key, 'name': _label(item.get('name') or key[4:]), 'kind': 'mcp',
                      'sourceId': _label(item.get('sourceId') or key), 'installedApps': apps,
                      'compatibleApps': list(APPS), 'sourceUrl': ''}
    for group in inventory.get('skillGroups', []):
        if group.get('kind', 'skill') != 'skill' or group.get('state') in {'disabled', 'uninstalled'}:
            continue
        apps = {_app(app) for app in group.get('apps', [])} & set(APPS)
        source = str(group.get('sourceId', ''))
        if source.startswith('provider:'):
            continue  # Session/built-in rows are informational, not folder evidence.
        for name in group.get('names', []):
            name = _label(name)
            # The shared adapter intentionally renamed ECC's deep-research so
            # it can coexist with Anthropic's unrelated skill of the same name.
            if source == 'skill:ecc' and name == 'deep-research':
                name = 'ecc-deep-research'
            if not name or not apps:
                continue
            key = 'skill:' + name
            native = NATIVE_SOURCES.get(source)
            compatible = set(native) if native else (set(APPS) if name in portable_names or source in PORTABLE_SOURCES else set(apps))
            row = items.setdefault(key, {'id': key, 'name': name, 'kind': 'skill',
                'sourceId': _label(source), 'installedApps': [], 'compatibleApps': [], 'sourceUrl': ''})
            row['installedApps'] = sorted(set(row['installedApps']) | apps)
            row['compatibleApps'] = sorted(set(row['compatibleApps']) | compatible | apps)
    return items


class AvailabilityTracker:
    def __init__(self, home, state_dir, app_dir, labels=None, which=None):
        self.home, self.state_dir, self.app_dir = Path(home), Path(state_dir), Path(app_dir)
        self.path = self.state_dir / 'availability-history.json'
        self.labels = labels or {}
        self.which = which or shutil.which
        self.lock = threading.RLock()
        saved = _read_json(self.path)
        valid_history = isinstance(saved, dict) and isinstance(saved.get('items', []), list)
        self.history_readable = not self.path.exists() or valid_history
        self.history = self._clean_history(saved if valid_history else {})
        if not self.history and not self.path.exists():
            # Capture the previous saved inventory before startup refreshes it.
            previous = _read_json(self.app_dir / 'web/snapshot.json', {})
            for key, row in inventory_items(previous, _portable_names(self.home)).items():
                self.history[key] = self._history_row(row)
        self.persisted_fingerprint = self._fingerprint() if self.path.exists() and self.history_readable else None

    def _fingerprint(self):
        return json.dumps(self.history, sort_keys=True, ensure_ascii=True)

    @staticmethod
    def _history_row(row):
        apps = sorted(_apps(row.get('installedApps')))
        return {'id': _label(row.get('id')), 'name': _label(row.get('name')),
                'kind': row.get('kind'), 'sourceId': _label(row.get('sourceId')),
                'compatibleApps': sorted(_apps(row.get('compatibleApps'))),
                'everInstalledApps': apps, 'lastInstalledApps': apps,
                'sourceUrl': _url(row.get('sourceUrl')), 'lastSeenAt': _now()}

    def _clean_history(self, saved):
        result = {}
        for row in saved.get('items', []) if isinstance(saved, dict) else []:
            if not isinstance(row, dict) or row.get('kind') not in {'mcp', 'skill'} or not row.get('id'):
                continue
            cleaned = self._history_row(row)
            cleaned['everInstalledApps'] = sorted(_apps(row.get('everInstalledApps')))
            cleaned['lastInstalledApps'] = sorted(_apps(row.get('lastInstalledApps')))
            cleaned['lastSeenAt'] = _label(row.get('lastSeenAt'))
            result[cleaned['id']] = cleaned
        return result

    def _configs(self):
        home = self.home
        maps, valid, configured = {}, {}, {}
        for app, relative, field, toml in [
            ('claude', '.claude.json', 'mcpServers', False),
            ('codex', '.codex/config.toml', 'mcp_servers', True),
            ('gemini', '.gemini/settings.json', 'mcpServers', False),
        ]:
            data, ok, exists = _load_config(home / relative, field, toml=toml)
            maps[app], valid[app], configured[app] = dict(data.get(field, {})), ok, exists
        # Plugin values differ from MCP server values; validate their schemas.
        try:
            registry_path = home / '.claude/plugins/installed_plugins.json'
            settings_path = home / '.claude/settings.json'
            registry = json.loads(registry_path.read_text(encoding='utf-8-sig')) if registry_path.exists() else {}
            settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.exists() else {}
            if not isinstance(registry, dict) or not isinstance(registry.get('plugins', {}), dict):
                raise ValueError()
            if not isinstance(settings, dict) or not isinstance(settings.get('enabledPlugins', {}), dict):
                raise ValueError()
            for plugin_id, enabled in settings.get('enabledPlugins', {}).items():
                if not enabled:
                    continue
                entries = registry.get('plugins', {}).get(plugin_id, [])
                if not isinstance(entries, list):
                    raise ValueError()
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError()
                    if entry.get('scope', 'user') != 'user':
                        continue
                    path = Path(entry.get('installPath', '')) / '.mcp.json'
                    if not path.resolve().is_relative_to((home / '.claude/plugins').resolve()):
                        valid['claude'] = False
                        continue
                    data, ok, _ = _load_config(path, 'mcpServers')
                    valid['claude'] = valid['claude'] and ok
                    maps['claude'].update(data.get('mcpServers', {}))
        except (OSError, ValueError, TypeError, UnicodeError):
            valid['claude'] = False
        return maps, valid, configured

    def _skill_health(self):
        valid = dict.fromkeys(APPS, True)
        roots = [(self.home / '.agents/skills', ['codex', 'gemini']),
                 (self.home / '.claude/skills', ['claude']),
                 (self.home / '.gemini/skills', ['gemini']),
                 (self.home / '.codex/skills', ['codex'])]
        for root, apps in roots:
            try:
                if root.exists():
                    list(root.iterdir())
                    for entry in root.glob('*/SKILL.md'):
                        with entry.open('rb') as stream:
                            stream.read(1)
            except OSError:
                for app in apps:
                    valid[app] = False
        return valid

    def _write(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(5) + '.tmp')
        try:
            temporary.write_text(json.dumps({'format': 1, 'scannedAt': _now(),
                'items': list(self.history.values())}, ensure_ascii=True, indent=2), encoding='utf-8')
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def scan(self, manager, app='all'):
        if app not in {'all', *APPS}:
            raise ValueError('Unknown LLM application')
        with self.lock:
            return self._scan(manager, app)

    def _scan(self, manager, app):
        maps, config_valid, configured = self._configs()
        skill_valid = self._skill_health()
        valid = {key: config_valid[key] and skill_valid[key] for key in APPS}
        warnings = [APP_NAMES[key] + ' could not be scanned completely; the last complete installation evidence is retained.'
                    for key in APPS if not valid[key]]
        running = bool(manager.job and manager.job.get('status') == 'running')
        if running:
            warnings.append('An update is running; removal detection resumes after it completes.')
        if not self.history_readable:
            warnings.append('Saved detection history is unreadable; current installations are still shown.')
        groups = []
        try:
            for row in manager.skills.discover():
                groups.append({'sourceId': row.get('sourceId'), 'apps': row.get('apps', []),
                    'names': [row.get('name')], 'kind': 'skill', 'state': row.get('state')})
        except Exception:
            skill_valid = dict.fromkeys(APPS, False)
            valid = dict.fromkeys(APPS, False)
            warnings.append('Skill folders could not be read completely; previous skill evidence is retained.')
        mcp, broken = {}, {}
        for agent, servers in maps.items():
            for name, config in servers.items():
                key = str(name).removeprefix('plugin:ecc:')
                row = mcp.setdefault(key, {'id': key, 'name': self.labels.get(key, key), 'apps': [],
                                          'sourceId': 'mcp:' + key})
                row['apps'].append(agent)
                if runtime_missing(config, home=self.home, which=self.which):
                    broken.setdefault('mcp:' + key, set()).add(agent)
        current = inventory_items({'mcp': list(mcp.values()), 'skillGroups': groups}, _portable_names(self.home))
        current = manager.sanitize(current)
        # An incomplete/busy scan may add evidence but never remove previous
        # evidence. This prevents transient atomic replacements appearing removed.
        uncertain = set(APPS) if running else {key for key in APPS if not valid[key]}
        for key, previous in self.history.items():
            keep = set(previous['lastInstalledApps']) & uncertain
            if keep:
                row = current.setdefault(key, {'id': key, 'name': previous['name'], 'kind': previous['kind'],
                    'sourceId': previous['sourceId'], 'installedApps': [],
                    'compatibleApps': previous['compatibleApps'], 'sourceUrl': previous['sourceUrl']})
                row['installedApps'] = sorted(set(row['installedApps']) | keep)
                broken.setdefault(key, set()).difference_update(keep)
        authoritative = not running and all(valid.values()) and self.history_readable
        if authoritative:
            for key, row in current.items():
                previous = self.history.get(key, self._history_row(row))
                before = copy.deepcopy(previous)
                previous.update(name=row['name'], sourceId=row['sourceId'],
                    compatibleApps=sorted(set(previous['compatibleApps']) | set(row['compatibleApps'])),
                    lastInstalledApps=sorted(set(row['installedApps']) - broken.get(key, set())))
                previous['everInstalledApps'] = sorted(set(previous['everInstalledApps']) | set(previous['lastInstalledApps']))
                if previous['lastInstalledApps'] and (key not in self.history or before != previous):
                    previous['lastSeenAt'] = _now()
                self.history[key] = previous
            for key in set(self.history) - set(current):
                self.history[key]['lastInstalledApps'] = []
            self.history = manager.sanitize(self.history)
            try:
                fingerprint = self._fingerprint()
                if fingerprint != self.persisted_fingerprint:
                    self._write()
                    self.persisted_fingerprint = fingerprint
            except OSError:
                warnings.append('Installation detection succeeded, but history could not be saved for the next start.')
                authoritative = False
        items = []
        for key in sorted(set(current) | set(self.history)):
            previous = self.history.get(key, {})
            row = current.get(key, previous)
            compatible = set(row.get('compatibleApps', [])) | set(previous.get('compatibleApps', []))
            scope = compatible if app == 'all' else compatible & {app}
            if not scope:
                continue
            present = set(current.get(key, {}).get('installedApps', [])) - broken.get(key, set())
            missing = compatible - present - uncertain
            scoped_missing = scope & missing
            if app in uncertain and not present.intersection(scope):
                continue  # Unknown is not evidence of an uninstall.
            removed = bool((scoped_missing - broken.get(key, set())) & set(previous.get('everInstalledApps', [])))
            runtime_broken = bool(scope & broken.get(key, set()))
            if runtime_broken:
                reason = 'A configured local MCP launcher is missing. Its existing configuration was preserved.'
            elif removed:
                reason = 'Previously detected here; it is no longer discoverable in the latest complete scan.'
            elif scoped_missing:
                reason = 'Available in another agent, but not installed for ' + ', '.join(APP_NAMES[x] for x in sorted(scoped_missing)) + '.'
            elif uncertain.intersection(scope):
                reason = 'Using the last complete installation evidence while a new scan is unavailable.'
            else:
                reason = 'Detected in the current global installation.'
            items.append({'id': key, 'name': row['name'], 'kind': row['kind'], 'sourceId': row.get('sourceId', ''),
                'state': 'missing' if scoped_missing or runtime_broken else 'installed',
                'installedApps': sorted(present), 'missingApps': sorted(missing),
                'compatibleApps': sorted(compatible), 'removed': removed, 'runtimeMissing': runtime_broken,
                'reason': reason, 'sourceUrl': _url(row.get('sourceUrl'))})
        # Suggestions are a catalog of never-installed candidates, not removal
        # evidence. Match all aliases before including a candidate.
        candidates = _read_json(self.app_dir / 'suggestions.json', [])
        if isinstance(candidates, dict):
            candidates = candidates.get('items', [])
        existing = {'mcp': set(), 'skill': set()}
        for row in items:
            existing[row['kind']].update({_normalize(row['id'].split(':', 1)[-1]), _normalize(row['name'])})
        if not uncertain or (app != 'all' and app not in uncertain):
            for candidate in candidates if isinstance(candidates, list) else []:
                kind = candidate.get('kind')
                if kind not in existing:
                    continue
                names = {candidate.get('id'), candidate.get('name'), *candidate.get('matchNames', [])}
                if {_normalize(name) for name in names if name} & existing[kind]:
                    continue
                items.append({'id': 'catalog:' + _label(candidate.get('id')), 'name': _label(candidate.get('name')),
                    'kind': kind, 'sourceId': '', 'state': 'not-installed', 'installedApps': [],
                    'missingApps': [app] if app != 'all' else list(APPS), 'compatibleApps': list(APPS),
                    'removed': False, 'runtimeMissing': False,
                    'reason': _label(candidate.get('description') or 'Not present in the installed inventory.', 600),
                    'sourceUrl': _url(candidate.get('url'))})
        items.sort(key=lambda row: ({'missing': 0, 'not-installed': 1, 'installed': 2}[row['state']], row['name'].lower()))
        summary = {'installedCount': sum(row['state'] == 'installed' for row in items),
                   'missingCount': sum(row['state'] == 'missing' for row in items),
                   'notInstalledCount': sum(row['state'] == 'not-installed' for row in items),
                   'removedCount': sum(row['removed'] for row in items)}
        applications = []
        for agent in APPS:
            cli = bool(self.which(agent) or self.which(agent + '.cmd') or self.which(agent + '.exe'))
            has_profile = configured[agent] or (self.home / ('.' + agent) / 'skills').is_dir()
            applications.append({'id': agent, 'name': APP_NAMES[agent], 'cliInstalled': cli,
                'configured': has_profile, 'detected': cli or has_profile})
        return manager.sanitize({'selectedApp': app, 'generatedAt': _now(), 'applications': applications,
            'summary': summary, 'items': items, 'scanComplete': authoritative, 'warnings': warnings})
