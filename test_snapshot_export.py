"""Saved HTML remains self-contained after theme and PWA assets are added."""
from test_manager import IsolatedManagerCase


class SnapshotExportTests(IsolatedManagerCase):
    def assets(self, prefix=''):
        web = self.manager.app_dir / 'web'
        web.mkdir(parents=True, exist_ok=True)
        (web / 'icons').mkdir(exist_ok=True)
        (web / 'icons/icon.svg').write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32"/></svg>',
            encoding='utf-8')
        (web / 'styles.css').write_text('body { color: #123456; }', encoding='utf-8')
        (web / 'app.js').write_text('window.testInventoryLoaded = true;', encoding='utf-8')
        (web / 'theme.js').write_text('document.documentElement.dataset.theme = "dark";', encoding='utf-8')
        (web / 'pwa.js').write_text('throw new Error("PWA must not run in a file export");', encoding='utf-8')
        (web / 'index.html').write_text(
            '<!doctype html><html><head>'
            '<link rel="manifest" href="/manifest.webmanifest">'
            '<link rel="apple-touch-icon" href="/icons/apple-touch-icon.png">'
            '<link rel="icon" href="/icons/icon.svg">'
            '<script src="' + prefix + 'theme.js"></script>'
            '<link rel="stylesheet" href="' + prefix + 'styles.css">'
            '<!-- INVENTORY_BOOTSTRAP -->'
            '</head><body><script src="' + prefix + 'app.js" defer></script>'
            '<script src="' + prefix + 'pwa.js" defer></script></body></html>', encoding='utf-8')

    def test_all_asset_path_styles_produce_self_contained_html(self):
        for prefix in ['', '/', './']:
            with self.subTest(prefix=prefix):
                self.assets(prefix)
                self.manager.export_snapshot()
                html = (self.manager.export_dir / 'index.html').read_text(encoding='utf-8')
                self.assertNotRegex(html, r'<script[^>]+\bsrc=')
                self.assertNotRegex(html, r'<link[^>]+rel="(?:stylesheet|manifest|apple-touch-icon)"')
                self.assertIn('dataset.theme = "dark"', html)
                self.assertIn('color: #123456', html)
                self.assertIn('window.testInventoryLoaded', html)
                self.assertIn('data:image/svg+xml;base64,', html)
                self.assertIn('window.INVENTORY_AVAILABILITY=', html)
                self.assertNotIn('PWA must not run', html)
                self.assertNotIn(self.manager.token, html)
                self.assertFalse((self.manager.app_dir / 'index.html').exists())
                self.assertFalse((self.manager.app_dir / 'web/snapshot.json').exists())

    def test_snapshot_payload_cannot_close_its_script_or_capture_session_token(self):
        self.assets()
        self.manager.skills.rows = [{
            'name': 'fixture</script><script>unexpected()</script>',
            'sourceId': 'skill:fixture', 'sourceName': 'Fixture', 'apps': ['Codex']}]
        self.manager.export_snapshot()
        html = (self.manager.export_dir / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('</script><script>unexpected()', html)
        self.assertIn('<\\/script>', html)
        self.assertNotIn(self.manager.token, html)
        self.assertNotIn('window.INVENTORY_TOKEN=', html)
        snapshot = (self.manager.export_dir / 'snapshot.js').read_text(encoding='utf-8')
        self.assertNotIn(self.manager.token, snapshot)

    def test_original_desktop_inventory_is_backed_up_once(self):
        desktop = self.home / 'Desktop'
        desktop.mkdir()
        destination = desktop / 'MCP and Agent Skills.html'
        destination.write_text('Original local report', encoding='utf-8')
        self.manager.app_dir = desktop / 'MCP and Agent Skills'
        self.assets()
        self.manager.export_snapshot()
        backup = self.state / 'backups/original-desktop-inventory.html'
        self.assertEqual('Original local report', backup.read_text(encoding='utf-8'))
        self.assertIn('window.INVENTORY_AVAILABILITY=', destination.read_text(encoding='utf-8'))
        self.manager.export_snapshot()
        self.assertEqual('Original local report', backup.read_text(encoding='utf-8'))
