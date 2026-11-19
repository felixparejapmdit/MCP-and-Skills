"""Loopback-only live dashboard for the global MCP and skill inventory."""
from __future__ import annotations

import argparse
import atexit
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit

from manager_core import APP_DIR, DEFAULT_STATE, HOME, Manager, VERSION, read_json, write_json


class InventoryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, manager):
        self.manager = manager
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
        return {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}

    def secure_request(self, mutation=False):
        host = self.headers.get('Host', '').lower()
        port = self.server.server_address[1]
        if host not in {f'127.0.0.1:{port}', f'localhost:{port}'}:
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

    def send_bytes(self, body, content_type, status=200, nonce=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        script = "'self'" + (" 'nonce-" + nonce + "'" if nonce else '')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src " + script + "; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, value, status=200):
        self.send_bytes(json.dumps(self.manager.sanitize(value), ensure_ascii=True).encode('utf-8'), 'application/json; charset=utf-8', status)

    def do_GET(self):
        if not self.secure_request():
            return
        path = urlsplit(self.path).path
        try:
            if path in ('/health', '/api/health'):
                return self.json_response({'application': 'AgentInventoryManager', 'version': VERSION, 'running': True})
            if path == '/api/inventory':
                return self.json_response(self.manager.inventory())
            if path == '/api/status':
                return self.json_response(self.manager.status())
            if path == '/api/updates':
                return self.json_response({'items': self.manager.state.get('updates', []), 'checkedAt': self.manager.state.get('lastCheck')})
            if path == '/api/suggestions':
                return self.json_response(self.manager.suggestions())
            if path == '/api/activity':
                return self.json_response({'items': self.manager.state.get('activity', [])})
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
            assets = {'/app.js': 'app.js', '/styles.css': 'styles.css', '/snapshot.js': 'snapshot.js', '/snapshot.json': 'snapshot.json'}
            if path in assets:
                source = self.manager.app_dir / 'web' / assets[path]
                if source.is_file():
                    content_type = mimetypes.guess_type(source.name)[0] or 'application/octet-stream'
                    return self.send_bytes(source.read_bytes(), content_type + '; charset=utf-8')
            return self.json_response({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            return
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


def create_server(manager, port=8766):
    return InventoryHTTPServer(('127.0.0.1', port), manager)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--once', action='store_true', help='Check and apply supported updates once')
    parser.add_argument('--no-scheduler', action='store_true', help='For controlled local verification')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    manager = Manager(state_dir=args.state_dir)
    if args.once:
        manager.start_job('check')
        while manager.job and manager.job['status'] == 'running':
            time.sleep(.25)
        print(json.dumps(manager.status(), indent=2))
        return 0 if manager.job['status'] == 'done' else 1
    server = create_server(manager, args.port)
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
