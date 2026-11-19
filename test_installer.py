"""Exercise actual transactions against temporary profiles, never the user's home."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from agent_adapters import AgentAdapter, ConfigConflict
from installer import InstallEngine, _ArchiveSnapshot


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mcp-installer-test-')
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.engine = InstallEngine(self.home, self.root / 'state', Path(__file__).parent)

    def tearDown(self):
        assert self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
        assert self.root.name.startswith('mcp-installer-test-')
        self.temp.cleanup()

    def execute(self, ids, targets, operation='install'):
        plan = self.engine.plan(ids, targets, operation)
        self.assertTrue(plan['canExecute'], plan)
        return self.engine.execute(plan['id'])

    def test_fresh_pc_keeps_default_catalog_and_detects_all_missing(self):
        items = self.engine.catalog()['items']
        self.assertEqual(355, len(items))
        self.assertEqual(18, sum(i['kind'] == 'mcp' for i in items))
        self.assertEqual(337, sum(i['kind'] == 'skill' for i in items))
        self.assertFalse(any(i['installedAgents'] for i in items))

    def test_gemini_only_install_keeps_other_profiles_absent(self):
        self.execute(['mcp:context7'], ['gemini'])
        self.assertIn('context7', AgentAdapter(self.home, 'gemini').read()[2])
        self.assertFalse((self.home / '.claude.json').exists())
        self.assertFalse((self.home / '.codex').exists())
        self.assertEqual(['gemini'], next(i for i in self.engine.catalog()['items'] if i['id'] == 'mcp:context7')['managedAgents'])

    def test_uninstall_removes_only_selected_agent(self):
        self.execute(['mcp:context7'], ['claude', 'codex', 'gemini'])
        self.execute(['mcp:context7'], ['codex'], 'uninstall')
        for agent in ['claude', 'codex', 'gemini']:
            self.assertEqual(agent != 'codex', 'context7' in AgentAdapter(self.home, agent).read()[2])

    def test_existing_external_registration_is_skipped_and_not_adopted(self):
        path = self.home / '.gemini/settings.json'
        path.parent.mkdir()
        original = b'{"theme":"custom","mcpServers":{"context7":{"httpUrl":"https://example.org/custom"}}}'
        path.write_bytes(original)
        p = self.engine.plan(['mcp:context7'], ['gemini'])
        self.assertEqual('skip', p['changes'][0]['action'])
        self.engine.execute(p['id'])
        self.assertEqual(original, path.read_bytes())
        self.assertFalse(self.engine.plan(['mcp:context7'], ['gemini'], 'uninstall')['canExecute'])

    def test_concurrent_config_change_is_preserved(self):
        p = self.engine.plan(['mcp:context7'], ['gemini'])
        path = self.home / '.gemini/settings.json'
        path.parent.mkdir()
        original = b'{"theme":"edited-after-plan"}'
        path.write_bytes(original)
        with self.assertRaises(ConfigConflict):
            self.engine.execute(p['id'])
        self.assertEqual(original, path.read_bytes())
        self.assertEqual('rolled-back', self.engine.history()[0]['state'])

    def test_one_use_plan_and_no_implicit_targets(self):
        with self.assertRaises(ValueError):
            self.engine.plan(['mcp:context7'], [])
        p = self.engine.plan(['mcp:context7'], ['gemini'])
        self.engine.execute(p['id'])
        with self.assertRaises(ValueError):
            self.engine.execute(p['id'])

    def test_receipt_failure_rolls_back_all_agent_configurations(self):
        import installer
        original_write = installer.write_json
        def fail_receipt(path, value):
            if Path(path) == self.engine.receipt_path:
                raise OSError('simulated disk failure')
            return original_write(path, value)
        p = self.engine.plan(['mcp:context7'], ['claude', 'gemini'])
        with patch('installer.write_json', side_effect=fail_receipt), self.assertRaises(OSError):
            self.engine.execute(p['id'])
        self.assertFalse((self.home / '.claude.json').exists())
        self.assertFalse((self.home / '.gemini/settings.json').exists())
        self.assertFalse(self.engine.receipts['entries'])

    def test_skill_install_and_remove_use_only_private_target_folder(self):
        item = self.engine.items['skill:brainstorming']
        staged = self.root / 'package'
        staged.mkdir()
        (staged / 'SKILL.md').write_text('---\nname: brainstorming\ndescription: Test package\n---\n# Brainstorming\n', encoding='utf-8')
        with patch.object(self.engine, '_latest', side_effect=lambda value, *args: copy.deepcopy(value)), \
             patch.object(self.engine.packages, 'prepare', return_value={item['id']: staged}):
            self.execute([item['id']], ['codex'])
        path = AgentAdapter(self.home, 'codex').skill_path(item['name'])
        self.assertTrue((path / 'SKILL.md').is_file())
        self.assertFalse((self.home / '.agents').exists())
        self.assertFalse((self.home / '.gemini').exists())
        self.assertEqual('mcp-and-skills', json.loads((path / 'source.json').read_text())['managedBy'])
        self.execute([item['id']], ['codex'], 'uninstall')
        self.assertFalse(path.exists())

    def test_shared_skill_is_preserved_without_explicit_migration(self):
        shared = self.home / '.agents/skills/brainstorming'
        shared.mkdir(parents=True)
        (shared / 'SKILL.md').write_text('# Existing shared skill', encoding='utf-8')
        p = self.engine.plan(['skill:brainstorming'], ['gemini'])
        self.assertEqual('skip', p['changes'][0]['action'])
        self.engine.execute(p['id'])
        self.assertTrue((shared / 'SKILL.md').is_file())
        migration = self.engine.plan(['skill:brainstorming'], ['gemini'], 'migrate')
        self.assertEqual(['codex'], migration['migrations'][0]['removedFromAgents'])
        self.engine.execute(migration['id'])
        self.assertFalse(shared.exists())
        self.assertTrue((self.home / '.gemini/skills/brainstorming/SKILL.md').exists())
        self.assertFalse((self.home / '.codex/skills/brainstorming').exists())

    def test_plan_keeps_connection_secret_out_of_public_payload(self):
        secret = 'postgresql://test:fixture-secret@localhost/database'
        with patch.object(self.engine, '_latest', side_effect=lambda value, *args: copy.deepcopy(value)):
            p = self.engine.plan(['mcp:postgres'], ['gemini'], options={'mcp:postgres': {'connectionString': secret}})
        self.assertNotIn(secret, json.dumps(p))
        self.assertNotIn(secret, json.dumps(self.engine.catalog()))

    def test_linked_profile_is_not_written_even_if_destination_stays_in_home(self):
        original = AgentAdapter(self.home, 'gemini')
        original.profile.mkdir()
        with patch('agent_adapters.is_link', side_effect=lambda path: Path(path) == original.profile):
            plan = self.engine.plan(['mcp:context7'], ['gemini'])
            self.assertFalse(plan['canExecute'])
        self.assertFalse(original.config_path.exists())

    def test_archive_line_ending_conversion_requires_matching_git_blob(self):
        from skill_updates import _Snapshot
        from types import SimpleNamespace
        path = 'scripts/tool.cmd'
        expected = b'@echo off\necho verified\n'
        archive = self.root / 'source.zip'
        commit = 'a' * 40
        snapshot = SimpleNamespace(commit=commit, tree={path: {'mode': '100644', 'sha': _Snapshot._blob(expected)}}, _blob=_Snapshot._blob)
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('source-' + commit + '/' + path, expected.replace(b'\n', b'\r\n'))
        source = _ArchiveSnapshot(snapshot, archive, None, 'publisher/source')
        self.assertEqual(expected, source.read(path))
        source.archive.close()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('source-' + commit + '/' + path, b'@echo off\r\necho tampered\r\n')
        source = _ArchiveSnapshot(snapshot, archive, None, 'publisher/source')
        with self.assertRaises(ConfigConflict):
            source.read(path)
        source.archive.close()


if __name__ == '__main__':
    unittest.main()
