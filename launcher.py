"""Native entry point: start the local app from a shortcut or any directory."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from platform_support import resources_dir, user_state_dir

APPLICATION = 'AgentInventoryManager'
LABEL = 'dev.mcpandskills.helper'


def _command():
    if getattr(sys, 'frozen', False):
        return [str(Path(sys.executable).resolve())]
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe' and executable.with_name('python.exe').exists():
        executable = executable.with_name('python.exe')
    return [str(executable), str(resources_dir() / 'launcher.py')]


def _request(port, path, *, token=None, payload=None):
    base = f'http://127.0.0.1:{port}'
    headers = {'Origin': base}
    if token:
        headers['X-Inventory-Token'] = token
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(base + path, headers=headers,
        data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read(65537))


def _health(port):
    try:
        result = _request(port, '/health')
        return result if result.get('application') == APPLICATION else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


@contextlib.contextmanager
def singleton(state_dir):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    handle = (directory / 'helper.lock').open('a+b')
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('A helper is already running for this user profile.') from None
        yield
    finally:
        handle.close()


def start(state_dir, port=8766, home=None, no_scheduler=False):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    if _health(port):
        # Do not adopt a helper from a different profile on the same port.
        runtime = state_dir / 'runtime.json'
        try:
            record = json.loads(runtime.read_text(encoding='utf-8'))
            if record.get('port') != port:
                raise ValueError()
        except (OSError, ValueError):
            raise RuntimeError('This port belongs to another MCP & Skills profile. Choose a different port.') from None
        return {'running': True, 'url': f'http://127.0.0.1:{port}/', 'alreadyRunning': True}
    args = [*_command(), 'serve', '--state-dir', str(state_dir), '--port', str(port), '--no-browser']
    if home is not None:
        args += ['--home', str(Path(home).resolve())]
    if no_scheduler:
        args += ['--no-scheduler']
    with (state_dir / 'helper.log').open('ab') as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            cwd=str(resources_dir()), creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt')
    if os.name != 'nt':
        (state_dir / 'helper.log').chmod(0o600)
    for _ in range(120):
        if process.poll() is not None:
            break
        if _health(port):
            return {'running': True, 'url': f'http://127.0.0.1:{port}/', 'alreadyRunning': False}
        time.sleep(.25)
    raise RuntimeError('The helper did not become ready. Check helper.log in the app data folder or try another port.')


def stop(state_dir, port=8766):
    if not _health(port):
        return {'running': False, 'message': 'The helper is not running.'}
    try:
        record = json.loads((Path(state_dir) / 'runtime.json').read_text(encoding='utf-8'))
        if record.get('port') != port:
            raise ValueError()
    except (OSError, ValueError):
        raise RuntimeError('The active helper does not match this user profile; it was left running.') from None
    try:
        token = _request(port, '/api/session')['token']
        _request(port, '/api/helper/stop', token=token, payload={})
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            raise RuntimeError('An installation or update is running. Wait for it to finish before stopping the helper.') from None
        raise RuntimeError('The helper could not be stopped safely. Open the dashboard and retry after updates finish.') from None
    except (OSError, ValueError, KeyError):
        raise RuntimeError('The helper could not be stopped safely; no process was forcefully terminated.') from None
    for _ in range(40):
        if not _health(port):
            return {'running': False, 'message': 'The helper stopped. Installed MCPs, skills, and saved data were retained.'}
        time.sleep(.25)
    raise RuntimeError('The helper has not finished shutting down yet.')


def startup_path(home=None, system=None):
    home, system = Path(home or Path.home()), system or platform.system()
    if system == 'Windows':
        base = Path(os.environ.get('APPDATA', home / 'AppData/Roaming'))
        return base / 'Microsoft/Windows/Start Menu/Programs/Startup/MCP and Skill Updates.vbs'
    if system == 'Darwin':
        return home / 'Library/LaunchAgents' / (LABEL + '.plist')
    config = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
    if shutil.which('systemctl') and os.environ.get('XDG_RUNTIME_DIR'):
        return config / 'systemd/user/mcp-and-skills.service'
    return config / 'autostart/mcp-and-skills.desktop'


def install_startup(state_dir, port=8766):
    path = startup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [*_command(), 'start', '--no-browser', '--state-dir', str(Path(state_dir)), '--port', str(port)]
    system = platform.system()
    if system == 'Windows':
        command = subprocess.list2cmdline(args).replace('"', '""')
        path.write_text('Set shell = CreateObject("WScript.Shell")\nshell.Run "' + command + '", 0, False\n', encoding='utf-16')
        # Migrate only the known shortcut installed by earlier versions.
        old = path.with_name('MCP and Skill Updates.lnk')
        old.unlink(missing_ok=True)
    elif system == 'Darwin':
        args[args.index('start')] = 'serve'
        path.write_bytes(plistlib.dumps({'Label': LABEL, 'ProgramArguments': args,
            'RunAtLoad': True, 'ProcessType': 'Background',
            'KeepAlive': {'SuccessfulExit': False},
            'StandardOutPath': str(Path(state_dir) / 'startup.log'),
            'StandardErrorPath': str(Path(state_dir) / 'startup.log')}))
        path.chmod(0o600)
    elif path.suffix == '.service':
        args[args.index('start')] = 'serve'
        escaped = ' '.join('"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"' for value in args)
        path.write_text('[Unit]\nDescription=MCP and Skills local helper\nAfter=network.target\n\n'
                        '[Service]\nType=simple\nExecStart=' + escaped + '\nRestart=on-failure\nRestartSec=5\n\n'
                        '[Install]\nWantedBy=default.target\n', encoding='utf-8')
        path.chmod(0o600)
        for command in [['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'enable', path.name]]:
            subprocess.run(command, capture_output=True, check=True)
    else:
        command = ' '.join('"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"' for value in args)
        path.write_text('[Desktop Entry]\nType=Application\nName=MCP and Skills\nExec=' + command +
                        '\nTerminal=false\nX-GNOME-Autostart-enabled=true\n', encoding='utf-8')
        path.chmod(0o600)
    return {'startupEnabled': True, 'path': str(path), 'message': 'The helper will start at your next sign-in.'}


def uninstall_startup():
    system = platform.system()
    path = startup_path()
    if system == 'Linux' and path.suffix == '.service' and path.exists():
        subprocess.run(['systemctl', '--user', 'disable', path.name], capture_output=True, check=False)
    path.unlink(missing_ok=True)
    # Also remove the other owned Linux integration if installation mode changed.
    if system == 'Linux':
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        for owned in [base / 'autostart/mcp-and-skills.desktop', base / 'systemd/user/mcp-and-skills.service']:
            owned.unlink(missing_ok=True)
        if shutil.which('systemctl'):
            subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True, check=False)
    if system == 'Darwin':
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/{LABEL}'], capture_output=True, check=False)
    if system == 'Windows':
        path.with_name('MCP and Skill Updates.lnk').unlink(missing_ok=True)
    return {'startupEnabled': False, 'message': 'Automatic startup disabled. Agent installations and app data were retained.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='launch', choices=[
        'launch', 'start', 'serve', 'stop', 'status', 'install-startup', 'uninstall-startup'])
    parser.add_argument('--state-dir', type=Path, default=user_state_dir())
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--no-scheduler', action='store_true')
    parser.add_argument('--output', type=Path, help='Write a JSON result (also works in a windowless Windows executable).')
    args = parser.parse_args(argv)
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError('Use a port between 1024 and 65535.')
        args.state_dir.mkdir(parents=True, exist_ok=True)
        if args.command == 'serve':
            with singleton(args.state_dir):
                from server import main as serve
                options = ['--serve', '--state-dir', str(args.state_dir), '--home', str(args.home), '--port', str(args.port)]
                if args.no_scheduler:
                    options += ['--no-scheduler']
                return serve(options)
        if args.command in {'launch', 'start'}:
            result = start(args.state_dir, args.port, args.home, args.no_scheduler)
            if args.command == 'launch' and not args.no_browser:
                webbrowser.open(result['url'])
        elif args.command == 'stop':
            result = stop(args.state_dir, args.port)
        elif args.command == 'status':
            result = {'running': bool(_health(args.port)), 'url': f'http://127.0.0.1:{args.port}/',
                      'startupEnabled': startup_path().is_file(), 'stateDirectory': str(args.state_dir)}
        elif args.command == 'install-startup':
            result = install_startup(args.state_dir, args.port)
        else:
            result = uninstall_startup()
        if args.output:
            args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
        if sys.stdout:
            print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {'error': str(exc), 'running': False}
        if args.output:
            args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
        if sys.stderr:
            print(result['error'], file=sys.stderr)
        else:
            (args.state_dir / 'last-launch-error.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            if args.command != 'serve' and not args.no_browser and os.name == 'nt':
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, result['error'], 'MCP & Skills', 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
