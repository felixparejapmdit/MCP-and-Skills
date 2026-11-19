"""Isolated checks for updater provenance, path safety and replacement rollback."""
from __future__ import annotations

import json
import io
from pathlib import Path
import tempfile
import threading
import subprocess
import urllib.error
import urllib.request
import unittest
from unittest import mock

import skill_updates as s


def entry(name='fixture', body='Original'):
    return f'---\nname: {name}\ndescription: Fixture skill\n---\n\n{body}\n'.encode()


class SkillUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='skill-manager-test-')
        self.root = Path(self.temp.name)
        self.home, self.state = self.root / 'home', self.root / 'state'
        self.home.mkdir()
        self.updater = s.SkillUpdater(self.home, self.state)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.root.name.startswith('skill-manager-test-'))
        self.temp.cleanup()

    def skill(self, name, body='Original'):
        path = self.home / '.agents/skills' / name
        path.mkdir(parents=True)
        (path / 'SKILL.md').write_bytes(entry(name, body))
        return path

    def test_unsafe_and_windows_ambiguous_paths_are_rejected(self):
        for value in ('../outside', '/outside', r'C:\outside', 'folder/../outside',
                      '.', './inside', 'folder//inside', 'NUL.txt', 'folder/file.', 'file '):
            with self.subTest(value=value), self.assertRaises(s.UpdateError):
                s._relative(value)
        self.assertEqual('references/design.md', s._relative('references/design.md'))

    def test_unknown_skill_source_is_not_eligible_for_auto_update(self):
        path = self.skill('fixture')
        s._write_json(path / 'source.json', {'source': 'https://github.com/stranger/arbitrary'})
        with mock.patch.object(self.updater.network, 'json') as network:
            units = self.updater.check_all()
            network.assert_not_called()
        self.assertEqual('host-managed', units[0]['status'])
        self.assertFalse(self.updater.discover()[0]['managed'])

    def test_recorded_preexisting_edit_is_detected_at_first_enrollment(self):
        path = self.skill('hallmark', 'User edit')
        target = s._Target('hallmark', path, expected={'SKILL.md': s._hash(entry('hallmark'))})
        bundle = s._Bundle('hallmark', [target])
        self.assertEqual([str(path)], self.updater._conflicts(bundle))
        self.assertEqual(entry('hallmark', 'User edit'), (path / 'SKILL.md').read_bytes())

    def test_later_user_edit_is_preserved(self):
        path = self.skill('fixture')
        target = s._Target('fixture', path, expected={'SKILL.md': s._hash(entry())})
        bundle = s._Bundle('hallmark', [target])
        self.assertEqual([], self.updater._conflicts(bundle))
        (path / 'notes.txt').write_text('User notes', encoding='utf-8')
        self.assertEqual([str(path)], self.updater._conflicts(bundle))

    def test_unrecorded_local_asset_collision_is_preserved(self):
        path = self.skill('fixture')
        stage_path = self.root / 'stage'
        stage_path.mkdir()
        (stage_path / 'custom.txt').write_text('User asset', encoding='utf-8')
        stage = s._Stage(s._Target('fixture', path), stage_path, {'managed': ['SKILL.md']})
        with self.assertRaises(s.Conflict):
            stage.write('custom.txt', b'Publisher asset')
        self.assertEqual('User asset', (stage_path / 'custom.txt').read_text())

    def make_stage(self, name):
        target = self.skill(name)
        staged = self.root / ('stage-' + name)
        staged.mkdir()
        (staged / 'SKILL.md').write_bytes(entry(name, 'Updated'))
        return s._Stage(s._Target(name, target), staged, {})

    def test_multi_target_failure_restores_every_completed_replacement(self):
        first, second = self.make_stage('first'), self.make_stage('second')
        real_replace = s.os.replace

        def fail_second(source, destination):
            if Path(source) == second.path:
                raise OSError('Fixture replacement failure')
            return real_replace(source, destination)

        with mock.patch.object(s.os, 'replace', side_effect=fail_second), self.assertRaises(OSError):
            with self.updater._commit([first, second]):
                pass
        for stage in (first, second):
            self.assertEqual(entry(stage.target.name), (stage.target.path / 'SKILL.md').read_bytes())
        manifests = list((self.state / 'skill-backups').glob('*/transaction.json'))
        self.assertEqual('rolled-back', json.loads(manifests[0].read_text())['state'])

    def test_concurrent_edit_before_later_replacement_survives_abort(self):
        first, second = self.make_stage('first'), self.make_stage('second')
        real_replace = s.os.replace

        def edit_after_first(source, destination):
            result = real_replace(source, destination)
            if Path(source) == first.path:
                (second.target.path / 'SKILL.md').write_bytes(entry('second', 'Concurrent edit'))
            return result

        with mock.patch.object(s.os, 'replace', side_effect=edit_after_first), self.assertRaises(s.Conflict):
            with self.updater._commit([first, second]):
                pass
        self.assertEqual(entry('first'), (first.target.path / 'SKILL.md').read_bytes())
        self.assertEqual(entry('second', 'Concurrent edit'), (second.target.path / 'SKILL.md').read_bytes())

    def test_rollback_retains_edit_to_a_just_installed_target(self):
        first = self.make_stage('first')
        with self.assertRaises(s.UpdateError):
            with self.updater._commit([first]):
                (first.target.path / 'SKILL.md').write_bytes(entry('first', 'New user edit'))
                raise OSError('Fixture post-install failure')
        self.assertEqual(entry('first', 'New user edit'), (first.target.path / 'SKILL.md').read_bytes())
        backups = list((self.state / 'skill-backups').glob('*/0-first/SKILL.md'))
        self.assertEqual(entry('first'), backups[0].read_bytes())

    def test_snapshot_rejects_symlink_blobs_and_corrupt_downloads(self):
        network = mock.Mock()
        sha = 'a' * 40
        network.json.return_value = {'tree': [{'path': 'file.txt', 'type': 'blob', 'mode': '120000', 'sha': sha}]}
        snapshot = s._Snapshot(network, 'pbakaus/impeccable', sha, self.root / 'cache')
        with self.assertRaises(s.UpdateError):
            snapshot.read('file.txt')
        network.bytes.assert_not_called()
        snapshot.tree['file.txt']['mode'] = '100644'
        network.bytes.return_value = b'Wrong content'
        with self.assertRaises(s.UpdateError):
            snapshot.read('file.txt')

    def test_windows_reparse_attribute_is_treated_as_a_link(self):
        path = mock.Mock()
        path.is_symlink.return_value = False
        path.lstat.return_value.st_file_attributes = 0x400
        self.assertTrue(s._is_link(path))

    def release_bundle(self):
        path = self.skill('ponytail')
        target = s._Target('ponytail', path, metadata={'commit': 'a' * 40, 'version': '5.3.3'})
        return s._Bundle('ponytail', [target])

    def test_latest_release_cannot_downgrade_an_installed_newer_commit(self):
        bundle = self.release_bundle()
        responses = [[{'tag_name': 'v5.3.3'}], {'sha': 'b' * 40}, {'status': 'behind'}]
        with mock.patch.object(self.updater.network, 'json', side_effect=responses):
            latest = self.updater._latest(bundle)
        self.assertEqual('a' * 40, latest['commit'])
        self.assertEqual('v5.3.3', latest['aheadOfRelease'])

    def test_genuinely_newer_tagged_revision_is_eligible(self):
        bundle = self.release_bundle()
        responses = [[{'tag_name': 'v5.4.0'}], {'sha': 'b' * 40}, {'status': 'ahead'}]
        with mock.patch.object(self.updater.network, 'json', side_effect=responses):
            latest = self.updater._latest(bundle)
        self.assertEqual('b' * 40, latest['commit'])
        self.assertEqual('v5.4.0', latest['version'])

    def test_diverged_release_history_is_preserved_for_review(self):
        bundle = self.release_bundle()
        responses = [[{'tag_name': 'v5.4.0'}], {'sha': 'b' * 40}, {'status': 'diverged'}]
        with mock.patch.object(self.updater.network, 'json', side_effect=responses), self.assertRaises(s.Conflict):
            self.updater._latest(bundle)

    def test_busy_update_keeps_last_inventory_readable(self):
        self.skill('fixture')
        original = self.updater.discover()
        locked, release = threading.Event(), threading.Event()

        def hold_update_lock():
            with self.updater._lock:
                locked.set()
                release.wait(5)

        worker = threading.Thread(target=hold_update_lock)
        worker.start()
        self.assertTrue(locked.wait(2))
        try:
            with mock.patch.object(self.updater, '_catalog', side_effect=AssertionError('Scan must not block')):
                self.assertEqual(original, self.updater.discover())
        finally:
            release.set()
            worker.join(2)

    def response(self, value=None):
        response = io.BytesIO(json.dumps(value or {'value': 'public metadata'}).encode())
        response.headers = {'ETag': 'fixture-etag'}
        return response

    def http_error(self, status, remaining=None):
        headers = {} if remaining is None else {'X-RateLimit-Remaining': remaining}
        return urllib.error.HTTPError('https://api.github.com/repos/fixture/test', status, 'Fixture failure', headers, io.BytesIO())

    def test_public_rate_limit_reuses_existing_cli_login_in_memory_only(self):
        network = self.updater.network
        token = 'ghp_fixture_credential_1234567890'
        replies = [self.http_error(403, '0'), self.response(), self.response()]
        with mock.patch.object(network._opener, 'open', side_effect=replies) as request, \
                mock.patch.object(s.shutil, 'which', return_value='gh.exe'), \
                mock.patch.object(s.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, token + '\n')) as command:
            network.json('https://api.github.com/repos/fixture/test')
            network.json('https://api.github.com/repos/fixture/other')
        sent = [x.args[0] for x in request.call_args_list]
        self.assertIsNone(sent[0].get_header('Authorization'))
        self.assertEqual('Bearer ' + token, sent[1].get_header('Authorization'))
        self.assertEqual('Bearer ' + token, sent[2].get_header('Authorization'))
        command.assert_called_once()
        for path in network.cache.rglob('*.json'):
            self.assertNotIn(token, path.read_text())

    def test_credentials_never_attach_to_raw_or_release_asset_hosts(self):
        network = self.updater.network
        network._github_token = 'ghp_fixture_credential_1234567890'
        with mock.patch.object(network._opener, 'open', side_effect=[self.response(), self.response()]) as request:
            for url in ['https://raw.githubusercontent.com/fixture/test/main/file', 'https://github.com/fixture/test/releases/file']:
                network._open(url, {'Authorization': 'Bearer caller-secret'})
        for call in request.call_args_list:
            self.assertIsNone(call.args[0].get_header('Authorization'))

    def test_rejected_existing_login_falls_back_to_public_request_once(self):
        network = self.updater.network
        network._github_token = 'ghp_fixture_credential_1234567890'
        with mock.patch.object(network._opener, 'open', side_effect=[self.http_error(401), self.response()]) as request:
            network.json('https://api.github.com/repos/fixture/test')
        self.assertIsNotNone(request.call_args_list[0].args[0].get_header('Authorization'))
        self.assertIsNone(request.call_args_list[1].args[0].get_header('Authorization'))
        self.assertIsNone(network._github_token)
        self.assertTrue(network._github_auth_rejected)

    def test_unrelated_forbidden_response_does_not_request_cli_credentials(self):
        network = self.updater.network
        with mock.patch.object(network._opener, 'open', side_effect=self.http_error(403, '42')), \
                mock.patch.object(s.subprocess, 'run') as command, self.assertRaises(s.UpdateError):
            network.json('https://api.github.com/repos/fixture/test')
        command.assert_not_called()

    def test_authenticated_redirect_cannot_forward_credentials_to_another_host(self):
        handler = s._CredentialSafeRedirect()
        request = urllib.request.Request('https://api.github.com/fixture', headers={'Authorization': 'Bearer fixture-secret'})
        for url in ['https://github.com/fixture', 'https://api.github.com.evil.test/fixture',
                    'http://api.github.com/fixture', 'https://api.github.com:444/fixture',
                    'https://user:password@api.github.com/fixture']:
            with self.subTest(url=url), self.assertRaises(s.UpdateError):
                handler.redirect_request(request, None, 302, 'Fixture redirect', {}, url)
        redirected = handler.redirect_request(request, None, 302, 'Fixture redirect', {}, 'https://api.github.com/new-fixture')
        self.assertEqual('Bearer fixture-secret', redirected.get_header('Authorization'))

    def test_cli_failure_never_surfaces_its_captured_output(self):
        network = self.updater.network
        with mock.patch.object(s.shutil, 'which', return_value='gh.exe'), \
                mock.patch.object(s.subprocess, 'run', side_effect=subprocess.TimeoutExpired('gh', 10, output='private-output')):
            self.assertIsNone(network._existing_github_token())


if __name__ == '__main__':
    unittest.main()
