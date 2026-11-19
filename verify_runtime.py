"""Read-only MCP handshake check. No tools are invoked and no credentials printed."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading

from manager_core import DEFAULT_STATE, HOME, MCPUpdater


def verify(key):
    updater = MCPUpdater(HOME, DEFAULT_STATE)
    config = next(server for app, _, _, servers in updater.configs()
                  for name, server in servers.items() if app == 'codex' and name == key)
    env = dict(os.environ, **config.get('env', {}))
    process = subprocess.Popen([config['command'], *config.get('args', [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding='utf-8', errors='replace', env=env, cwd=HOME,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            try:
                messages.put(json.loads(line))
            except ValueError:
                continue
        messages.put(None)
    threading.Thread(target=read, daemon=True).start()
    def send(value):
        process.stdin.write(json.dumps(value) + '\n')
        process.stdin.flush()
    def response(identifier):
        for _ in range(100):
            value = messages.get(timeout=45)
            if value is None:
                raise RuntimeError('The MCP process exited before replying')
            if value.get('id') == identifier:
                if 'error' in value:
                    raise RuntimeError('The MCP returned a protocol error')
                return value.get('result', {})
        raise RuntimeError('The MCP did not return the requested response')
    try:
        send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-03-26','capabilities':{},
            'clientInfo':{'name':'LocalInventoryVerification','version':'1.0'}}})
        initialized = response(1)
        send({'jsonrpc':'2.0','method':'notifications/initialized'})
        send({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
        tools = response(2).get('tools', [])
        if not tools:
            raise RuntimeError('The MCP returned no tools')
        return {'name':key,'connected':True,'version':initialized.get('serverInfo',{}).get('version'),
                'toolCount':len(tools)}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        process.stdin.close()
        process.stdout.close()


if __name__ == '__main__':
    results = []
    for name in ['chrome-devtools', 'rea']:
        try:
            results.append(verify(name))
        except Exception as exc:
            results.append({'name':name,'connected':False,'error':type(exc).__name__})
    print(json.dumps(results))
    raise SystemExit(0 if all(row['connected'] for row in results) else 1)
