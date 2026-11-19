"""Isolated tests for host paths, runtime archives, launchers and Docker."""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

import docker_support as docker
import launcher
import platform_support as host


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mcp-distribution-test-')
        self.root = Path(self.temp.name)
        self.home, self.state, self.app = self.root / 'home', self.root / 'state', self.root / 'app'
        self.home.mkdir()
        self.app.mkdir()
        self.state.mkdir()

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.root.name.startswith('mcp-distribution-test-'))
        self.temp.cleanup()

    def test_native_user_state_paths_preserve_windows_compatibility(self):
        self.assertEqual(self.home/'AppData/Local/AgentInventoryManager', host.user_state_dir(self.home, {}, 'Windows'))
        self.assertEqual(self.home/'Library/Application Support/MCPandSkills', host.user_state_dir(self.home, {}, 'Darwin'))
        self.assertEqual(self.home/'.local/share/mcp-and-skills', host.user_state_dir(self.home, {}, 'Linux'))
        self.assertEqual(self.root/'xdg/mcp-and-skills', host.user_state_dir(self.home, {'XDG_DATA_HOME':str(self.root/'xdg')}, 'Linux'))

    def test_frozen_resources_are_independent_of_working_directory(self):
        with mock.patch.object(sys, '_MEIPASS', str(self.app), create=True):
            self.assertEqual(self.app.resolve(), host.resources_dir())

    def test_frozen_executable_is_never_used_as_general_python(self):
        fake = self.app/'manager.exe'; fake.write_bytes(b'fake')
        with mock.patch.object(sys, 'frozen', True, create=True), mock.patch.object(sys, 'executable', str(fake)), \
                mock.patch.object(host.shutil, 'which', return_value=str(fake)), mock.patch.object(host, '_run') as run:
            self.assertIsNone(host.find_python())
            run.assert_not_called()

    def test_node_engine_ranges_cover_current_catalog(self):
        self.assertTrue(host.satisfies('24.22.3', '^22.23.4 || ^24.22.1 || >=26.12.1'))
        self.assertFalse(host.satisfies('24.16.0', '^22.23.4 || ^24.22.1 || >=26.12.1'))
        self.assertTrue(host.satisfies('22.4.0', '>=20 <24'))
        self.assertTrue(host.satisfies('20.8.1', '~20.8.0'))
        self.assertFalse(host.satisfies('20.9.0', '~20.8.0'))
        self.assertFalse(host.satisfies('24.0.0', 'untrusted syntax'))

    def test_unsupported_architecture_is_explicit(self):
        self.assertEqual(('darwin','arm64'), host._architecture('Darwin','arm64'))
        self.assertEqual(('linux','x64'), host._architecture('Linux','x86_64'))
        with self.assertRaises(RuntimeError):
            host._architecture('Plan9','x86_64')

    def test_archive_traversal_never_writes_outside_staging(self):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:z.writestr('../escaped',b'bad')
        with self.assertRaises(RuntimeError):
            host.extract_runtime(data.getvalue(),self.root/'stage','zip')
        self.assertFalse((self.root/'escaped').exists())

    def test_tar_internal_links_are_skipped_and_escaping_links_rejected(self):
        for name in ['../lib/npm.js','../../../escaped']:
            data=io.BytesIO()
            with tarfile.open(fileobj=data,mode='w:gz') as archive:
                entry=tarfile.TarInfo('node/bin/npm');entry.type=tarfile.SYMTYPE;entry.linkname=name;archive.addfile(entry)
            if name.startswith('../../../'):
                with self.assertRaises(RuntimeError):
                    host.extract_runtime(data.getvalue(),self.root/'stage-bad','tar.gz')
            else:
                host.extract_runtime(data.getvalue(),self.root/'stage-good','tar.gz')
                self.assertFalse((self.root/'stage-good/node/bin/npm').exists())

    def test_runtime_checksum_failure_leaves_config_and_runtime_absent(self):
        index=[{'version':'v24.30.0','lts':'Krypton','files':['win-x64-zip']}]
        def download(url,limit):
            if url.endswith('index.json'):return json.dumps(index).encode()
            if url.endswith('SHASUMS256.txt'):return ('0'*64+'  node-v24.30.0-win-x64.zip\n').encode()
            return b'incorrect archive'
        with mock.patch.object(host,'find_node',return_value=None),mock.patch.object(host,'_architecture',return_value=('win','x64')), \
                mock.patch.object(host,'_download',side_effect=download):
            with self.assertRaisesRegex(RuntimeError,'checksum'):
                host.ensure_node('>=24',self.state)
        self.assertFalse((self.state/'runtimes/node/v24.30.0').exists())

    def test_node_macos_uses_official_osx_index_identifier(self):
        index=[{'version':'v24.30.0','lts':'Krypton','files':['osx-arm64-tar']}]
        def download(url,limit):
            if url.endswith('index.json'):return json.dumps(index).encode()
            raise RuntimeError('Selected Darwin distribution')
        with mock.patch.object(host,'find_node',return_value=None),mock.patch.object(host,'_architecture',return_value=('darwin','arm64')), \
                mock.patch.object(host,'_download',side_effect=download):
            with self.assertRaisesRegex(RuntimeError,'Selected Darwin'):
                host.ensure_node('>=24',self.state)

    def test_singleton_rejects_a_second_helper_for_same_profile(self):
        with launcher.singleton(self.state):
            with self.assertRaises(RuntimeError):
                with launcher.singleton(self.state):pass
        with launcher.singleton(self.state):pass

    def test_launcher_source_command_does_not_depend_on_cwd(self):
        with mock.patch.object(sys,'frozen',False,create=True),mock.patch.object(launcher,'resources_dir',return_value=self.app):
            self.assertEqual(str(self.app/'launcher.py'),launcher._command()[-1])

    def test_startup_windows_is_hidden_and_unicode_safe(self):
        path=self.root/'startup/MCP and Skill Updates.vbs'
        with mock.patch.object(launcher,'startup_path',return_value=path),mock.patch.object(launcher.platform,'system',return_value='Windows'), \
                mock.patch.object(launcher,'_command',return_value=[str(self.app/'管理器.exe')]):
            result=launcher.install_startup(self.state)
        self.assertTrue(result['startupEnabled'])
        script=path.read_text(encoding='utf-16')
        self.assertIn('管理器.exe',script)
        self.assertIn(', 0, False',script)
        self.assertIn('start --no-browser',script)

    def test_mac_launch_agent_is_foreground_helper_with_restart_on_failure(self):
        path=self.root/'launchagents/app.plist'
        with mock.patch.object(launcher,'startup_path',return_value=path),mock.patch.object(launcher.platform,'system',return_value='Darwin'), \
                mock.patch.object(launcher,'_command',return_value=[str(self.app/'manager')]):
            launcher.install_startup(self.state)
        data=plistlib.loads(path.read_bytes())
        self.assertIn('serve',data['ProgramArguments'])
        self.assertTrue(data['RunAtLoad'])
        self.assertEqual({'SuccessfulExit':False},data['KeepAlive'])

    def test_missing_docker_returns_guidance_without_starting_any_process(self):
        support=docker.DockerSupport(self.state,self.app)
        with mock.patch.object(support,'_docker',return_value=None),mock.patch.object(subprocess,'run') as run:
            result=support.check()
        self.assertFalse(result['available']);self.assertFalse(result['running'])
        self.assertIn('https://docs.docker.com/',result['installUrl']);run.assert_not_called()

    def test_docker_engine_running_does_not_imply_owned_sandbox_running(self):
        support=docker.DockerSupport(self.state,self.app)
        with mock.patch.object(support,'_docker',return_value='docker'),mock.patch.object(support,'_call',
                return_value=subprocess.CompletedProcess([],0,'27.0.0','')):
            result=support.check()
        self.assertTrue(result['engineRunning']);self.assertFalse(result['running'])

    def test_managed_compose_is_isolated_and_edited_file_is_not_executed(self):
        support=docker.DockerSupport(self.state,self.app);result=support.generate()
        content=result['content']
        self.assertIn('127.0.0.1:8877:8766',content)
        self.assertNotIn('docker.sock',content);self.assertNotIn(str(self.home),content)
        self.assertIn('sandbox-data:/data',content)
        self.assertIn('MCP-and-Skills.git#v2.0.0',content)
        self.assertEqual(support.project,support._owned()[2])
        support.compose.write_text(content+'\n# modified',encoding='utf-8')
        with mock.patch.object(support,'_call') as call,self.assertRaisesRegex(RuntimeError,'changed'):
            support.stop()
        call.assert_not_called()

    def test_docker_logs_redact_bearer_and_passwords(self):
        support=docker.DockerSupport(self.state,self.app);support.generate()
        sample='Authorization: Bearer fictional-credential\npassword=fictional-password'
        with mock.patch.object(support,'_call',return_value=subprocess.CompletedProcess([],0,sample,'')):
            text=support.logs()['logs']
        self.assertNotIn('fictional-credential',text);self.assertNotIn('fictional-password',text)


if __name__=='__main__':
    unittest.main()
