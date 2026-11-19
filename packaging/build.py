"""Build the native app on its target OS. No cross-compilation claims."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)", (ROOT / 'manager_core.py').read_text(encoding='utf-8'), re.M)[1]
TOOLS = json.loads((ROOT / 'packaging/toolchain.json').read_text(encoding='utf-8'))


def run(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def download_tool(spec, destination):
    destination = Path(destination)
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == spec['sha256']:
        return destination
    request = urllib.request.Request(spec['url'], headers={'User-Agent': 'MCPandSkills-native-build/2.0'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(120 * 1024 * 1024 + 1)
    if len(data) > 120 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != spec['sha256']:
        raise RuntimeError('The pinned build tool checksum did not match.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    destination.chmod(0o755)
    return destination


def build_icons(work):
    png192 = (ROOT / 'web/icons/icon-192.png').read_bytes()
    ico = work / 'app.ico'
    ico.write_bytes(struct.pack('<HHH', 0, 1, 1) + struct.pack('<BBBBHHII', 192, 192, 0, 0, 1, 32, len(png192), 22) + png192)
    png512 = (ROOT / 'web/icons/icon-512.png').read_bytes()
    icns = work / 'app.icns'
    icns.write_bytes(b'icns' + struct.pack('>I', 16 + len(png512)) + b'ic09' + struct.pack('>I', 8 + len(png512)) + png512)
    return ico, icns


def windows_compiler(cache):
    compiler = cache / 'InnoSetup7/ISCC.exe'
    if compiler.is_file():
        return compiler
    installer = download_tool(TOOLS['innoSetup'], cache / 'innosetup-7.1.1-x64.exe')
    run([installer, '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-',
         '/DIR=' + str(compiler.parent)], creationflags=subprocess.CREATE_NO_WINDOW)
    if not compiler.is_file():
        raise RuntimeError('The per-user Inno Setup compiler did not install.')
    return compiler


def windows_sign(path):
    thumbprint = os.environ.get('WINDOWS_SIGN_CERT_THUMBPRINT')
    if not thumbprint:
        return False
    tool = shutil.which('signtool')
    if not tool:
        raise RuntimeError('Signing was requested but signtool is unavailable.')
    run([tool, 'sign', '/sha1', thumbprint, '/fd', 'SHA256', '/td', 'SHA256',
         '/tr', 'https://timestamp.digicert.com', path])
    run([tool, 'verify', '/pa', path])
    return True


def linux_packages(bundle, artifacts, work, arch):
    machine = 'aarch64' if arch == 'arm64' else 'x86_64'
    appdir = work / 'MCP-and-Skills.AppDir'
    appdir.mkdir()
    target = appdir / 'usr/lib/mcp-and-skills'
    shutil.copytree(bundle, target)
    icon = ROOT / 'web/icons/icon-512.png'
    shutil.copy2(icon, appdir / 'mcp-and-skills.png')
    desktop = ('[Desktop Entry]\nType=Application\nName=MCP & Skills\nExec=mcp-skills\n'
               'Icon=mcp-and-skills\nCategories=Development;Utility;\nTerminal=false\n')
    (appdir / 'mcp-and-skills.desktop').write_text(desktop, encoding='utf-8')
    (appdir / 'AppRun').write_text('#!/bin/sh\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        'exec "$HERE/usr/lib/mcp-and-skills/mcp-skills" "$@"\n', encoding='utf-8')
    (appdir / 'AppRun').chmod(0o755)
    tool = download_tool(TOOLS['appImageTool'][machine], work / 'appimagetool')
    env = dict(os.environ, APPIMAGE_EXTRACT_AND_RUN='1', ARCH=machine)
    run([tool, '--no-appstream', appdir, artifacts / f'MCP-and-Skills-{VERSION}-linux-{arch}.AppImage'], env=env)
    debroot = work / 'debroot'
    package_path = debroot / 'opt/mcp-and-skills'
    shutil.copytree(bundle, package_path)
    (debroot / 'usr/bin').mkdir(parents=True)
    (debroot / 'usr/bin/mcp-skills').write_text('#!/bin/sh\nexec /opt/mcp-and-skills/mcp-skills "$@"\n', encoding='utf-8')
    (debroot / 'usr/bin/mcp-skills').chmod(0o755)
    (debroot / 'usr/share/applications').mkdir(parents=True)
    (debroot / 'usr/share/applications/mcp-and-skills.desktop').write_text(desktop, encoding='utf-8')
    iconroot = debroot / 'usr/share/icons/hicolor/512x512/apps'
    iconroot.mkdir(parents=True)
    shutil.copy2(icon, iconroot / 'mcp-and-skills.png')
    (debroot / 'DEBIAN').mkdir()
    (debroot / 'DEBIAN/control').write_text(
        f'Package: mcp-and-skills\nVersion: {VERSION}\nArchitecture: {"arm64" if arch == "arm64" else "amd64"}\n'
        'Maintainer: MCP and Skills contributors <155054492+felixparejapmdit@users.noreply.github.com>\n'
        'Section: devel\nPriority: optional\nDepends: libc6, libzstd1, zlib1g\n'
        'Description: Local MCP and agent skills manager\n'
        ' Detect and selectively install global tools for Claude, Codex, and Gemini.\n',
        encoding='utf-8')
    run(['dpkg-deb', '--build', '--root-owner-group', debroot, artifacts / f'MCP-and-Skills-{VERSION}-linux-{arch}.deb'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle-only', action='store_true', help='Skip OS installer creation.')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--work', type=Path, default=ROOT / 'build/native')
    args = parser.parse_args()
    output, work = args.output.resolve(), args.work.resolve()
    if work == ROOT or not work.is_relative_to(ROOT / 'build'):
        raise ValueError('Build work must be inside this checkout build directory.')
    if work.exists():
        # Only an explicit, verified build subdirectory is replaced.
        shutil.rmtree(work)
    work.mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)
    artifacts = output / 'installers'
    artifacts.mkdir(exist_ok=True)
    ico, icns = build_icons(work)
    system = platform.system()
    machine = platform.machine().lower()
    arch = 'arm64' if machine in {'arm64', 'aarch64'} else 'x64'
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
        '--name', 'mcp-skills', '--distpath', str(output), '--workpath', str(work / 'pyinstaller'),
        '--specpath', str(work), '--paths', str(ROOT), '--icon', str(ico if system == 'Windows' else icns)]
    if system == 'Darwin':
        command += ['--osx-bundle-identifier', 'dev.mcpandskills.desktop']
        if identity := os.environ.get('MACOS_SIGN_IDENTITY'):
            command += ['--codesign-identity', identity]
    for relative in ['web', 'catalog', 'skill_templates', 'catalog.json', 'suggestions.json', 'Dockerfile', 'compose.yaml']:
        source = ROOT / relative
        if source.exists():
            # Never bundle generated personal inventories.
            if source.is_dir() and relative == 'web':
                clean_web = work / 'web'
                clean_web.mkdir()
                for name in ['index.html', 'app.js', 'styles.css', 'theme.js', 'pwa.js', 'service-worker.js', 'manifest.webmanifest']:
                    if (source / name).is_file():
                        shutil.copy2(source / name, clean_web / name)
                (clean_web / 'icons').mkdir()
                for name in ['icon.svg', 'icon-192.png', 'icon-512.png', 'icon-maskable-512.png', 'apple-touch-icon.png']:
                    if (source / 'icons' / name).is_file():
                        shutil.copy2(source / 'icons' / name, clean_web / 'icons' / name)
                template = (clean_web / 'index.html').read_text(encoding='utf-8')
                if re.search(r'window\.INVENTORY_(?:TOKEN|SNAPSHOT|FALLBACK)\s*=', template):
                    raise RuntimeError('The public web template unexpectedly contains a personal bootstrap payload.')
                source = clean_web
            command += ['--add-data', str(source) + os.pathsep + (relative if source.is_dir() else '.')]
    command.append(str(ROOT / 'launcher.py'))
    run(command, cwd=ROOT)
    bundle = output / 'mcp-skills'
    signed = False
    if system == 'Windows':
        signed = windows_sign(bundle / 'mcp-skills.exe')
    if system == 'Darwin':
        app = output / 'mcp-skills.app'
        metadata_path = app / 'Contents/Info.plist'
        metadata = plistlib.loads(metadata_path.read_bytes())
        metadata.update(CFBundleName='MCP & Skills', CFBundleDisplayName='MCP & Skills',
                        CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION)
        metadata_path.write_bytes(plistlib.dumps(metadata))
        if identity := os.environ.get('MACOS_SIGN_IDENTITY'):
            run(['codesign', '--force', '--deep', '--options', 'runtime', '--timestamp', '--sign', identity, app])
            run(['codesign', '--verify', '--deep', '--strict', app])
            signed = True
        if not args.bundle_only:
            suffix = '' if signed else '-unsigned'
            dmg = artifacts / f'MCP-and-Skills-{VERSION}-macos-{arch}{suffix}.dmg'
            run(['hdiutil', 'create', '-volname', 'MCP and Skills', '-srcfolder', app, '-ov', '-format', 'UDZO', dmg])
            if profile := os.environ.get('MACOS_NOTARY_PROFILE'):
                if not signed:
                    raise RuntimeError('Notarization requires a configured signing identity.')
                run(['xcrun', 'notarytool', 'submit', dmg, '--keychain-profile', profile, '--wait'])
                run(['xcrun', 'stapler', 'staple', dmg])
    else:
        name = f'MCP-and-Skills-{VERSION}-{system.lower()}-{arch}' + ('-unsigned' if system == 'Windows' and not signed else '') + '-portable'
        shutil.make_archive(str(artifacts / name), 'zip', root_dir=output, base_dir='mcp-skills')
        if not args.bundle_only and system == 'Windows':
            cache = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / 'MCPandSkillsBuild'
            compiler = windows_compiler(cache)
            suffix = '' if signed else '-unsigned'
            setup_name = f'MCP-and-Skills-{VERSION}-windows-{arch}{suffix}-setup'
            run([compiler, '/DAppVersion=' + VERSION, '/DSourceDir=' + str(bundle),
                 '/DOutputDir=' + str(artifacts), '/DOutputName=' + setup_name, ROOT / 'packaging/windows.iss'])
            windows_sign(artifacts / (setup_name + '.exe'))
        elif not args.bundle_only and system == 'Linux':
            linux_packages(bundle, artifacts, work, arch)
    files = [p for p in artifacts.iterdir() if p.is_file() and p.name.startswith('MCP-and-Skills-' + VERSION)]
    manifest = {'version': VERSION, 'platform': system, 'architecture': arch, 'pythonVersion': platform.python_version(), 'signed': signed,
                'notarized': bool(system == 'Darwin' and os.environ.get('MACOS_NOTARY_PROFILE')),
                'artifacts': [{'name': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in files]}
    (artifacts / f'build-{system.lower()}-{arch}.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    (artifacts / f'SHA256SUMS-{system.lower()}-{arch}.txt').write_text(
        ''.join(row['sha256'] + '  ' + row['name'] + '\n' for row in manifest['artifacts']), encoding='utf-8')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
