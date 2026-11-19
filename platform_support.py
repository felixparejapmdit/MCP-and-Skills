"""Host platform paths and verified, per-user tool runtimes.

The Python interpreter packaged with this app is not a general-purpose Python
CLI. Managed tools use an external interpreter or the native uv executable.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

APP_NAME = 'MCPandSkills'
UV_VERSION = '0.13.9'
_VERSION = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')


def user_state_dir(home=None, environ=None, system=None):
    home, environ, system = Path(home or Path.home()), os.environ if environ is None else environ, system or platform.system()
    if system == 'Windows':
        return Path(environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'AgentInventoryManager'
    if system == 'Darwin':
        return home / 'Library/Application Support' / APP_NAME
    return Path(environ.get('XDG_DATA_HOME', home / '.local/share')) / 'mcp-and-skills'


def resources_dir():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)).resolve()


def export_dir(state_dir=None):
    return Path(state_dir or user_state_dir()) / 'exports'


def _run(args, timeout=30, **kwargs):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, **kwargs)
    if result.returncode:
        raise RuntimeError('The selected tool did not complete successfully.')
    return result.stdout.strip()


def find_python():
    candidates = []
    if not getattr(sys, 'frozen', False):
        interpreter = Path(sys.executable)
        if interpreter.name.lower() == 'pythonw.exe' and interpreter.with_name('python.exe').is_file():
            interpreter = interpreter.with_name('python.exe')
        candidates.append(interpreter)
    candidates += [Path(value) for name in ['python3', 'python'] if (value := shutil.which(name))]
    if (platform.system() == 'Windows' and (py := shutil.which('py')) and
        not (getattr(sys, 'frozen', False) and Path(py).resolve() == Path(sys.executable).resolve())):
        try:
            candidates.append(Path(_run([py, '-3', '-c', 'import sys; print(sys.executable)'], timeout=10)))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            pass
    for candidate in dict.fromkeys(candidates):
        if 'WindowsApps' in candidate.parts or not candidate.is_file():
            continue
        if getattr(sys, 'frozen', False) and candidate.resolve() == Path(sys.executable).resolve():
            continue
        try:
            version = json.loads(_run([candidate, '-c', 'import sys,json; print(json.dumps(list(sys.version_info[:2])))'], timeout=10))
            if version >= [3, 11]:
                return candidate
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            continue
    return None


def find_uv(state_dir=None):
    candidates = []
    if value := shutil.which('uv'):
        candidates.append(Path(value))
    if not getattr(sys, 'frozen', False):
        spec = importlib.util.find_spec('uv')
        if spec and spec.origin:
            candidates.append(Path(spec.origin).parent / ('uv.exe' if os.name == 'nt' else 'uv'))
            if os.name == 'nt':
                candidates.append(Path(spec.origin).parent.parent.parent / 'Scripts/uv.exe')
    candidates.append(Path.home() / '.local/bin' / ('uv.exe' if os.name == 'nt' else 'uv'))
    if os.name == 'nt':
        candidates += sorted((Path.home() / 'AppData/Roaming/Python').glob('*/Scripts/uv.exe'), reverse=True)
    root = Path(state_dir or user_state_dir()) / 'runtimes/uv'
    candidates += sorted(root.glob('*/uv.exe' if os.name == 'nt' else '*/uv'), reverse=True)
    return next((p for p in candidates if p.is_file()), None)


def find_node(state_dir=None):
    if value := shutil.which('node'):
        return Path(value)
    root = Path(state_dir or user_state_dir()) / 'runtimes/node'
    candidates = list(root.glob('*/node.exe')) if os.name == 'nt' else list(root.glob('*/bin/node'))
    return max(candidates, key=lambda p: version_tuple(p.parent.name if os.name == 'nt' else p.parent.parent.name), default=None)


def find_npm_cli(node=None):
    node = Path(node or find_node() or '')
    candidates = [node.parent / 'node_modules/npm/bin/npm-cli.js',
                  node.parent.parent / 'lib/node_modules/npm/bin/npm-cli.js']
    for name in ['npm.cmd', 'npm']:
        if value := shutil.which(name):
            p = Path(value)
            candidates += [p.parent / 'node_modules/npm/bin/npm-cli.js',
                           p.resolve().parent / 'npm-cli.js',
                           p.parent.parent / 'lib/node_modules/npm/bin/npm-cli.js']
    return next((p for p in candidates if p.is_file()), None)


def version_tuple(value):
    match = _VERSION.fullmatch(str(value))
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def satisfies(version, expression):
    """Common stable Node engine syntax, failing closed on unknown syntax."""
    actual = version_tuple(version)
    if actual == (0, 0, 0):
        return False
    expression = str(expression or '*').strip()
    if expression in {'', '*', 'latest'}:
        return True
    for alternative in expression.split('||'):
        terms = alternative.replace(',', ' ').split()
        if not terms:
            continue
        passed = True
        for term in terms:
            if term in {'*', 'x', 'X'}:
                continue
            match = re.fullmatch(r'(\^|~|>=|<=|>|<|=)?v?(\d+)(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?', term)
            if not match:
                passed = False
                break
            op, major, minor, patch = match.groups()
            major = int(major)
            wildcard_minor = minor is None or minor in {'x', 'X', '*'}
            wildcard_patch = patch is None or patch in {'x', 'X', '*'}
            minor, patch = (0 if wildcard_minor else int(minor)), (0 if wildcard_patch else int(patch))
            target = (major, minor, patch)
            if op == '^':
                upper = (major + 1, 0, 0) if major else ((0, minor + 1, 0) if minor else (0, 0, patch + 1))
                ok = target <= actual < upper
            elif op == '~':
                upper = (major + 1, 0, 0) if wildcard_minor else (major, minor + 1, 0)
                ok = target <= actual < upper
            elif op in {'>=', '<=', '>', '<'}:
                ok = {'>=': actual >= target, '<=': actual <= target, '>': actual > target, '<': actual < target}[op]
            elif wildcard_minor:
                ok = actual[0] == major
            elif wildcard_patch:
                ok = actual[:2] == target[:2]
            else:
                ok = actual == target
            passed = passed and ok
        if passed:
            return True
    return False


def _download(url, limit):
    if not (url.startswith('https://nodejs.org/dist/') or url.startswith('https://github.com/astral-sh/uv/releases/download/')):
        raise ValueError('Unregistered runtime download URL.')
    request = urllib.request.Request(url, headers={'User-Agent': 'MCPandSkills/2.0'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError('The runtime download exceeded its limit.')
    return data


def _member_path(name):
    value = str(name).replace('\\', '/')
    parts = PurePosixPath(value).parts
    if not parts or value.startswith('/') or any(p in {'.', '..'} or ':' in p or '\x00' in p for p in parts):
        raise RuntimeError('A runtime archive contains an unsafe path.')
    return Path(*parts)


def extract_runtime(payload, destination, kind):
    """Extract regular files only. Valid internal symlinks are unnecessary here."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    total, seen = 0, set()
    def path_for(name):
        relative = _member_path(name)
        folded = relative.as_posix().casefold()
        if folded in seen:
            raise RuntimeError('A runtime archive has duplicate file paths.')
        seen.add(folded)
        target = destination / relative
        if not target.resolve().is_relative_to(destination):
            raise RuntimeError('A runtime archive escaped its staging directory.')
        return target
    if kind == 'zip':
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for member in archive.infolist():
                target = path_for(member.filename)
                if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                    raise RuntimeError('A runtime ZIP contains a link.')
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                total += member.file_size
                if total > 650 * 1024 * 1024:
                    raise RuntimeError('Runtime archive expansion exceeded its limit.')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:*') as archive:
            for member in archive:
                target = path_for(member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    linked = target.parent / member.linkname if member.issym() else destination / member.linkname
                    if Path(member.linkname).is_absolute() or not linked.resolve().is_relative_to(destination):
                        raise RuntimeError('A runtime archive contains an escaping link.')
                    continue  # npm is launched through its real JS file.
                if not member.isfile():
                    raise RuntimeError('A runtime archive contains a special file.')
                total += member.size
                if total > 650 * 1024 * 1024:
                    raise RuntimeError('Runtime archive expansion exceeded its limit.')
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as stream:
                    target.write_bytes(stream.read())
                target.chmod(member.mode & 0o755)


def _architecture(system=None, machine=None):
    system, machine = system or platform.system(), (machine or platform.machine()).lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine)
    name = {'Windows': 'win', 'Darwin': 'darwin', 'Linux': 'linux'}.get(system)
    if not arch or not name:
        raise RuntimeError('This operating system or CPU architecture is not supported.')
    return name, arch


def ensure_node(engine, state_dir=None, progress=None, matches=None):
    """Return a compatible native Node; bootstrap a complete verified LTS distro."""
    state_dir = Path(state_dir or user_state_dir())
    current = find_node(state_dir)
    match = matches or satisfies
    if current:
        try:
            current_version = _run([current, '--version']).lstrip('v')
            if match(current_version, engine or '*') and find_npm_cli(current):
                return current
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            current_version = ''
    else:
        current_version = ''
    system, arch = _architecture()
    suffix = 'zip' if system == 'win' else 'tar.gz' if system == 'darwin' else 'tar.xz'
    file_id = f'win-{arch}-zip' if system == 'win' else f'osx-{arch}-tar' if system == 'darwin' else f'linux-{arch}'
    index = json.loads(_download('https://nodejs.org/dist/index.json', 5 * 1024 * 1024))
    candidates = [r for r in index if _VERSION.fullmatch(str(r.get('version', ''))) and
                  file_id in r.get('files', []) and match(r['version'][1:], engine or '*')]
    if not candidates:
        raise RuntimeError('No official Node release satisfies the package requirement.')
    selected = max(candidates, key=lambda r: (bool(current_version) and version_tuple(r['version'])[0] == version_tuple(current_version)[0],
                                              bool(r.get('lts')), version_tuple(r['version'])))
    version = selected['version']
    root = state_dir / 'runtimes/node' / version
    executable = root / ('node.exe' if system == 'win' else 'bin/node')
    if executable.is_file() and find_npm_cli(executable) and _run([executable, '--version']) == version:
        return executable
    if progress:
        progress({'message': 'Downloading verified native Node ' + version})
    filename = f'node-{version}-{system}-{arch}.{suffix}'
    base = f'https://nodejs.org/dist/{version}/'
    sums = _download(base + 'SHASUMS256.txt', 512 * 1024).decode().splitlines()
    digest = next((x.split()[0] for x in sums if len(x.split()) == 2 and x.split()[1] == filename), '')
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise RuntimeError('The official runtime checksum was not found.')
    payload = _download(base + filename, 160 * 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise RuntimeError('The Node runtime failed checksum verification.')
    staging = state_dir / 'runtime-staging' / secrets.token_hex(8)
    try:
        extract_runtime(payload, staging, suffix)
        unpacked = staging / f'node-{version}-{system}-{arch}'
        binary = unpacked / ('node.exe' if system == 'win' else 'bin/node')
        if not binary.is_file() or _run([binary, '--version']) != version or not find_npm_cli(binary):
            raise RuntimeError('The staged runtime is incomplete or has the wrong version.')
        root.parent.mkdir(parents=True, exist_ok=True)
        if root.exists():
            preserved = root.with_name(root.name + '-preserved-' + secrets.token_hex(4))
            os.replace(root, preserved)
        os.replace(unpacked, root)
        return executable
    finally:
        if staging.exists() and staging.resolve().is_relative_to((state_dir / 'runtime-staging').resolve()):
            shutil.rmtree(staging)


def ensure_uv(state_dir=None, progress=None):
    state_dir = Path(state_dir or user_state_dir())
    if value := find_uv(state_dir):
        return value
    system, arch = _architecture()
    triple = {'win': f'{"aarch64" if arch == "arm64" else "x86_64"}-pc-windows-msvc',
              'darwin': f'{"aarch64" if arch == "arm64" else "x86_64"}-apple-darwin',
              'linux': f'{"aarch64" if arch == "arm64" else "x86_64"}-unknown-linux-gnu'}[system]
    suffix = 'zip' if system == 'win' else 'tar.gz'
    filename = f'uv-{triple}.{suffix}'
    base = f'https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/'
    if progress:
        progress({'message': 'Downloading verified native uv for Python tools'})
    checksum = _download(base + filename + '.sha256', 4096).decode().split()[0]
    if not re.fullmatch(r'[0-9a-f]{64}', checksum):
        raise RuntimeError('The uv publisher checksum is invalid.')
    payload = _download(base + filename, 80 * 1024 * 1024)
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise RuntimeError('The uv runtime failed checksum verification.')
    staging = state_dir / 'runtime-staging' / secrets.token_hex(8)
    try:
        extract_runtime(payload, staging, suffix)
        candidates = list(staging.rglob('uv.exe' if system == 'win' else 'uv'))
        if len(candidates) != 1 or not _run([candidates[0], '--version']).startswith('uv '):
            raise RuntimeError('The staged uv runtime could not be verified.')
        root = state_dir / 'runtimes/uv' / UV_VERSION
        root.mkdir(parents=True, exist_ok=True)
        destination = root / candidates[0].name
        shutil.copy2(candidates[0], destination)
        return destination
    finally:
        if staging.exists() and staging.resolve().is_relative_to((state_dir / 'runtime-staging').resolve()):
            shutil.rmtree(staging)
