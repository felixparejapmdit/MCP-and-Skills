"""Independent manager/HTTP regression checks using isolated homes and no network."""
from __future__ import annotations

import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import tomllib
import types
import unittest
from unittest import mock

import manager_core as core
import server


SECRET = "fixture-provider-secret-9234781"


class FakeSkillUpdater:
    def __init__(self, home, state_dir, log=None):
        self.rows = []
        self.units = []
        self.applied = []

    def discover(self):
        return copy.deepcopy(self.rows)

    def check_all(self, progress=None):
        return copy.deepcopy(self.units)

    def apply(self, unit_id, progress=None):
        self.applied.append(unit_id)
        return {"id": unit_id, "status": "updated"}


class IsolatedManagerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="inventory-manager-test-")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.app = self.root / "app"
        self.home.mkdir()
        (self.app / "web").mkdir(parents=True)
        (self.app / "web/index.html").write_text(
            "<!doctype html><html><head><!-- INVENTORY_BOOTSTRAP -->"
            "</head><body>Fixture dashboard</body></html>", encoding="utf-8")
        core.write_json(self.app / "suggestions.json", [])
        fake_module = types.ModuleType("skill_updates")
        fake_module.SkillUpdater = FakeSkillUpdater
        with mock.patch.dict(sys.modules, {"skill_updates": fake_module}):
            self.manager = core.Manager(self.home, self.state, self.app)
        self.manager.settings["autoUpdate"] = False

    def tearDown(self):
        # The only recursive cleanup target is the verified, isolated test root.
        self.assertTrue(self.root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertTrue(self.root.name.startswith("inventory-manager-test-"))
        self.temporary.cleanup()

    def fixture_configs(self, multiline_codex=False):
        allowed = str(self.home / "projects")
        (self.home / "projects").mkdir(exist_ok=True)
        package = "@modelcontextprotocol/server-filesystem@1.0.0"
        entry = {
            "type": "stdio", "command": "cmd",
            "args": ["/c", "npx", "-y", package, allowed],
            "env": {"FIXTURE_API_KEY": SECRET},
        }
        core.write_json(self.home / ".claude.json", {
            "mcpServers": {
                "filesystem": copy.deepcopy(entry),
                "github": {"type": "http", "url":
                    "https://user:" + SECRET + "@api.example.test/mcp?token=" + SECRET,
                    "headers": {"Authorization": "Bearer " + SECRET}},
            }, "unrelatedSetting": {"keep": [1, 2, 3]},
        })
        (self.home / ".codex").mkdir(exist_ok=True)
        args = json.dumps(entry["args"])
        if multiline_codex:
            args = "[\n" + "\n".join("  " + json.dumps(x) + "," for x in entry["args"]) + "\n]"
        (self.home / ".codex/config.toml").write_text(
            'model = "fixture-model"\n\n[mcp_servers.filesystem]\n'
            'command = "cmd"\nargs = ' + args + '\n'
            'startup_timeout_sec = 30\n\n[mcp_servers.filesystem.env]\n'
            'FIXTURE_API_KEY = ' + json.dumps(SECRET) + '\n\n'
            '[projects."fixture-project"]\ntrust_level = "trusted"\n', encoding="utf-8")
        core.write_json(self.home / ".gemini/settings.json", {
            "theme": "preserve-me", "mcpServers": {"filesystem": copy.deepcopy(entry)},
        })
        return entry

    def config_bytes(self):
        paths = [self.home / ".claude.json", self.home / ".codex/config.toml",
                 self.home / ".gemini/settings.json"]
        return {p: p.read_bytes() for p in paths}


class ManagerCoreTests(IsolatedManagerCase):
    def test_inventory_never_exposes_provider_credentials(self):
        self.fixture_configs()
        text = json.dumps(self.manager.inventory())
        self.assertNotIn(SECRET, text)
        self.assertNotIn("?token=", text)
        self.assertNotIn("user:", text)
        self.assertIn("https://api.example.test/mcp", text)

    def test_settings_validate_types_and_preserve_valid_state(self):
        original = dict(self.manager.settings)
        for payload in ({"intervalHours": True}, {"intervalHours": 1},
                        {"autoUpdate": "true"}, {"unknown": True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.manager.set_settings(payload)
        self.assertEqual(original, self.manager.settings)
        result = self.manager.set_settings({"autoUpdate": True, "intervalHours": 12})
        self.assertEqual({"autoUpdate": True, "intervalHours": 12}, result)
        self.assertEqual(result, core.read_json(self.state / "settings.json"))

    def test_unknown_job_ids_do_not_start_a_thread_or_network(self):
        self.manager.state["updates"] = [{"id": "mcp:filesystem"}]
        with mock.patch.object(core.threading, "Thread") as thread, \
                mock.patch.object(core.urllib.request, "urlopen") as network:
            with self.assertRaises(ValueError):
                self.manager.start_job("update", ["mcp:unregistered"])
            thread.assert_not_called()
            network.assert_not_called()
        self.assertIsNone(self.manager.job)

    def test_explicit_empty_update_selection_does_not_mean_update_all(self):
        self.manager.state["updates"] = [{"id": "mcp:filesystem"}]
        with mock.patch.object(core.threading, "Thread") as thread:
            with self.assertRaises(ValueError):
                self.manager.start_job("update", [])
            thread.assert_not_called()

    def test_suggestions_hide_installed_aliases_and_refresh_when_inventory_changes(self):
        core.write_json(self.home / ".claude.json", {"mcpServers": {
            "context7": {"type": "http", "url": "https://example.test/mcp"}}})
        self.manager.skills.rows = [{"name": "Frontend_Design", "sourceId": "skill:fixture",
                                    "sourceName": "Fixture", "apps": ["Codex", "Gemini"]}]
        candidates = [
            {"id": "context7-docs", "name": "Context7 Documentation", "kind": "mcp",
             "matchNames": ["Context-7"]},
            {"id": "frontend", "name": "Frontend design", "kind": "skill",
             "matchNames": ["frontend-design"]},
            {"id": "new-search", "name": "Brave Search", "kind": "mcp",
             "matchNames": ["brave-search"]},
        ]
        core.write_json(self.app / "suggestions.json", candidates)
        self.assertEqual(["new-search"], [x["id"] for x in self.manager.suggestions()["items"]])
        current = core.read_json(self.home / ".claude.json")
        current["mcpServers"]["brave_search"] = {"type": "http", "url": "https://example.test/brave"}
        core.write_json(self.home / ".claude.json", current)
        self.assertEqual([], self.manager.suggestions()["items"])

    def test_unknown_or_uninstalled_mcp_updates_are_rejected_before_network(self):
        with mock.patch.object(core.urllib.request, "urlopen") as network, \
                mock.patch.object(core, "run") as command:
            for unit_id in ("mcp:invented", "skill:filesystem", "mcp:filesystem", "../../evil"):
                with self.subTest(unit_id=unit_id), self.assertRaises(ValueError):
                    self.manager.mcp.apply(unit_id)
            network.assert_not_called()
            command.assert_not_called()

    def test_config_rewrite_preserves_secrets_arguments_and_unrelated_fields(self):
        self.fixture_configs()
        before = {app: copy.deepcopy(data) for app, _, data, _ in self.manager.mcp.configs()}
        entry = self.root / "runtime/server.js"
        entry.parent.mkdir()
        entry.write_text("// fixture server", encoding="utf-8")
        self.assertEqual(3, self.manager.mcp._rewrite("filesystem", "fixture-node", entry))
        for app, _, data, _ in self.manager.mcp.configs():
            expected = copy.deepcopy(before[app])
            group = "mcp_servers" if app == "codex" else "mcpServers"
            expected[group]["filesystem"]["command"] = "fixture-node"
            expected[group]["filesystem"]["args"] = [str(entry), str(self.home / "projects")]
            self.assertEqual(expected, data)
        backups = list((self.state / "backups").glob("mcp-filesystem-*/*"))
        self.assertEqual(3, len(backups))

    def test_config_rewrite_supports_valid_multiline_toml_arguments(self):
        self.fixture_configs(multiline_codex=True)
        before = tomllib.loads((self.home / ".codex/config.toml").read_text())
        entry = self.root / "server.js"
        entry.write_text("// fixture server", encoding="utf-8")
        self.manager.mcp._rewrite("filesystem", "fixture-node", entry)
        after = tomllib.loads((self.home / ".codex/config.toml").read_text())
        before["mcp_servers"]["filesystem"]["command"] = "fixture-node"
        before["mcp_servers"]["filesystem"]["args"] = [str(entry), str(self.home / "projects")]
        self.assertEqual(before, after)

    def test_config_transaction_rolls_back_first_file_when_second_replace_fails(self):
        self.fixture_configs()
        originals = self.config_bytes()
        second = self.home / ".codex/config.toml"
        real_replace = core.os.replace

        def fail_second(source, destination):
            if Path(destination) == second:
                raise OSError("fixture replacement failure")
            return real_replace(source, destination)

        with mock.patch.object(core.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                self.manager.mcp._rewrite("filesystem", "fixture-node", self.root / "server.js")
        for path, content in originals.items():
            self.assertEqual(content, path.read_bytes())

    def test_concurrent_user_edit_survives_transaction_abort(self):
        self.fixture_configs()
        originals = self.config_bytes()
        first, second = self.home / ".claude.json", self.home / ".codex/config.toml"
        real_replace = core.os.replace
        external = b'model = "edited-by-user"\n'

        def edit_after_first(source, destination):
            result = real_replace(source, destination)
            if Path(destination) == first:
                second.write_bytes(external)
            return result

        with mock.patch.object(core.os, "replace", side_effect=edit_after_first):
            with self.assertRaises(RuntimeError):
                self.manager.mcp._rewrite("filesystem", "fixture-node", self.root / "server.js")
        self.assertEqual(originals[first], first.read_bytes())
        self.assertEqual(external, second.read_bytes())
        self.assertEqual(originals[self.home / ".gemini/settings.json"],
                         (self.home / ".gemini/settings.json").read_bytes())

    def test_unrecognized_custom_launcher_is_not_replaced(self):
        self.fixture_configs()
        original = core.read_json(self.home / ".claude.json")
        custom = self.home / "custom-server.js"
        custom.write_text("// User's independent server adapter", encoding="utf-8")
        original["mcpServers"]["filesystem"]["command"] = "node"
        original["mcpServers"]["filesystem"]["args"] = [str(custom), "--keep-custom"]
        core.write_json(self.home / ".claude.json", original)
        before = self.config_bytes()
        with self.assertRaises(RuntimeError):
            self.manager.mcp._rewrite("filesystem", "fixture-node", self.root / "new-server.js")
        self.assertEqual(before, self.config_bytes())

    def test_staged_package_cannot_escape_its_own_launcher_directory(self):
        self.fixture_configs()
        updater = self.manager.mcp
        updater.checks["filesystem"] = {"version": "9.0.0", "metadata": {}}
        originals = self.config_bytes()

        def staged_package(args, **kwargs):
            if "install" in args:
                prefix = Path(args[args.index("--prefix") + 1])
                package = prefix / "node_modules/@modelcontextprotocol/server-filesystem"
                package.mkdir(parents=True)
                core.write_json(package / "package.json", {
                    "name": "@modelcontextprotocol/server-filesystem", "version": "9.0.0",
                    "bin": "../../../escaped.js"})
                (prefix / "escaped.js").write_text("// Outside the package", encoding="utf-8")
            return ""

        with mock.patch.object(updater, "_npm", return_value="fixture-npm"), \
                mock.patch.object(core, "run", side_effect=staged_package), \
                mock.patch.object(core.urllib.request, "urlopen") as network:
            with self.assertRaises(RuntimeError):
                updater.apply("mcp:filesystem")
            network.assert_not_called()
        self.assertEqual(originals, self.config_bytes())


class HTTPContractTests(IsolatedManagerCase):
    def setUp(self):
        super().setUp()
        self.fixture_configs()
        self.http = server.create_server(self.manager, port=0)
        self.port = self.http.server_address[1]
        self.origin = "http://127.0.0.1:" + str(self.port)
        self.thread = threading.Thread(
            target=self.http.serve_forever, kwargs={"poll_interval": .025}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, method, path, payload=None, headers=None, authenticated=True):
        actual_headers = {"Host": "127.0.0.1:" + str(self.port)}
        body = None
        if method == "POST":
            actual_headers.update({"Origin": self.origin, "Content-Type": "application/json"})
            if authenticated:
                actual_headers["X-Inventory-Token"] = self.manager.token
            body = json.dumps({} if payload is None else payload).encode()
        actual_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=actual_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def test_all_get_api_payloads_and_html_exclude_provider_secrets(self):
        for path in ("/", "/api/inventory", "/api/status", "/api/updates",
                     "/api/suggestions", "/api/activity"):
            with self.subTest(path=path):
                status, headers, text = self.request("GET", path)
                self.assertEqual(200, status)
                self.assertNotIn(SECRET, text)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        _, headers, html = self.request("GET", "/")
        self.assertIn(self.manager.token, html)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("nonce=", html)

    def test_non_loopback_host_cannot_read_dashboard_token(self):
        for host in ("evil.example", "127.0.0.1.evil.example:" + str(self.port), "localhost:1"):
            with self.subTest(host=host):
                status, _, body = self.request("GET", "/", headers={"Host": host})
                self.assertEqual(403, status)
                self.assertNotIn(self.manager.token, body)

    def test_mutation_requires_token_same_origin_and_json(self):
        cases = [
            ({}, False, 403),
            ({"X-Inventory-Token": "wrong"}, True, 403),
            ({"Origin": "https://evil.example"}, True, 403),
            ({"Origin": "null"}, True, 403),
            ({"Sec-Fetch-Site": "cross-site"}, True, 403),
            ({"Content-Type": "text/plain"}, True, 415),
        ]
        before = dict(self.manager.settings)
        for headers, auth, expected in cases:
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/settings", {"autoUpdate": True},
                                             headers=headers, authenticated=auth)
                self.assertEqual(expected, status)
        self.assertEqual(before, self.manager.settings)
        status, _, _ = self.request("POST", "/api/settings", {"autoUpdate": True})
        self.assertEqual(200, status)
        self.assertTrue(self.manager.settings["autoUpdate"])

    def test_update_unknown_id_is_a_client_error_without_work(self):
        self.manager.state["updates"] = [{"id": "mcp:filesystem"}]
        with mock.patch.object(self.manager, "_worker") as worker:
            status, _, _ = self.request("POST", "/api/update", {"ids": ["mcp:fake"]})
            self.assertEqual(400, status)
            worker.assert_not_called()

    def test_only_whitelisted_static_paths_are_served(self):
        for path in ("/../.claude.json", "/%2e%2e/.claude.json", "/web/../../.claude.json"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(404, status)
                self.assertNotIn(SECRET, body)

    def test_provider_error_messages_are_sanitized_in_all_public_views(self):
        self.manager.log("Request failed for https://api.example.test/mcp?token=" + SECRET)
        self.manager.state["updates"] = [{"id": "mcp:filesystem", "name": "Filesystem",
            "status": "error", "detail": "Provider failed with key=" + SECRET}]
        self.manager.job = {"status": "error", "error": "Provider failure: " + SECRET}
        for path in ("/api/status", "/api/updates", "/api/activity"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(200, status)
                self.assertNotIn(SECRET, body)


if __name__ == "__main__":
    unittest.main()
