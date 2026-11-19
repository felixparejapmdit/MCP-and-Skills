"""Isolated read-only detection checks. No real agent configuration is changed."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from availability import AvailabilityTracker, inventory_items, runtime_missing


SECRET = 'fixture-private-credential-918273'


class FakeSkills:
    def __init__(self):
        self.rows = []

    def discover(self):
        return self.rows


class FakeManager:
    def __init__(self):
        self.skills = FakeSkills()
        self.job = None

    def sanitize(self, value):
        if isinstance(value, str):
            return value.replace(SECRET, '[redacted]')
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.sanitize(item) for item in value]
        return value


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent-availability-test-')
        self.root = Path(self.temp.name)
        self.home, self.state, self.app = self.root / 'home', self.root / 'state', self.root / 'app'
        self.home.mkdir()
        self.app.mkdir()
        self.manager = FakeManager()
        self.which = lambda command: 'fixture-launcher' if command in {'node', 'node.exe', 'cmd', 'cmd.exe', 'npx'} else None
        self.tracker = AvailabilityTracker(self.home, self.state, self.app, which=self.which)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.root.name.startswith('agent-availability-test-'))
        self.temp.cleanup()

    def json(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def server(self, apps=('claude', 'codex', 'gemini'), key='github', config=None):
        config = config or {'type': 'http', 'url': 'https://example.test/mcp'}
        if 'claude' in apps:
            self.json('.claude.json', {'mcpServers': {key: config}})
        if 'gemini' in apps:
            self.json('.gemini/settings.json', {'mcpServers': {key: config}})
        if 'codex' in apps:
            path = self.home / '.codex/config.toml'
            path.parent.mkdir(exist_ok=True)
            body = '\n'.join(name + ' = ' + json.dumps(value) for name, value in config.items() if isinstance(value, (str, list)))
            path.write_text('[mcp_servers.' + key + ']\n' + body + '\n', encoding='utf-8')

    def item(self, result, key):
        return next(row for row in result['items'] if row['id'] == key)

    def test_selected_agent_detects_configuration_gap_and_restoration(self):
        self.server(('claude', 'codex'))
        result = self.tracker.scan(self.manager, 'gemini')
        row = self.item(result, 'mcp:github')
        self.assertEqual('missing', row['state'])
        self.assertEqual(['claude', 'codex'], row['installedApps'])
        self.assertEqual(['gemini'], row['missingApps'])
        self.assertFalse(row['removed'])
        self.server()
        restored = self.item(self.tracker.scan(self.manager, 'gemini'), 'mcp:github')
        self.assertEqual('installed', restored['state'])

    def test_deleted_configuration_is_retained_as_removed_across_restart(self):
        self.server()
        self.tracker.scan(self.manager)
        for path in ['.claude.json', '.codex/config.toml', '.gemini/settings.json']:
            (self.home / path).unlink()
        restarted = AvailabilityTracker(self.home, self.state, self.app, which=self.which)
        result = restarted.scan(self.manager)
        row = self.item(result, 'mcp:github')
        self.assertEqual('missing', row['state'])
        self.assertTrue(row['removed'])
        self.assertEqual([], row['installedApps'])
        self.assertEqual(1, result['summary']['removedCount'])
        self.assertEqual(1, len(json.loads(restarted.path.read_text())['items']))

    def test_partial_uninstall_only_affects_the_missing_agent(self):
        self.server()
        self.tracker.scan(self.manager)
        (self.home / '.gemini/settings.json').unlink()
        row = self.item(self.tracker.scan(self.manager, 'gemini'), 'mcp:github')
        self.assertTrue(row['removed'])
        self.assertEqual(['gemini'], row['missingApps'])
        self.assertEqual('installed', self.item(self.tracker.scan(self.manager, 'codex'), 'mcp:github')['state'])

    def test_malformed_config_does_not_fabricate_uninstalls_or_overwrite_history(self):
        self.server()
        self.tracker.scan(self.manager)
        before = self.tracker.path.read_bytes()
        (self.home / '.codex/config.toml').write_text('[broken TOML')
        result = self.tracker.scan(self.manager, 'codex')
        self.assertFalse(result['scanComplete'])
        self.assertTrue(result['warnings'])
        self.assertEqual('installed', self.item(result, 'mcp:github')['state'])
        self.assertEqual(before, self.tracker.path.read_bytes())

    def test_incomplete_initial_scan_omits_unknown_target_and_catalog_claims(self):
        self.server(('claude',))
        (self.home / '.gemini').mkdir()
        (self.home / '.gemini/settings.json').write_text('not json')
        (self.app / 'suggestions.json').write_text(json.dumps([{'id':'new', 'name':'New MCP', 'kind':'mcp'}]))
        result = self.tracker.scan(self.manager, 'gemini')
        self.assertFalse(result['scanComplete'])
        self.assertEqual([], result['items'])
        all_result = self.tracker.scan(self.manager)
        self.assertFalse(any(row['state'] == 'not-installed' for row in all_result['items']))

    def test_active_update_does_not_treat_transient_directory_gap_as_removal(self):
        self.manager.skills.rows = [{'name':'portable', 'sourceId':'skill:ecc', 'apps':['Claude','Codex','Gemini']}]
        self.tracker.scan(self.manager)
        before = self.tracker.path.read_bytes()
        self.manager.skills.rows = []
        self.manager.job = {'status':'running'}
        result = self.tracker.scan(self.manager, 'gemini')
        self.assertEqual('installed', self.item(result, 'skill:portable')['state'])
        self.assertFalse(result['scanComplete'])
        self.assertEqual(before, self.tracker.path.read_bytes())
        self.manager.job = {'status':'done'}
        self.assertTrue(self.item(self.tracker.scan(self.manager, 'gemini'), 'skill:portable')['removed'])

    def test_native_skills_do_not_create_cross_agent_missing_items(self):
        self.manager.skills.rows = [{'name':'codex-native', 'sourceId':'skill:codex-system', 'apps':['Codex']},
            {'name':'claude-native', 'sourceId':'skill:claude-native', 'apps':['Claude']}]
        self.assertEqual([], self.tracker.scan(self.manager, 'gemini')['items'])
        all_result = self.tracker.scan(self.manager)
        self.assertEqual(2, all_result['summary']['installedCount'])
        self.assertEqual(0, all_result['summary']['missingCount'])

    def test_install_operation_does_not_record_transient_removal(self):
        self.manager.skills.rows = [{'name':'portable', 'sourceId':'skill:ecc', 'apps':['Gemini']}]
        self.tracker.scan(self.manager)
        before = self.tracker.path.read_bytes()
        self.manager.skills.rows = []
        self.manager.operations = mock.Mock()
        self.manager.operations.running.return_value = True
        result = self.tracker.scan(self.manager, 'gemini')
        self.assertEqual('installed', self.item(result, 'skill:portable')['state'])
        self.assertFalse(result['scanComplete'])
        self.assertEqual(before, self.tracker.path.read_bytes())

    def test_shared_skill_is_compared_across_compatible_agents(self):
        skill = self.home / '.agents/skills/custom/SKILL.md'
        skill.parent.mkdir(parents=True)
        skill.write_text('---\nname: custom\ndescription: Example\n---\n')
        self.manager.skills.rows = [{'name':'custom', 'sourceId':'skill:local', 'apps':['Codex','Gemini']}]
        row = self.item(self.tracker.scan(self.manager, 'claude'), 'skill:custom')
        self.assertEqual('missing', row['state'])
        self.assertFalse(row['removed'])
        self.assertEqual(['claude','codex','gemini'], row['compatibleApps'])

    def test_missing_runtime_is_distinct_from_preserved_configuration(self):
        launcher = self.root / 'missing-server.js'
        self.server(('claude',), key='filesystem', config={'command':'node','args':[str(launcher)]})
        result = self.tracker.scan(self.manager, 'claude')
        row = self.item(result, 'mcp:filesystem')
        self.assertEqual('missing', row['state'])
        self.assertTrue(row['runtimeMissing'])
        self.assertFalse(row['removed'])
        self.assertTrue((self.home / '.claude.json').is_file())
        launcher.write_text('// restored')
        self.assertEqual('installed', self.item(self.tracker.scan(self.manager, 'claude'), 'mcp:filesystem')['state'])

    def test_npx_on_demand_does_not_require_a_preexisting_package_cache(self):
        config = {'command':'cmd','args':['/c','npx','-y','@example/server@latest']}
        self.assertFalse(runtime_missing(config, home=self.home, which=self.which))
        self.assertTrue(runtime_missing(config, home=self.home, which=lambda command: 'cmd' if command == 'cmd' else None))

    def test_suggestions_are_not_installed_and_aliases_avoid_duplicates(self):
        self.server()
        (self.app / 'suggestions.json').write_text(json.dumps([
            {'id':'mcp-github','name':'GitHub Official','kind':'mcp','matchNames':['github']},
            {'id':'mcp-brave','name':'Brave Search','kind':'mcp','url':'https://example.test/source?private=yes'}]))
        result = self.tracker.scan(self.manager, 'claude')
        self.assertEqual(1, result['summary']['notInstalledCount'])
        new = self.item(result, 'catalog:mcp-brave')
        self.assertEqual('https://example.test/source', new['sourceUrl'])
        self.assertFalse(new['removed'])

    def test_snapshot_seeds_removal_detection_before_first_scan(self):
        web = self.app / 'web'; web.mkdir()
        (web / 'snapshot.json').write_text(json.dumps({'mcp':[{'id':'old','name':'Old server','apps':['gemini']}]}))
        tracker = AvailabilityTracker(self.home, self.state, self.app, which=self.which)
        row = self.item(tracker.scan(self.manager, 'gemini'), 'mcp:old')
        self.assertTrue(row['removed'])

    def test_history_and_api_do_not_contain_secrets_configs_or_launch_paths(self):
        config = {'url':'https://user:'+SECRET+'@example.test/mcp?token='+SECRET,
                  'headers':{'Authorization':'Bearer '+SECRET},'env':{'SECRET':SECRET},
                  'args':[str(self.home / 'private-launch-path.py')]}
        self.server(('claude',), config=config)
        result = self.tracker.scan(self.manager, 'claude')
        serialized = json.dumps(result) + self.tracker.path.read_text()
        for value in [SECRET, 'Authorization', 'private-launch-path', str(self.home), '?token=']:
            self.assertNotIn(value, serialized)

    def test_cli_presence_and_existing_configuration_are_distinct(self):
        self.server(('claude',))
        self.tracker.which = lambda command: 'fixture-cli' if command == 'codex' else None
        apps = {row['id']:row for row in self.tracker.scan(self.manager)['applications']}
        self.assertTrue(apps['claude']['configured'])
        self.assertFalse(apps['claude']['cliInstalled'])
        self.assertTrue(apps['codex']['cliInstalled'])
        self.assertFalse(apps['codex']['configured'])
        self.assertFalse(apps['gemini']['detected'])

    def test_invalid_target_is_rejected_without_scanning_or_writing(self):
        with mock.patch.object(self.tracker, '_scan') as scan:
            with self.assertRaises(ValueError):
                self.tracker.scan(self.manager, 'invented')
            scan.assert_not_called()

    def test_provider_session_rows_are_not_global_installation_evidence(self):
        result = inventory_items({'skillGroups':[{'kind':'skill','sourceId':'provider:work-session',
            'names':['sites:sites'],'apps':['codex']}]})
        self.assertEqual({}, result)

    def test_known_ecc_adapter_rename_is_not_a_false_cross_agent_gap(self):
        self.manager.skills.rows = [
            {'name':'deep-research','sourceId':'skill:ecc','apps':['Claude']},
            {'name':'ecc-deep-research','sourceId':'skill:ecc','apps':['Codex','Gemini']},
            {'name':'deep-research','sourceId':'skill:claude-synced','apps':['Claude','Codex','Gemini']},
        ]
        result = self.tracker.scan(self.manager)
        self.assertEqual(2, result['summary']['installedCount'])
        self.assertEqual(0, result['summary']['missingCount'])
        self.assertEqual(['claude','codex','gemini'], self.item(result, 'skill:ecc-deep-research')['installedApps'])

    def test_repeated_agent_views_do_not_rewrite_identical_history(self):
        self.server()
        self.tracker.scan(self.manager)
        before = self.tracker.path.read_bytes()
        with mock.patch.object(self.tracker, '_write') as write:
            self.tracker.scan(self.manager, 'gemini')
            self.tracker.scan(self.manager, 'codex')
            write.assert_not_called()
        self.assertEqual(before, self.tracker.path.read_bytes())

    def test_malformed_history_is_reported_and_preserved_instead_of_crashing(self):
        self.state.mkdir()
        path = self.state / 'availability-history.json'
        path.write_text('{"items":42}')
        self.server()
        tracker = AvailabilityTracker(self.home, self.state, self.app, which=self.which)
        result = tracker.scan(self.manager, 'claude')
        self.assertEqual('installed', self.item(result, 'mcp:github')['state'])
        self.assertFalse(result['scanComplete'])
        self.assertTrue(result['warnings'])
        self.assertEqual('{"items":42}', path.read_text())


if __name__ == '__main__':
    unittest.main()
