"""PWA publication, offline cache, and local session security checks."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest

import server
from test_manager import IsolatedManagerCase


class PWAHTTPTests(IsolatedManagerCase):
    def setUp(self):
        super().setUp()
        self.web = self.app / 'web'
        (self.web / 'index.html').write_text(
            '<!doctype html><html><head><!-- INVENTORY_BOOTSTRAP -->'
            '<script>window.INVENTORY_TOKEN="PRIVATE_FIXTURE";'
            'window.INVENTORY_SNAPSHOT={"name":"PERSONAL_INVENTORY"};</script>'
            '</head><body><script id="saved-inventory" type="application/json">'
            '{"mcp":[{"name":"PERSONAL_INVENTORY"}]}</script></body></html>', encoding='utf-8')
        for relative, _ in server.PWA_ASSETS.values():
            source = server.APP_DIR / 'web' / relative
            destination = self.web / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_file():
                shutil.copyfile(source, destination)
            else:
                destination.write_text('// fixture static asset', encoding='utf-8')
        shutil.copyfile(server.APP_DIR / 'web/service-worker.js', self.web / 'service-worker.js')
        self.selected_apps = []
        def availability(app='all'):
            self.selected_apps.append(app)
            return {'selectedApp': app, 'items': [], 'summary': {'missingCount': 0}}
        self.manager.availability = availability
        self.http = server.create_server(self.manager, port=0)
        self.port = self.http.server_address[1]
        self.origin = 'http://127.0.0.1:' + str(self.port)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .025}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            actual = {'Host': '127.0.0.1:' + str(self.port), **(headers or {})}
            connection.request('GET', path, headers=actual)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_session_is_memory_only_same_origin_and_never_cacheable(self):
        status, headers, body = self.request('/api/session')
        self.assertEqual(200, status)
        self.assertEqual({'token': self.manager.token}, json.loads(body))
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        for supplied in ({'Origin': 'null'}, {'Origin': 'https://outside.example'},
                         {'Sec-Fetch-Site': 'cross-site'}, {'Host': 'outside.example'}):
            with self.subTest(headers=supplied):
                status, _, body = self.request('/api/session', supplied)
                self.assertEqual(403, status)
                self.assertNotIn(self.manager.token.encode(), body)

    def test_offline_document_contains_neither_session_nor_personal_bootstrap(self):
        status, headers, body = self.request('/offline.html')
        self.assertEqual(200, status)
        text = body.decode()
        for private in ('PRIVATE_FIXTURE', 'PERSONAL_INVENTORY', 'INVENTORY_BOOTSTRAP',
                        'window.INVENTORY_TOKEN', 'window.INVENTORY_SNAPSHOT', self.manager.token):
            self.assertNotIn(private, text)
        self.assertIn('"mcp":[]', text)
        self.assertNotIn('nonce-', headers['Content-Security-Policy'])

    def test_worker_and_manifest_use_installable_routes_and_strict_csp(self):
        status, headers, body = self.request('/service-worker.js')
        self.assertEqual(200, status)
        self.assertEqual('/', headers['Service-Worker-Allowed'])
        self.assertTrue(headers['Content-Type'].startswith('text/javascript'))
        self.assertEqual('no-store', headers['Cache-Control'])
        self.assertIn("worker-src 'self'", headers['Content-Security-Policy'])
        self.assertIn("manifest-src 'self'", headers['Content-Security-Policy'])
        self.assertNotIn(b'__SHELL_REVISION__', body)
        status, headers, manifest = self.request('/manifest.webmanifest')
        self.assertEqual(200, status)
        self.assertTrue(headers['Content-Type'].startswith('application/manifest+json'))
        data = json.loads(manifest)
        self.assertEqual('/', data['start_url'])
        self.assertEqual('/', data['scope'])
        self.assertEqual('standalone', data['display'])
        self.assertTrue(data['name'])

    def test_shell_asset_changes_update_the_service_worker_fingerprint(self):
        _, _, before = self.request('/service-worker.js')
        (self.web / 'styles.css').write_text('/* new theme */', encoding='utf-8')
        _, _, after = self.request('/service-worker.js')
        self.assertNotEqual(before, after)

    def test_agent_selection_is_allowlisted_before_calling_discovery(self):
        for app in ('all', 'claude', 'codex', 'gemini'):
            status, _, body = self.request('/api/availability?app=' + app)
            self.assertEqual(200, status)
            self.assertEqual(app, json.loads(body)['selectedApp'])
        status, _, body = self.request('/api/availability')
        self.assertEqual(200, status)
        self.assertEqual('all', json.loads(body)['selectedApp'])
        calls = list(self.selected_apps)
        for query in ('?app=', '?app=unknown', '?app=claude&app=gemini', '?path=../../private', '?app=All'):
            with self.subTest(query=query):
                self.assertEqual(400, self.request('/api/availability' + query)[0])
        self.assertEqual(calls, self.selected_apps)

    def test_pwa_assets_do_not_enable_general_file_access(self):
        for path in ('/icons/../server.py', '/icons/%2e%2e/server.py', '/service-worker.js/../server.py',
                     '/.git/config', '/skill-updates-state.json'):
            with self.subTest(path=path):
                self.assertEqual(404, self.request(path)[0])

    def test_declared_png_icons_have_real_pixel_dimensions(self):
        manifest = json.loads((self.web / 'manifest.webmanifest').read_text(encoding='utf-8'))
        pngs = [icon for icon in manifest['icons'] if icon['type'] == 'image/png']
        self.assertTrue(any(icon.get('purpose') == 'maskable' for icon in pngs))
        self.assertTrue(any(icon['sizes'] == '192x192' for icon in pngs))
        self.assertTrue(any(icon['sizes'] == '512x512' for icon in pngs))
        for icon in pngs:
            with self.subTest(icon=icon['src']):
                status, headers, data = self.request(icon['src'])
                self.assertEqual(200, status)
                self.assertEqual('image/png', headers['Content-Type'])
                self.assertEqual(b'\x89PNG\r\n\x1a\n', data[:8])
                dimensions = struct.unpack('>II', data[16:24])
                self.assertEqual(tuple(map(int, icon['sizes'].split('x'))), dimensions)


class WorkerCachePolicyTests(unittest.TestCase):
    def test_live_html_api_tokens_and_mutations_never_enter_worker_cache(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is needed for a JavaScript service-worker sandbox')
        source = str(server.APP_DIR / 'web/service-worker.js')
        program = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const handlers={},writes=[],precache=[],responses=new Map();
let offline=false;
responses.set('/offline.html',new Response('GENERIC_OFFLINE_SHELL'));
responses.set('/app.js',new Response('CACHED_PUBLIC_JS'));
const cache={addAll:async requests=>precache.push(...requests.map(r=>r.url)),put:async(path,response)=>writes.push([path,await response.text()]),match:async path=>responses.get(path)?.clone()};
const self={location:{origin:'http://127.0.0.1:8766'},addEventListener:(type,fn)=>handlers[type]=fn,skipWaiting:async()=>{},clients:{claim:async()=>{}}};
const context={self,caches:{open:async()=>cache,keys:async()=>[],delete:async()=>true},URL,Response,Request:class{constructor(url){this.url=url;}},Set,fetch:async()=>{if(offline)throw Error('offline');return new Response('LIVE_TOKEN_AND_PERSONAL_BOOTSTRAP');}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
async function event(path,{method='GET',mode='cors'}={}){
 let promise=null;
 handlers.fetch({request:{url:new URL(path,self.location.origin).href,method,mode},respondWith:p=>{promise=p;}});
 return promise?await promise:null;
}
(async()=>{
 let install;
 handlers.install({waitUntil:p=>install=p});await install;
 for(const privatePath of ['/','/index.html','/api/inventory','/api/session','/snapshot.js','/snapshot.json'])assert(!precache.includes(privatePath));
 const live=await event('/',{mode:'navigate'});assert.equal(await live.text(),'LIVE_TOKEN_AND_PERSONAL_BOOTSTRAP');assert.equal(writes.length,0);
 for(const path of ['/api/session','/api/inventory','/snapshot.json','/private.json','https://outside.example/app.js','/app.js?token=DO_NOT_STORE'])assert.equal(await event(path),null);
 assert.equal(await event('/api/update',{method:'POST'}),null);
 assert.equal(await event('/app.js',{method:'POST'}),null);
 await event('/app.js');assert.equal(writes.length,1);assert.equal(writes[0][0],'/app.js');writes.length=0;
 offline=true;
 const saved=await event('/',{mode:'navigate'});assert.equal(await saved.text(),'GENERIC_OFFLINE_SHELL');
 const asset=await event('/app.js');assert.equal(await asset.text(),'CACHED_PUBLIC_JS');assert.equal(writes.length,0);
 console.log('Static-only PWA cache boundaries verified.');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        completed = subprocess.run([node, '-e', program, source], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == '__main__':
    unittest.main()
