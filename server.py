"""Loopback-only live dashboard for the global MCP and skill inventory."""
from __future__ import annotations

import argparse
import atexit
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from manager_core import APP_DIR, DEFAULT_STATE, HOME, Manager, VERSION, read_json, write_json


# Only these public assets can be served or cached by the PWA. Local snapshots,
# credentials and API responses are deliberately outside the offline shell.
PWA_ASSETS = {
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
    '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
    '/theme.js': ('theme.js', 'text/javascript; charset=utf-8'),
    '/pwa.js': ('pwa.js', 'text/javascript; charset=utf-8'),
    '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json; charset=utf-8'),
    '/icons/icon.svg': ('icons/icon.svg', 'image/svg+xml'),
    '/icons/icon-192.png': ('icons/icon-192.png', 'image/png'),
    '/icons/icon-512.png': ('icons/icon-512.png', 'image/png'),
    '/icons/icon-maskable-512.png': ('icons/icon-maskable-512.png', 'image/png'),
    '/icons/apple-touch-icon.png': ('icons/apple-touch-icon.png', 'image/png'),
}


def offline_document(web):
    """Return the generic shell without any live bootstrap or personal data."""
    text = (Path(web) / 'index.html').read_text(encoding='utf-8').replace('<!-- INVENTORY_BOOTSTRAP -->', '')
    # Defense in depth if a generated page is ever copied over the template.
    text = re.sub(r'<script\b[^>]*>.*?</script\s*>',
                  lambda match: '' if re.search(r'window\.INVENTORY_(?:TOKEN|SNAPSHOT|FALLBACK|AVAILABILITY|CATALOG)', match[0]) else match[0],
                  text, flags=re.I | re.S)
    return re.sub(r'(<script\b[^>]*\bid=[\"\']saved-inventory[\"\'][^>]*>).*?(</script\s*>)',
                  lambda match: match[1] + '{"mcp":[],"skillGroups":[],"stats":{},"applications":[],"plugins":[]}' + match[2],
                  text, flags=re.I | re.S)


def service_worker_source(web):
    """Changing a shell asset changes the worker bytes and its cache version."""
    web = Path(web)
    fingerprint = hashlib.sha256(offline_document(web).encode('utf-8'))
    for relative, _ in sorted(PWA_ASSETS.values()):
        source = web / relative
        fingerprint.update(relative.encode('utf-8'))
        if source.is_file():
            fingerprint.update(source.read_bytes())
    return (web / 'service-worker.js').read_text(encoding='utf-8').replace('__SHELL_REVISION__', fingerprint.hexdigest()[:16])


class InventoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager, public_origin=None):
        self.manager = manager
        self.public_origin = public_origin
        super().__init__(address, InventoryHandler)


class InventoryHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):
        # Request paths, headers and body are deliberately not logged.
        pass

    @property
    def manager(self):
        return self.server.manager

    def expected_origins(self):
        port = self.server.server_address[1]
        origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
        if self.server.public_origin:
            origins.add(self.server.public_origin)
        return origins

    def secure_request(self, mutation=False):
        host = self.headers.get('Host', '').lower()
        port = self.server.server_address[1]
        if host not in {urlsplit(origin).netloc for origin in self.expected_origins()}:
            self.json_response({'error': 'Unrecognized local host'}, 403)
            return False
        origin = self.headers.get('Origin')
        if origin is not None and origin not in self.expected_origins():
            self.json_response({'error': 'Only the local dashboard can make this request'}, 403)
            return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.json_response({'error': 'Cross-site requests are not accepted'}, 403)
            return False
        if mutation:
            content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
            if content_type != 'application/json':
                self.json_response({'error': 'JSON is required'}, 415)
                return False
            token = self.headers.get('X-Inventory-Token', '')
            if not hmac.compare_digest(token, self.manager.token):
                self.json_response({'error': 'Open the local dashboard and retry'}, 403)
                return False
        return True

    def send_bytes(self, body, content_type, status=200, nonce=None, extra_headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        script = "'self'" + (" 'nonce-" + nonce + "'" if nonce else '')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src " + script + "; style-src 'self'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, value, status=200):
        self.send_bytes(json.dumps(self.manager.sanitize(value), ensure_ascii=True).encode('utf-8'), 'application/json; charset=utf-8', status)

    def do_GET(self):
        if not self.secure_request():
            return
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
        try:
            if path in ('/health', '/api/health'):
                return self.json_response({'application': 'AgentInventoryManager', 'version': VERSION, 'running': True})
            if path == '/api/inventory':
                return self.json_response(self.manager.inventory())
            if path == '/api/catalog':
                query = parse_qs(parsed_url.query, keep_blank_values=True)
                apps = query.get('app', ['all'])
                if set(query) - {'app'} or len(apps) != 1 or apps[0] not in {'all', 'claude', 'codex', 'gemini'}:
                    return self.json_response({'error': 'Choose all, claude, codex, or gemini'}, 400)
                return self.json_response(self.manager.catalog(apps[0]))
            if path == '/api/install/status':
                return self.json_response(self.manager.operations.status('install'))
            if path == '/api/install/history':
                return self.json_response(self.manager.install_history())
            if path == '/api/docker':
                return self.json_response(self.manager.docker().check())
            if path == '/api/docker/status':
                return self.json_response(self.manager.operations.status('docker'))
            if path == '/api/docker/logs':
                return self.json_response(self.manager.docker().logs())
            if path == '/api/runtime':
                from management_jobs import runtime_summary
                return self.json_response(runtime_summary(self.manager))
            if path == '/api/session':
                # This token is held only in live page memory. It lets a generic
                # offline shell reconnect after the helper has restarted.
                return self.json_response({'token': self.manager.token})
            if path == '/api/availability':
                query = parse_qs(parsed_url.query, keep_blank_values=True)
                apps = query.get('app', ['all'])
                if set(query) - {'app'} or len(apps) != 1 or apps[0] not in {'all', 'claude', 'codex', 'gemini'}:
                    return self.json_response({'error': 'Choose all, claude, codex, or gemini'}, 400)
                return self.json_response(self.manager.availability(apps[0]))
            if path == '/api/status':
                return self.json_response(self.manager.status())
            if path == '/api/updates':
                return self.json_response({'items': self.manager.state.get('updates', []), 'checkedAt': self.manager.state.get('lastCheck')})
            if path == '/api/suggestions':
                return self.json_response(self.manager.suggestions())
            if path == '/api/activity':
                return self.json_response({'items': self.manager.state.get('activity', [])})
            web = self.manager.app_dir / 'web'
            if path == '/offline.html':
                return self.send_bytes(offline_document(web).encode('utf-8'), 'text/html; charset=utf-8')
            if path == '/service-worker.js':
                return self.send_bytes(service_worker_source(web).encode('utf-8'), 'text/javascript; charset=utf-8',
                                       extra_headers={'Service-Worker-Allowed': '/'})
            if path in ('/', '/index.html'):
                source = self.manager.app_dir / 'web/index.html'
                if not source.is_file():
                    return self.json_response({'error': 'The dashboard is still being prepared'}, 503)
                text = source.read_text(encoding='utf-8')
                nonce = secrets.token_urlsafe(20)
                snapshot = json.dumps(self.manager.inventory(), ensure_ascii=True).replace('</', '<\\/')
                boot = '<script nonce="' + nonce + '">window.INVENTORY_TOKEN=' + json.dumps(self.manager.token) + ';window.INVENTORY_SNAPSHOT=' + snapshot + ';</script>'
                if '<!-- INVENTORY_BOOTSTRAP -->' in text:
                    text = text.replace('<!-- INVENTORY_BOOTSTRAP -->', boot, 1)
                else:
                    text = text.replace('</head>', boot + '</head>', 1)
                return self.send_bytes(text.encode('utf-8'), 'text/html; charset=utf-8', nonce=nonce)
            assets = {**PWA_ASSETS, '/snapshot.js': ('snapshot.js', 'text/javascript; charset=utf-8'),
                      '/snapshot.json': ('snapshot.json', 'application/json; charset=utf-8')}
            if path in assets:
                relative, content_type = assets[path]
                source = (self.manager.export_dir / relative) if path in {'/snapshot.js', '/snapshot.json'} else web / relative
                if source.is_file():
                    return self.send_bytes(source.read_bytes(), content_type)
            return self.json_response({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except ValueError:
            return self.json_response({'error': 'Invalid or unknown request parameters'}, 400)
        except Exception:
            return self.json_response({'error': 'The inventory could not be read. Check local activity or restart the helper.'}, 500)

    def do_OPTIONS(self):
        # No CORS API is exposed to arbitrary web pages or file:// origins.
        if self.secure_request():
            self.json_response({'error': 'Cross-origin access is not enabled'}, 403)

    def do_POST(self):
        if not self.secure_request(mutation=True):
            self.close_connection = True
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0 or length > 65536:
                return self.json_response({'error': 'Request body too large'}, 413)
            raw = self.rfile.read(length)
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError('A JSON object is required')
            path = urlsplit(self.path).path
            if path == '/api/install/plan':
                return self.json_response(self.manager.install_plan(payload))
            if path == '/api/install/execute':
                if set(payload) != {'planId'}:
                    raise ValueError('Choose a validated installation plan')
                return self.json_response({'job': self.manager.execute_install(payload['planId'])}, 202)
            if path == '/api/docker/generate':
                if set(payload) - {'mode'} or payload.get('mode', 'sandbox') != 'sandbox':
                    raise ValueError('Only the isolated developer sandbox is supported')
                return self.json_response(self.manager.docker().generate())
            if path in {'/api/docker/start', '/api/docker/stop'}:
                if payload:
                    raise ValueError('Docker actions do not accept custom commands')
                return self.json_response({'job': self.manager.docker_action(path.rsplit('/', 1)[-1])}, 202)
            if path == '/api/runtime/startup':
                if set(payload) != {'enabled'} or type(payload['enabled']) is not bool:
                    raise ValueError('Choose whether to start at sign-in')
                if self.manager.home.resolve() != Path.home().resolve():
                    raise ValueError('Autostart is only available for the current native user')
                from launcher import install_startup, uninstall_startup
                result = install_startup(self.manager.state_dir, self.server.server_address[1]) if payload['enabled'] else uninstall_startup()
                return self.json_response(result or {'enabled': payload['enabled']})
            if path == '/api/helper/stop':
                if payload:
                    raise ValueError('Stop does not accept options')
                if ((self.manager.job and self.manager.job.get('status') == 'running') or self.manager.operations.running()):
                    return self.json_response({'error': 'Wait for the active operation before stopping the helper'}, 409)
                self.json_response({'stopping': True}, 202)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == '/api/check-updates':
                if payload:
                    raise ValueError('This endpoint does not accept options')
                return self.json_response({'job': self.manager.start_job('check')}, 202)
            if path == '/api/update':
                if set(payload) - {'ids'}:
                    raise ValueError('Unknown update option')
                return self.json_response({'job': self.manager.start_job('update', payload.get('ids'))}, 202)
            if path == '/api/settings':
                return self.json_response({'settings': self.manager.set_settings(payload)})
            return self.json_response({'error': 'Not found'}, 404)
        except (ValueError, json.JSONDecodeError):
            return self.json_response({'error': 'Invalid or unknown request parameters'}, 400)
        except RuntimeError:
            return self.json_response({'error': 'An update job is already running. Follow its progress in the dashboard.'}, 409)
        except Exception:
            return self.json_response({'error': 'The request could not be completed'}, 500)


def create_server(manager, port=8766, bind='127.0.0.1', public_origin=None):
    if bind not in {'127.0.0.1', 'localhost', '0.0.0.0'}:
        raise ValueError('Choose a supported bind address')
    if bind == '0.0.0.0' and os.environ.get('MCP_SKILLS_CONTAINER') != '1':
        raise ValueError('Public binding is only available in the explicit container sandbox')
    if public_origin is not None:
        parts = urlsplit(public_origin)
        if (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost'} or
            not parts.port or parts.username or parts.password or parts.path or parts.query or parts.fragment):
            raise ValueError('The public origin must be an exact loopback HTTP origin')
    return InventoryHTTPServer((bind, port), manager, public_origin)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--once', action='store_true', help='Check and apply supported updates once')
    parser.add_argument('--no-scheduler', action='store_true', help='For controlled local verification')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--home', type=Path, default=HOME, help='Agent profile home (isolated in Docker)')
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--public-origin', help='Explicit loopback published origin for a Docker NAT port')
    args = parser.parse_args(argv)
    manager = Manager(home=args.home, state_dir=args.state_dir)
    if args.once:
        manager.start_job('check')
        while manager.job and manager.job['status'] == 'running':
            time.sleep(.25)
        print(json.dumps(manager.status(), indent=2))
        return 0 if manager.job['status'] == 'done' else 1
    server = create_server(manager, args.port, args.bind, args.public_origin)
    manager.export_snapshot()
    stop = threading.Event()
    atexit.register(stop.set)
    write_json(args.state_dir / 'runtime.json', {'pid': __import__('os').getpid(), 'port': server.server_address[1], 'startedAt': now_for_runtime()})
    if not args.no_scheduler:
        threading.Thread(target=manager.scheduler, args=(stop,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


def now_for_runtime():
    from manager_core import now
    return now()


if __name__ == '__main__':
    raise SystemExit(main())
