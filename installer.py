"""Reviewed, selective user-scope installation with owned-only lifecycle actions.

Plans are opaque, one-use server-memory objects. Browser-visible plans contain
neither supplied secrets nor raw configurations. Downloads are pinned before
mutation; a transaction keeps private backups and compares every destination.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import threading
import time
import urllib.request
import zipfile

from agent_adapters import AgentAdapter, ConfigConflict, binding_hash, digest, is_link, tree_hashes
from catalog import AGENTS, NAME, catalog_items, load_catalog


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError, UnicodeError):
        raise ConfigConflict('Installation ownership records are unreadable; no changes were made.') from None


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(5) + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _relative(path, root):
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        raise ConfigConflict('An installation destination escaped its authorized directory.') from None


def _platform():
    return {'Windows': 'windows', 'Darwin': 'macos', 'Linux': 'linux'}.get(platform.system(), 'unsupported')


def _emit(callback, message):
    if callback:
        callback({'message': message})


def _metadata(url):
    if not (url.startswith('https://registry.npmjs.org/') or url.startswith('https://pypi.org/pypi/')):
        raise ValueError('Unknown publisher metadata endpoint')
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'MCP-and-Skills/2'}), timeout=30) as response:
        data = response.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError('Publisher metadata exceeded its limit')
    return json.loads(data)


_CODE_REVIEW = """---
name: code-review
description: Review local code changes or a pull request for concrete bugs, security defects, and regressions.
---

# Code review

Read the local project instructions and the changed code before reviewing.
Use available git and repository tools to understand the diff and its callers.
Report actionable findings with file locations, evidence, and impact. Prioritize
correctness and security. Keep an ordinary review read-only unless the user
requests fixes. Verify assumptions rather than inventing defects.

The publisher reference is in [the upstream review workflow](references/anthropic-code-review.md).
Its tool names are examples: use the actual tools provided by the current host.
"""

_DESIGN_LIBRARY = """---
name: awesome-design-md
description: Browse and apply the bundled VoltAgent DESIGN.md design references when choosing an interface direction.
---

# Awesome Design MD

Find suitable references in [the catalog](references/catalog.md), then read the
selected DESIGN.md before designing. Adapt the reference to the user's product,
accessibility requirements, responsive layouts, and available implementation.
Preserve existing design decisions unless a redesign is requested. These are
third-party analyses of public websites, not official brand specifications.
"""


class _FilteredSnapshot:
    """Library installs expose only selected skill folders to the builder."""
    def __init__(self, snapshot, folders):
        self.snapshot, self.folders = snapshot, set(folders)
        self.commit = snapshot.commit

    def __getattr__(self, name):
        return getattr(self.snapshot, name)

    def paths(self, prefix=''):
        values = self.snapshot.paths(prefix)
        if prefix == 'skills':
            return [path for path in values if len(path.split('/')) > 1 and path.split('/')[1] in self.folders]
        return values


class _ArchiveSnapshot:
    """One pinned archive avoids thousands of individual raw-file requests."""
    def __init__(self, snapshot, archive_path, network, repo):
        from skill_updates import _relative
        self.snapshot, self.commit = snapshot, snapshot.commit
        if not archive_path.exists():
            payload = network.bytes('https://github.com/' + repo + '/archive/' + self.commit + '.zip',
                                    limit=128 * 1024 * 1024)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = archive_path.with_suffix('.tmp-' + secrets.token_hex(5))
            temporary.write_bytes(payload)
            os.replace(temporary, archive_path)
        self.archive = zipfile.ZipFile(archive_path)
        self.entries = {}
        prefix = repo.split('/')[-1].lower() + '-' + self.commit
        total = 0
        for entry in self.archive.infolist():
            raw = entry.filename.replace('\\', '/')
            first, _, relative = raw.partition('/')
            if first.lower() != prefix or raw.startswith('/') or ((entry.external_attr >> 16) & 0o170000) == 0o120000:
                raise ConfigConflict('The publisher archive contains an unsafe path.')
            if entry.is_dir() or not relative:
                continue
            relative = _relative(relative)
            if relative.casefold() in self.entries:
                raise ConfigConflict('The publisher archive contains colliding filenames.')
            total += entry.file_size
            if entry.file_size > 64 * 1024 * 1024 or total > 512 * 1024 * 1024:
                raise ConfigConflict('The publisher archive exceeds its extraction limits.')
            self.entries[relative.casefold()] = entry

    def __getattr__(self, name):
        return getattr(self.snapshot, name)

    def read(self, path):
        from skill_updates import _relative
        path = _relative(path)
        entry = self.entries.get(path.casefold())
        recorded = self.snapshot.tree.get(path)
        if entry is None or recorded is None or recorded.get('mode') not in {'100644', '100755'}:
            return self.snapshot.read(path)
        data = self.archive.read(entry)
        if self.snapshot._blob(data) != recorded['sha']:
            raise ConfigConflict('An archived source file did not match its pinned Git object.')
        return data


class SkillPackages:
    """Immutable, checksum-verified source cache outside every discovery root."""
    def __init__(self, home, state_dir):
        self.home, self.state_dir = Path(home), Path(state_dir)
        self.root = self.state_dir / 'packages/skills'
        self.lock = threading.RLock()

    def latest(self, item, current=None):
        from skill_updates import SkillUpdater, _Bundle, _Target
        builder = SkillUpdater(self.home, self.state_dir / 'install-source-cache')
        metadata = {'commit': (current or {}).get('ref', item['recipe']['ref']),
                    'version': (current or {}).get('version', item['version'])}
        bundle = _Bundle(item['recipe']['sourceKey'], [_Target(item['name'], self.root / item['name'], metadata)])
        return builder._latest(bundle)

    def prepare(self, items, progress=None):
        from skill_updates import SkillUpdater, _Bundle, _Target, _Stage, _Snapshot, _LINK, _validate_tree
        result, groups = {}, {}
        for item in items:
            key = (item['recipe']['sourceKey'], item['recipe']['ref'])
            groups.setdefault(key, []).append(item)
        with self.lock:
            for (source, commit), group in groups.items():
                signature = digest('\n'.join(sorted(item['id'] for item in group)).encode())[:16]
                package_root = self.root / source / commit / signature
                marker = package_root / 'package.json'
                saved = read_json(marker)
                if saved:
                    if tree_hashes(package_root) != saved.get('files', {}) | {'package.json': digest(marker.read_bytes())}:
                        raise ConfigConflict('A cached package was modified; it was preserved for review.')
                    for item in group:
                        result[item['id']] = package_root / item['name']
                    continue
                if package_root.exists():
                    quarantine = self.state_dir / 'package-quarantine' / secrets.token_hex(10)
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    _relative(package_root, self.root)
                    os.replace(package_root, quarantine)
                builder = SkillUpdater(self.home, self.state_dir / 'install-source-cache')
                builder.shared = package_root
                bundle = _Bundle(source)
                by_name = {item['name']: item for item in group}
                for item in group:
                    recipe = item['recipe']
                    bundle.targets.append(_Target(item['name'], package_root / item['name'],
                        {'commit': commit, 'version': item['version'], 'upstreamName': recipe.get('upstreamName', item['name'])},
                        source_path=recipe['path']))
                git_snapshot = _Snapshot(builder.network, group[0]['recipe']['repo'], commit, builder.cache)
                snapshot = _ArchiveSnapshot(git_snapshot, self.state_dir / 'install-source-cache/archives' / source / (commit + '.zip'),
                                            builder.network, group[0]['recipe']['repo'])
                folders = [item['recipe']['path'].split('/')[1] for item in group if item['recipe']['path'].startswith('skills/')]
                filtered = _FilteredSnapshot(snapshot, folders) if source in {'ecc', 'ponytail'} else snapshot
                builder._snapshot = lambda _bundle, _latest, filtered=filtered: filtered
                original_stage = builder._target_stage
                def stage_target(target, stage_root, ordinal, active_bundle):
                    stage = original_stage(target, stage_root, ordinal, active_bundle)
                    if target.name == 'code-review':
                        stage.write('SKILL.md', _CODE_REVIEW.encode())
                    elif target.name == 'awesome-design-md':
                        stage.write('SKILL.md', _DESIGN_LIBRARY.encode())
                    stage.baseline['managed'] = sorted(tree_hashes(stage.path))
                    return stage
                builder._target_stage = stage_target
                original_copy = builder._copy_upstream
                builder._copy_upstream = lambda stage, upstream, preserve_adapter=False: original_copy(stage, upstream, preserve_adapter=False)
                staging = self.state_dir / 'install-staging' / secrets.token_hex(12)
                staging.mkdir(parents=True)
                latest = {'commit': commit, 'version': group[0]['version']}
                _emit(progress, 'Downloading verified ' + source + ' skill sources')
                try:
                    stages = builder._build(bundle, latest, staging, progress)
                    for stage in stages:
                        target = stage.target.path
                        _relative(target, package_root)
                        # Relative links into the immutable package support tree
                        # must stay valid after the skill is copied to an agent.
                        if stage.target.name != '@resources':
                            if source == 'superpowers':
                                from skill_updates import _insert_prefix
                                entry = stage.path / 'SKILL.md'
                                note = ('\n## Standalone installation note\n\n'
                                    'Only this skill and passive supporting documents were installed. '
                                    'Use the current host tools for named agent roles; do not assume native '
                                    'Superpowers hooks or commands are registered. Supporting workflows: '
                                    '[writing plans](references/superpowers/writing-plans.md), '
                                    '[test-driven development](references/superpowers/test-driven-development.md), '
                                    '[verification](references/superpowers/verification-before-completion.md).\n\n')
                                entry.write_text(_insert_prefix(entry.read_text(encoding='utf-8-sig'), note), encoding='utf-8')
                            for path in stage.path.rglob('*.md'):
                                logical = target / path.relative_to(stage.path)
                                def external_link(match):
                                    raw = match[1].strip().strip('<>')
                                    if re.match(r'^[A-Za-z][\w+.-]*:|^[/#]', raw):
                                        return match[0]
                                    value, marker_char, anchor = raw.partition('#')
                                    resolved = (logical.parent / value).resolve()
                                    if resolved.is_relative_to(package_root.resolve()) and not resolved.is_relative_to(target.resolve()):
                                        replacement = resolved.as_posix() + (marker_char + anchor if marker_char else '')
                                        return match[0].replace(match[1], replacement, 1)
                                    return match[0]
                                path.write_text(_LINK.sub(external_link, path.read_text(encoding='utf-8-sig')), encoding='utf-8')
                            source_json = stage.path / 'source.json'
                            metadata = read_json(source_json, {})
                            metadata.update(managedBy='mcp-and-skills', source='https://github.com/' + group[0]['recipe']['repo'],
                                name=stage.target.name, commit=commit, version=group[0]['version'])
                            write_json(source_json, metadata)
                            _validate_tree(stage.path, stage.target.name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(stage.path, target)
                    # Executable source launchers remain passive until an agent
                    # explicitly invokes them; installers never execute them.
                    if os.name != 'nt':
                        for item in group:
                            skill_root = package_root / item['name']
                            prefix = item['recipe']['path'].rstrip('/') + '/'
                            for path in skill_root.rglob('*'):
                                if path.is_file() and (snapshot.tree.get(prefix + path.relative_to(skill_root).as_posix(), {}).get('mode') == '100755'):
                                    path.chmod(0o755)
                    hashes = tree_hashes(package_root)
                    write_json(marker, {'source': source, 'ref': commit, 'items': [item['id'] for item in group], 'files': hashes})
                    for item in group:
                        result[item['id']] = package_root / item['name']
                finally:
                    snapshot.archive.close()
                    if staging.exists():
                        _relative(staging, self.state_dir / 'install-staging')
                        shutil.rmtree(staging)
        return result


class InstallEngine:
    def __init__(self, home, state_dir, app_dir, runtime=None, log=None):
        self.home, self.state_dir, self.app_dir = Path(home).resolve(), Path(state_dir).resolve(), Path(app_dir).resolve()
        self.runtime, self.log = runtime, log
        self.items = catalog_items(self.app_dir)
        self.receipt_path = self.state_dir / 'installations.json'
        self.lock = threading.RLock()
        self.receipts = read_json(self.receipt_path, {'schemaVersion': 1, 'entries': {}})
        if self.receipts.get('schemaVersion') != 1 or not isinstance(self.receipts.get('entries'), dict):
            raise ConfigConflict('Installation ownership records have an unsupported format.')
        self.packages = SkillPackages(self.home, self.state_dir)
        self._plans = {}

    def _adapter(self, target):
        return AgentAdapter(self.home, target)

    def _receipt(self, item_id, target):
        return self.receipts['entries'].get(item_id + ':' + target)

    def _save(self):
        write_json(self.receipt_path, self.receipts)

    def _presence(self, item, target, configs_cache=None):
        adapter = self._adapter(target)
        receipt = self._receipt(item['id'], target)
        if item['kind'] == 'mcp':
            if configs_cache is not None and target in configs_cache:
                configs = configs_cache[target]
            else:
                _, _, configs = adapter.read()
            value = configs.get(item['name'])
            managed = bool(receipt and value and binding_hash(value) == receipt.get('configHash'))
            if not value and target == 'claude':
                registry = read_json(self.home / '.claude/plugins/installed_plugins.json', {}).get('plugins', {})
                enabled = read_json(self.home / '.claude/settings.json', {}).get('enabledPlugins', {})
                for plugin_id, entries in registry.items():
                    if not enabled.get(plugin_id):
                        continue
                    for installed in entries:
                        folder = Path(installed.get('installPath', ''))
                        if folder.resolve().is_relative_to((self.home / '.claude/plugins').resolve()):
                            value = read_json(folder / '.mcp.json', {}).get('mcpServers', {}).get(item['name'])
                            if value:
                                break
                    if value:
                        break
            return {'state': 'installed' if value else 'not-installed', 'managed': managed, 'shared': False,
                    'conflict': bool(receipt and value and not managed)}
        path = adapter.skill_path(item['name'])
        private = (path / 'SKILL.md').is_file()
        shared = target in {'codex', 'gemini'} and (self.home / '.agents/skills' / item['name'] / 'SKILL.md').is_file()
        if private and is_link(path):
            shared = True
        managed = False
        if private and receipt and not is_link(path):
            managed = tree_hashes(path) == receipt.get('files')
        return {'state': 'installed' if private or shared else 'not-installed', 'managed': managed,
                'shared': shared, 'conflict': bool(receipt and private and not managed)}

    def catalog(self, app='all'):
        if app not in {'all', *AGENTS}:
            raise ValueError('Unknown agent')
        rows, warnings = [], []
        configs_cache = {}
        for target in AGENTS:
            try:
                configs_cache[target] = self._adapter(target).read()[2]
            except (OSError, ValueError):
                pass
        for item in self.items.values():
            if app != 'all' and app not in item['supportedAgents']:
                continue
            row = copy.deepcopy(item)
            states = {}
            for target in AGENTS:
                try:
                    states[target] = self._presence(item, target, configs_cache)
                except (OSError, ValueError):
                    states[target] = {'state': 'unknown', 'managed': False, 'shared': False,
                                      'reason': 'This agent could not be inspected safely.'}
            row.update(states=states, installedAgents=[x for x, state in states.items() if state['state'] == 'installed'],
                managedAgents=[x for x, state in states.items() if state['managed']],
                sharedAgents=[x for x, state in states.items() if state['shared']])
            rows.append(row)
        apps = []
        for target in AGENTS:
            cli = bool(shutil.which(target) or shutil.which(target + '.cmd') or shutil.which(target + '.exe'))
            try:
                adapter = self._adapter(target)
                configured = adapter.config_path.exists()
                detected = cli or configured or adapter.skill_root.exists()
            except (OSError, ValueError):
                configured, detected = False, cli
            apps.append({'id': target, 'name': {'claude':'Claude Code','codex':'Codex','gemini':'Gemini CLI'}[target],
                         'cliInstalled': cli, 'configured': configured, 'detected': detected})
        return {'catalogVersion': load_catalog(self.app_dir)['catalogVersion'], 'selectedApp': app,
                'items': rows, 'applications': apps, 'warnings': warnings, 'generatedAt': now(),
                'capabilities': {'install': True, 'uninstall': True, 'update': True, 'sharedMigration': True, 'adoptExisting': False}}

    def _latest(self, item, receipt=None, cache=None):
        item = copy.deepcopy(item)
        recipe = item['recipe']
        if recipe['type'] == 'github':
            key = ('github', recipe['sourceKey'], (receipt or {}).get('ref'), (receipt or {}).get('version'))
            if cache is not None and key in cache:
                latest = cache[key]
            else:
                latest = self.packages.latest(item, receipt)
                if cache is not None:
                    cache[key] = latest
            recipe['ref'], item['version'] = latest['commit'], latest['version']
        elif recipe['type'] == 'npm':
            key = ('npm', recipe['package'])
            value = cache.get(key) if cache is not None else None
            if value is None:
                value = _metadata('https://registry.npmjs.org/' + recipe['package'].replace('/', '%2f') + '/latest')
                if cache is not None:
                    cache[key] = value
            recipe['version'] = item['version'] = value['version']
        elif recipe['type'] == 'python':
            key = ('python', recipe['package'])
            value = cache.get(key) if cache is not None else None
            if value is None:
                value = _metadata('https://pypi.org/pypi/' + recipe['package'] + '/json')
                if cache is not None:
                    cache[key] = value
            recipe['version'] = item['version'] = value['info']['version']
        if recipe['type'] in {'npm', 'python'} and receipt:
            numeric = lambda value: tuple(map(int, re.findall(r'^\D*(\d+)\.(\d+)\.(\d+)', str(value))[0])) if re.match(r'^\D*\d+\.\d+\.\d+', str(value)) else (0, 0, 0)
            if numeric(item['version']) < numeric(receipt.get('version')):
                recipe['version'] = item['version'] = receipt['version']
        return item

    def plan(self, ids, targets, operation='install', options=None):
        if operation not in {'install', 'uninstall', 'update', 'migrate'}:
            raise ValueError('Unsupported installation operation')
        if not isinstance(ids, list) or not ids or len(ids) > 500 or any(not isinstance(x, str) or x not in self.items for x in ids):
            raise ValueError('Choose known catalog items')
        if not isinstance(targets, list) or not targets or any(x not in AGENTS for x in targets):
            raise ValueError('Choose supported agent targets')
        ids, targets = list(dict.fromkeys(ids)), list(dict.fromkeys(targets))
        if options is not None and not isinstance(options, dict):
            raise ValueError('Installation options must be an object')
        options = copy.deepcopy(options or {})
        for item_id in ids:
            if item_id not in options and isinstance(options.get('mcp'), dict):
                options[item_id] = options['mcp'].get(item_id, options['mcp'].get(item_id.split(':', 1)[-1], {}))
        private = {'id': secrets.token_urlsafe(24), 'operation': operation, 'ids': ids, 'targets': targets,
                   'items': {}, 'actions': [], 'configs': {}, 'options': options, 'created': time.time()}
        changes, conflicts, inputs, dependencies = [], [], [], set()
        resolved, source_versions = {}, {}
        with self.lock:
            for target in targets:
                try:
                    adapter = self._adapter(target)
                    raw, _, _ = adapter.read()
                    private['configs'][target] = digest(raw) if raw is not None else None
                except (ValueError, OSError):
                    conflicts.append({'target': target, 'reason': 'The agent configuration is unreadable.'})
                    continue
                for item_id in ids:
                    original = self.items[item_id]
                    item = copy.deepcopy(original)
                    if target not in item['supportedAgents'] or _platform() not in item['supportedPlatforms']:
                        conflicts.append({'id': item_id, 'target': target, 'reason': 'This item does not support the selected agent or platform.'})
                        continue
                    if operation != 'uninstall' and not item['installable'] and operation != 'migrate':
                        conflicts.append({'id': item_id, 'target': target, 'reason': item.get('reason') or 'Publisher-managed item.'})
                        continue
                    try:
                        state = self._presence(item, target)
                    except (OSError, ValueError):
                        conflicts.append({'id': item_id, 'target': target, 'reason': 'The existing installation cannot be inspected safely.'})
                        continue
                    receipt = self._receipt(item_id, target)
                    if operation == 'update' and receipt and not receipt.get('updateEligible', True):
                        conflicts.append({'id': item_id, 'target': target, 'reason': 'The migrated local source has no verified publisher binding; it was preserved.'})
                        continue
                    if operation == 'install' and state['state'] == 'installed':
                        changes.append({'id': item_id, 'name': item['name'], 'target': target, 'action': 'skip',
                                        'reason': 'Already available; existing files and shared visibility are preserved.'})
                        continue
                    if operation in {'update', 'uninstall'} and not receipt:
                        conflicts.append({'id': item_id, 'target': target, 'reason': 'This installation is not owned by this application.'})
                        continue
                    if state['conflict'] or (receipt and state['state'] == 'not-installed' and operation == 'update'):
                        conflicts.append({'id': item_id, 'target': target, 'reason': 'Owned files or launch settings changed; review before replacing them.'})
                        continue
                    if operation == 'update':
                        key = (item_id, (receipt or {}).get('version'), (receipt or {}).get('ref'))
                        if key not in resolved:
                            try:
                                resolved[key] = self._latest(item, receipt, source_versions)
                            except Exception:
                                conflicts.append({'id': item_id, 'target': target, 'reason': 'The publisher version could not be verified.'})
                                continue
                        item = copy.deepcopy(resolved[key])
                    value = options.get(item_id, {})
                    if not isinstance(value, dict):
                        raise ValueError('Per-item options must be objects')
                    if item_id == 'mcp:filesystem' and operation == 'install' and not value.get('roots'):
                        inputs.append({'id': item_id, 'key': 'roots', 'type': 'paths', 'required': True, 'secret': False,
                                       'label': 'Folders this filesystem server may access'})
                    if item_id == 'mcp:postgres' and operation == 'install' and not value.get('connectionString'):
                        inputs.append({'id': item_id, 'key': 'connectionString', 'type': 'password', 'required': True,
                                       'secret': True, 'label': 'PostgreSQL connection string'})
                    if operation in {'install', 'update'} and item['recipe'].get('type') in {'npm', 'python'}:
                        dependencies.add('Node.js/npm' if item['recipe']['type'] == 'npm' else 'Python/uv')
                    action = {'id': item_id, 'target': target, 'kind': item['kind'], 'fingerprint': None}
                    if item['kind'] == 'skill':
                        path = adapter.skill_path(item['name'])
                        action['fingerprint'] = tree_hashes(path) if path.exists() and not is_link(path) else None
                    if operation == 'migrate':
                        if item['kind'] != 'skill':
                            conflicts.append({'id': item_id, 'target': target, 'reason': 'Shared migration applies to skill folders only.'})
                            continue
                        source = self.home / '.agents/skills' / item['name']
                        if not (source / 'SKILL.md').is_file() or is_link(source):
                            conflicts.append({'id': item_id, 'target': target, 'reason': 'No ordinary shared skill folder is available to migrate.'})
                            continue
                        if path.exists() and path.resolve() != source.resolve():
                            conflicts.append({'id': item_id, 'target': target, 'reason': 'A separate agent skill already exists and was preserved.'})
                            continue
                        action['sharedFingerprint'] = tree_hashes(source)
                        action['fingerprint'] = None
                    private['items'][item_id] = item
                    private['actions'].append(action)
                    changes.append({'id': item_id, 'name': item['name'], 'target': target, 'action': operation,
                                    'location': adapter.display_config() if item['kind'] == 'mcp' else '~/' + adapter.skill_path(item['name']).relative_to(self.home).as_posix(),
                                    'version': item['version'], 'requiresAuth': item['requiresAuth']})
            migrations = []
            if operation == 'migrate':
                for item_id in ids:
                    item = self.items[item_id]
                    if item['kind'] != 'skill':
                        continue
                    source = self.home / '.agents/skills' / item['name']
                    affected = {'codex', 'gemini'}
                    for target in AGENTS:
                        path = self._adapter(target).skill_root / item['name']
                        if path.exists() and path.resolve() == source.resolve():
                            affected.add(target)
                    migrations.append({'id': item_id, 'affectedAgents': sorted(affected),
                                       'removedFromAgents': sorted(affected - set(targets)),
                                       'sharedFolderRemoved': True,
                                       'reason': 'The shared copy is backed up and removed after selected private copies are installed.'})
            inputs = list({(value['id'], value['key']): value for value in inputs}.values())
            public = {'id': private['id'], 'operation': operation, 'items': ids, 'targets': targets,
                      'changes': changes, 'conflicts': conflicts, 'requiresInput': inputs,
                      'dependencies': sorted(dependencies), 'migrations': migrations,
                      'canExecute': not conflicts and not inputs, 'createdAt': now(), 'expiresInSeconds': 1800}
            private['public'] = copy.deepcopy(public)
            self._plans[private['id']] = private
            # Bound memory, including any connection strings held only in plans.
            self._plans = {key: value for key, value in self._plans.items() if time.time() - value['created'] < 1800}
            return public

    def _mcp_config(self, item, target, options, current, progress):
        recipe = item['recipe']
        if recipe['type'] == 'http':
            return {'type': 'http', 'url': recipe['url']} if target == 'claude' else (
                {'httpUrl': recipe['url']} if target == 'gemini' else {'url': recipe['url']})
        if self.runtime is None:
            raise ConfigConflict('A compatible local runtime provider is required for this MCP.')
        options = copy.deepcopy(options)
        if item['id'] == 'mcp:postgres' and current and not options.get('connectionString'):
            options['connectionString'] = current.get('args', [''])[-1]
        if item['id'] == 'mcp:filesystem' and current and not options.get('roots'):
            args = current.get('args', [])
            options['roots'] = args[2:] if len(args) > 1 and args[1] == '--confine-roots' else args[1:]
        staged = self.runtime.stage(copy.deepcopy(item), {item['id']: options}, target, progress)
        if not isinstance(staged, dict) or not isinstance(staged.get('command'), str) or not isinstance(staged.get('args', []), list):
            raise ConfigConflict('The runtime provider returned an invalid launcher.')
        if recipe.get('version') and staged.get('version') and recipe['version'] != staged['version']:
            raise ConfigConflict('The staged runtime did not match the reviewed version.')
        value = copy.deepcopy(current or {})
        value.update(command=staged['command'], args=staged.get('args', []))
        if staged.get('env'):
            value['env'] = {**staged['env'], **value.get('env', {})}
        return value

    def execute(self, plan_id, progress=None):
        if isinstance(plan_id, dict):
            plan_id = plan_id.get('id')  # Re-read the private plan; never trust posted changes.
        with self.lock:
            plan = self._plans.pop(plan_id, None)
            if not plan or time.time() - plan['created'] > 1800:
                raise ValueError('The installation plan expired; review a new plan')
            if not plan['public']['canExecute']:
                raise ValueError('Resolve the installation plan conflicts and required inputs first')
            return self._execute(plan, progress)

    def validate_plan(self, plan_id):
        with self.lock:
            if not isinstance(plan_id, str) or plan_id not in self._plans:
                raise ValueError('The installation plan is unknown or expired')
            plan = self._plans[plan_id]
            if time.time() - plan['created'] > 1800 or not plan['public']['canExecute']:
                raise ValueError('Review an executable installation plan first')
            return copy.deepcopy(plan['public'])

    def history(self):
        values = []
        root = self.state_dir / 'install-backups'
        for path in sorted(root.glob('*/transaction.json'), reverse=True):
            try:
                value = read_json(path, {})
                values.append({'id': path.parent.name, 'operation': value.get('operation'),
                    'state': value.get('state'), 'items': value.get('items', []),
                    'targets': value.get('targets', []), 'completedAt': value.get('completedAt')})
            except (OSError, ValueError):
                continue
            if len(values) >= 100:
                break
        return values

    def _execute(self, plan, progress):
        operation = plan['operation']
        actions = plan['actions']
        skill_items = [item for item in plan['items'].values() if item['kind'] == 'skill']
        packages = self.packages.prepare(skill_items, progress) if operation in {'install', 'update'} else {}
        transaction = self.state_dir / 'install-backups' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(6))
        transaction.mkdir(parents=True)
        if os.name != 'nt':
            transaction.chmod(0o700)
        staging = self.state_dir / 'install-staging' / secrets.token_hex(12)
        staging.mkdir(parents=True)
        config_changes, new_receipts = {}, copy.deepcopy(self.receipts)
        mutations, completed = [], []
        old_receipt = self.receipt_path.read_bytes() if self.receipt_path.exists() else None
        old_receipts = copy.deepcopy(self.receipts)
        try:
            for target, fingerprint in plan['configs'].items():
                raw, _, _ = self._adapter(target).read()
                if (digest(raw) if raw is not None else None) != fingerprint:
                    raise ConfigConflict('An agent configuration changed after review; create a new plan.')
            for index, action in enumerate(actions):
                item, target = plan['items'][action['id']], action['target']
                adapter = self._adapter(target)
                key = item['id'] + ':' + target
                receipt = {'itemId': item['id'], 'target': target, 'kind': item['kind'],
                           'version': item['version'], 'sourceUrl': item['sourceUrl'],
                           'ref': item.get('recipe', {}).get('ref', ''), 'managedBy': 'mcp-and-skills',
                           'installedAt': now(), 'updateEligible': bool(item['installable'])}
                _emit(progress, operation.capitalize() + ': ' + item['name'] + ' for ' + target)
                if item['kind'] == 'mcp':
                    _, _, servers = adapter.read()
                    before = servers.get(item['name'])
                    old = self._receipt(item['id'], target)
                    if operation in {'update', 'uninstall'} and (not before or not old or binding_hash(before) != old.get('configHash')):
                        raise ConfigConflict('Owned MCP launch settings changed after review.')
                    value = None if operation == 'uninstall' else self._mcp_config(item, target, plan['options'].get(item['id'], {}), before, progress)
                    config_changes.setdefault(target, {})[item['name']] = value
                    if value is not None:
                        receipt['configHash'] = binding_hash(value)
                else:
                    path = adapter.skill_path(item['name'])
                    if operation == 'migrate':
                        source = self.home / '.agents/skills' / item['name']
                        if tree_hashes(source) != action['sharedFingerprint']:
                            raise ConfigConflict('The shared skill changed after review; nothing was removed.')
                        original = source
                        prior_metadata = read_json(source / 'parity-source.json', read_json(source / 'source.json', {}))
                        known_source = item.get('sourceUrl', '').rstrip('/').lower()
                        recorded_source = str(prior_metadata.get('source', '')).rstrip('/').lower()
                        receipt['updateEligible'] = bool(item['installable'] and known_source and
                            known_source == recorded_source and re.fullmatch(r'[0-9a-f]{40}', str(prior_metadata.get('commit', ''))))
                        if receipt['updateEligible']:
                            receipt['ref'] = prior_metadata['commit']
                            receipt['version'] = str(prior_metadata.get('version') or prior_metadata['commit'][:12])
                        if path.exists() and path.resolve() != source.resolve():
                            raise ConfigConflict('An agent skill appeared after migration review.')
                        expected = 'link' if is_link(path) else (tree_hashes(path) if path.exists() else None)
                    else:
                        original, expected = packages.get(item['id']), action['fingerprint']
                        if (tree_hashes(path) if path.exists() else None) != expected:
                            raise ConfigConflict('A skill changed after review; it was preserved.')
                    staged = None
                    if operation != 'uninstall':
                        staged = staging / str(index)
                        shutil.copytree(original, staged)
                        for metadata_name in ['source.json', 'parity-source.json']:
                            metadata_path = staged / metadata_name
                            if metadata_name == 'parity-source.json' and not metadata_path.exists():
                                continue
                            metadata = read_json(metadata_path, {})
                            metadata.update(managedBy='mcp-and-skills', itemId=item['id'], target=target)
                            write_json(metadata_path, metadata)
                        receipt['files'] = tree_hashes(staged)
                        receipt['skillPath'] = path.relative_to(adapter.profile).as_posix()
                    mutations.append({'kind': 'directory', 'path': path, 'expected': expected, 'staged': staged})
                if operation == 'uninstall':
                    new_receipts['entries'].pop(key, None)
                else:
                    new_receipts['entries'][key] = receipt
            for target, changes in config_changes.items():
                adapter = self._adapter(target)
                before, payload = adapter.payload(changes)
                if (digest(before) if before is not None else None) != plan['configs'][target]:
                    raise ConfigConflict('An agent configuration changed while staging; it was preserved.')
                mutations.append({'kind': 'file', 'path': adapter.config_path, 'expected': before, 'payload': payload})
            if operation == 'migrate':
                for item_id in plan['ids']:
                    item = plan['items'].get(item_id)
                    if not item:
                        continue
                    source = self.home / '.agents/skills' / item['name']
                    fingerprint = next(action['sharedFingerprint'] for action in actions if action['id'] == item_id)
                    for target in AGENTS:
                        if target in plan['targets']:
                            continue
                        alias = self._adapter(target).skill_root / item['name']
                        if is_link(alias) and alias.resolve() == source.resolve():
                            mutations.append({'kind': 'directory', 'path': alias, 'expected': 'link', 'staged': None})
                    mutations.append({'kind': 'directory', 'path': source, 'expected': fingerprint, 'staged': None})
            for index, mutation in enumerate(mutations):
                path = mutation['path']
                if mutation['kind'] == 'file':
                    actual = path.read_bytes() if path.exists() else None
                else:
                    actual = 'link' if is_link(path) else (tree_hashes(path) if path.exists() else None)
                if actual != mutation['expected']:
                    raise ConfigConflict('An installation destination changed concurrently; it was preserved.')
                backup = transaction / (str(index) + '-' + path.name)
                if path.exists() or is_link(path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, backup)
                completed.append({'path': path, 'backup': backup, 'kind': mutation['kind'], 'installed': False, 'fingerprint': None})
                if mutation['kind'] == 'file':
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(path.name + '.install-' + secrets.token_hex(5))
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(mutation['payload'])
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                    fingerprint = digest(mutation['payload'])
                    completed[-1].update(installed=True, fingerprint=fingerprint)
                elif mutation['staged'] is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(mutation['staged']), str(path))
                    completed[-1].update(installed=True, fingerprint=tree_hashes(path))
            write_json(self.receipt_path, new_receipts)
            self.receipts = new_receipts
            write_json(transaction / 'transaction.json', {'operation': operation, 'state': 'complete',
                       'items': plan['ids'], 'targets': plan['targets'], 'completedAt': now()})
            return {'status': 'updated' if operation == 'update' else 'installed' if operation == 'install' else operation,
                    'operation': operation, 'changed': len(actions), 'targets': plan['targets'],
                    'message': 'Selected global installations were ' + ('removed.' if operation == 'uninstall' else 'committed with backups.')}
        except Exception:
            self.receipts = old_receipts
            restoration_errors = []
            for index, change in reversed(list(enumerate(completed))):
                path, backup = change['path'], change['backup']
                try:
                    if change['installed'] and path.exists():
                        actual = digest(path.read_bytes()) if change['kind'] == 'file' else tree_hashes(path)
                        if actual != change['fingerprint']:
                            restoration_errors.append(path.name)
                            continue
                        os.replace(path, transaction / ('failed-' + str(index)))
                    if backup.exists() or is_link(backup):
                        os.replace(backup, path)
                except OSError:
                    restoration_errors.append(path.name)
            # Receipts are committed last. Restore only our exact written data.
            try:
                if self.receipt_path.exists() and read_json(self.receipt_path) == new_receipts:
                    if old_receipt is None:
                        self.receipt_path.unlink()
                    else:
                        self.receipt_path.write_bytes(old_receipt)
            except (OSError, ValueError):
                restoration_errors.append('ownership records')
            write_json(transaction / 'transaction.json', {'operation': operation,
                       'state': 'rollback-incomplete' if restoration_errors else 'rolled-back',
                       'unrestoredCount': len(restoration_errors), 'completedAt': now()})
            if restoration_errors:
                raise ConfigConflict('An installation failed; some concurrent edits were preserved and private backups remain for review.') from None
            raise
        finally:
            if staging.exists():
                _relative(staging, self.state_dir / 'install-staging')
                shutil.rmtree(staging)

    def check_updates(self, progress=None, targets=None):
        if targets is not None and (not isinstance(targets, list) or any(x not in AGENTS for x in targets)):
            raise ValueError('Unknown update target')
        units, resolved, source_versions = [], {}, {}
        with self.lock:
            for receipt in self.receipts['entries'].values():
                item_id, target = receipt.get('itemId'), receipt.get('target')
                if item_id not in self.items or target not in AGENTS or targets and target not in targets:
                    continue
                item = self.items[item_id]
                unit = {'id': 'install:' + item_id + ':' + target, 'name': item['name'] + ' (' + target + ')',
                        'kind': item['kind'], 'targetNames': [item['name']], 'targets': [target], 'itemCount': 1,
                        'installedVersion': receipt.get('version', 'Unknown'), 'sourceUrl': item['sourceUrl']}
                try:
                    state = self._presence(item, target)
                    if not state['managed']:
                        unit.update(status='local-changes', latestVersion='Not checked',
                                    detail='Owned installation changed or was removed; review it before updating.')
                    elif not receipt.get('updateEligible', True) or not item['installable']:
                        unit.update(status='host-managed', latestVersion='Manual source',
                                    detail='This migrated local skill has no verified public publisher binding; its files are preserved.')
                    elif item['recipe']['type'] == 'http':
                        unit.update(status='host-managed', latestVersion='Provider managed',
                                    detail='The provider deploys this hosted MCP service.')
                    else:
                        key = (item_id, receipt.get('version'), receipt.get('ref'))
                        if key not in resolved:
                            resolved[key] = self._latest(item, receipt, source_versions)
                        latest = resolved[key]
                        newer = latest['version'] != receipt.get('version') or latest['recipe'].get('ref', '') != receipt.get('ref', '')
                        unit.update(latestVersion=latest['version'], status='update-available' if newer else 'up-to-date',
                                    detail='Only this owned agent installation is eligible for replacement.')
                except Exception:
                    unit.update(status='error', latestVersion='Unavailable', detail='The owned source version could not be verified.')
                units.append(unit)
                _emit(progress, 'Checked owned installation: ' + item['name'] + ' for ' + target)
        return units

    def update_unit(self, unit_id, progress=None):
        if not isinstance(unit_id, str) or not unit_id.startswith('install:'):
            raise ValueError('Unknown owned update source')
        item_id, _, target = unit_id[len('install:'):].rpartition(':')
        plan = self.plan([item_id], [target], 'update')
        if not plan['canExecute']:
            return {'id': unit_id, 'status': 'local-changes', 'message': 'Review the owned installation plan before updating.'}
        result = self.execute(plan['id'], progress)
        result.update(id=unit_id, installedVersion=self._receipt(item_id, target)['version'])
        return result
