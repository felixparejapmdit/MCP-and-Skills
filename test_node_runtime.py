"""Independent, network-free checks for isolated Node distribution staging."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import manager_core as core


class NodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="inventory-node-test-")
        self.root = Path(self.temporary.name)
        self.updater = core.MCPUpdater(self.root / "home", self.root / "state")
        self.node_patch = mock.patch.object(self.updater, "_node", return_value="fixture-system-node.exe")
        self.node_patch.start()
        self.addCleanup(self.node_patch.stop)

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.root.name.startswith("inventory-node-test-"))
        self.temporary.cleanup()

    def archive(self, version="v24.30.0", extra=(), executable=b"fixture official executable"):
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w") as archive:
            archive.writestr("node-" + version + "-win-x64/node.exe", executable)
            for name, data in extra:
                archive.writestr(name, data)
        return content.getvalue()

    def install_fixtures(self, payload=None, manifest=None, index=None, staged_version="v24.30.0"):
        payload = self.archive() if payload is None else payload
        filename = "node-v24.30.0-win-x64.zip"
        digest = hashlib.sha256(payload).hexdigest()
        manifest = (digest + "  " + filename + "\n").encode() if manifest is None else manifest
        index = [
            {"version": "v26.14.0", "files": ["win-x64-zip"], "lts": "Future"},
            {"version": "v24.30.0", "files": ["win-x64-zip"], "lts": "Current"},
            {"version": "v24.22.1", "files": ["win-x64-zip"], "lts": "Current"},
            {"version": "v24.99.0-beta", "files": ["win-x64-zip"], "lts": False},
            {"version": "v28.1.0", "files": ["linux-x64"], "lts": False},
        ] if index is None else index

        def download(url, limit):
            if url.endswith("index.json"):
                return json.dumps(index).encode()
            if url.endswith("SHASUMS256.txt"):
                return manifest
            if url.endswith(filename):
                return payload
            raise AssertionError("Unexpected download: " + url)

        def command(args, **kwargs):
            self.assertEqual("--version", args[-1])
            if str(args[0]) == "fixture-system-node.exe":
                return "v24.16.0"
            return staged_version

        patches = [
            mock.patch.object(self.updater, "_node_download", side_effect=download),
            mock.patch.object(self.updater, "_engine_matches", side_effect=lambda versions, engine: [v for v in versions if core.version_tuple(v) >= (24, 22, 1)]),
            mock.patch.object(core, "run", side_effect=command),
            mock.patch("platform.machine", return_value="AMD64"),
        ]
        mocks = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        return mocks

    def test_current_compatible_node_does_not_download_or_create_runtime(self):
        with mock.patch.object(core, "run", return_value="v24.16.0"), \
                mock.patch.object(self.updater, "_engine_matches", return_value=["24.16.0"]), \
                mock.patch.object(self.updater, "_node_download") as download:
            self.assertEqual("fixture-system-node.exe", self.updater._node_for_engine(">=22"))
            download.assert_not_called()
        self.assertFalse((self.root / "state/runtimes").exists())

    def test_unrestricted_package_uses_current_node_without_semver_or_network(self):
        with mock.patch.object(core, "run", return_value="v24.16.0"), \
                mock.patch.object(self.updater, "_engine_matches") as matches, \
                mock.patch.object(self.updater, "_node_download") as download:
            self.assertEqual("fixture-system-node.exe", self.updater._node_for_engine(""))
            matches.assert_not_called()
            download.assert_not_called()

    def test_selects_current_major_stable_release_and_only_publishes_node(self):
        payload = self.archive(extra=[("node-v24.30.0-win-x64/README.md", b"other distribution content")])
        self.install_fixtures(payload=payload)
        progress = []
        selected = Path(self.updater._node_for_engine("^24.22.1 || >=26.12.1", progress.append))
        self.assertEqual(self.root / "state/runtimes/node/v24.30.0/node.exe", selected)
        self.assertEqual(b"fixture official executable", selected.read_bytes())
        self.assertEqual([selected], list(selected.parent.iterdir()))
        self.assertEqual(1, len(progress))
        self.assertFalse((self.root / "state/runtimes/node/v26.14.0").exists())

    def test_reuses_valid_staged_node_without_redownloading_archive(self):
        mocks = self.install_fixtures()
        expected = self.root / "state/runtimes/node/v24.30.0/node.exe"
        expected.parent.mkdir(parents=True)
        expected.write_bytes(b"existing compatible runtime")
        self.assertEqual(str(expected), self.updater._node_for_engine(">=24.22.1"))
        self.assertEqual(1, mocks[0].call_count)  # Release metadata only.
        self.assertEqual(b"existing compatible runtime", expected.read_bytes())

    def test_checksum_mismatch_never_creates_executable_or_runtime_directory(self):
        self.install_fixtures(manifest=("0" * 64 + "  node-v24.30.0-win-x64.zip\n").encode())
        with self.assertRaisesRegex(RuntimeError, "checksum verification"):
            self.updater._node_for_engine(">=24.22.1")
        self.assertFalse((self.root / "state/runtimes/node").exists())

    def test_checksum_must_match_exact_release_filename(self):
        payload = self.archive()
        manifest = (hashlib.sha256(payload).hexdigest() + "  node-v24.30.0-win-x64.zip.evil\n").encode()
        self.install_fixtures(payload=payload, manifest=manifest)
        with self.assertRaisesRegex(RuntimeError, "checksum was not found"):
            self.updater._node_for_engine(">=24.22.1")
        self.assertFalse((self.root / "state/runtimes/node").exists())

    def test_parent_traversal_is_rejected_even_with_correct_archive_hash(self):
        self.install_fixtures(payload=self.archive(extra=[("node-v24.30.0-win-x64/../../outside.exe", b"malicious")]))
        with self.assertRaisesRegex(RuntimeError, "unsafe path"):
            self.updater._node_for_engine(">=24.22.1")
        self.assertFalse((self.root / "outside.exe").exists())
        self.assertFalse((self.root / "state/runtimes/node").exists())

    def test_absolute_and_drive_paths_are_rejected(self):
        for index, name in enumerate(["/node-v24.30.0-win-x64/absolute", "node-v24.30.0-win-x64/C:/outside"]):
            with self.subTest(name=name):
                payload = self.archive(extra=[(name, b"malicious")])
                mocks = self.install_fixtures(payload=payload)
                with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                    self.updater._node_for_engine(">=24.22.1")
                self.assertFalse((self.root / "state/runtimes/node").exists())

    def test_zip_symbolic_links_are_rejected(self):
        link = zipfile.ZipInfo("node-v24.30.0-win-x64/link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        self.install_fixtures(payload=self.archive(extra=[(link, b"../../outside")]))
        with self.assertRaisesRegex(RuntimeError, "unsafe path"):
            self.updater._node_for_engine(">=24.22.1")
        self.assertFalse((self.root / "state/runtimes/node").exists())

    def test_wrong_reported_version_does_not_publish_staged_executable(self):
        self.install_fixtures(staged_version="v24.22.1")
        with self.assertRaisesRegex(RuntimeError, "did not match"):
            self.updater._node_for_engine(">=24.22.1")
        directory = self.root / "state/runtimes/node/v24.30.0"
        self.assertEqual([], list(directory.iterdir()))

    def test_download_rejects_unregistered_hosts_before_network(self):
        with mock.patch.object(core.urllib.request, "urlopen") as network:
            for url in ["http://nodejs.org/dist/index.json", "https://nodejs.org.evil.test/dist/index.json", "https://other.test/dist/index.json"]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    core.MCPUpdater._node_download(url, 64)
            network.assert_not_called()

    def test_download_limit_is_enforced(self):
        with mock.patch.object(core.urllib.request, "urlopen", return_value=io.BytesIO(b"123456789")):
            with self.assertRaisesRegex(RuntimeError, "download limit"):
                core.MCPUpdater._node_download("https://nodejs.org/dist/index.json", 8)

    def test_deep_chrome_launcher_resolves_its_actual_version_and_preserves_flags(self):
        package = self.root / "runtime/node_modules/chrome-devtools-mcp"
        launcher = package / "out/Default/gen/src/bin/chrome-devtools-mcp.js"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("// Fixture package launcher", encoding="utf-8")
        core.write_json(package / "package.json", {
            "name": "chrome-devtools-mcp", "version": "1.13.0",
            "bin": {"chrome-devtools-mcp": "out/Default/gen/src/bin/chrome-devtools-mcp.js"},
        })
        entry = {"command": "node.exe", "args": [str(launcher), "--quiet", "--headless"]}
        core.write_json(self.root / "home/.claude.json", {"mcpServers": {"chrome-devtools": entry}})
        self.assertEqual("1.13.0", self.updater.installed_version("chrome-devtools"))
        self.assertEqual(["--quiet", "--headless"], self.updater._runtime_tail("chrome-devtools", entry))


if __name__ == "__main__":
    unittest.main()
