# MCP and Skills

A local dashboard to detect, selectively install, and update global MCP servers and agent skills for **Claude Code, Codex, and Gemini CLI**. Version 2 adds a portable default catalog, native installers, a responsive light/dark PWA, and an optional Docker sandbox.

## Install and open

Download a package from [Releases](https://github.com/felixparejapmdit/MCP-and-Skills/releases). Packaged applications include Python. You do not need Python, Docker, or a cloned repository to open them.

| Platform | Package | Open |
| --- | --- | --- |
| Windows x64 | `windows-x64-unsigned-setup.exe` | Install for your account; open **MCP and Skills** from Start. Desktop shortcut is optional. |
| Windows portable | `windows-x64-unsigned-portable.zip` | Extract everything and open `mcp-skills.exe`. Keep `_internal` alongside it. |
| macOS Intel / Apple Silicon | Matching `macos-x64` / `macos-arm64` `.dmg` | Drag the app into Applications and open it. |
| Linux x64 / ARM64 | Matching `.AppImage` or `.deb` | Make the AppImage executable and open it, or install the DEB and use the Applications menu. |

Windows and macOS builds are **unsigned**. macOS builds are also **not notarized**. Your OS may require explicit approval to open downloaded software; managed devices may block it. Verify the release SHA256SUMS and follow your organization's policy. AppImages may require FUSE; extraction with `--appimage-extract` is another option. DEBs target glibc-based Debian/Ubuntu. macOS builds target macOS 15 or newer.

**Release status:** Windows setup and portable packages are built and tested. macOS/Linux build recipes and Docker support are implemented, but their native/integration checks are pending activation of GitHub Actions. The current publishing login lacks GitHub's `workflow` scope; the complete workflow is provided at `packaging/release-workflow.yml`. Download only artifacts actually attached to a release.

The app opens **http://127.0.0.1:8766/**. Closing the browser leaves the local helper running. Enable **Preferences → Run at sign-in** for checks after login. The installed launcher works outside the project folder. Installing this dashboard does not install every catalog item or the agent CLIs.

## Choose tools and agents

1. Open **MCP servers** or **Agent skills**. The default catalog contains **18 MCPs and 337 skills**, independent of this PC's installations.
2. Select an agent and filter **Not installed** to find gaps. The view filter is separate from installation targets.
3. Choose **Choose agents** on one item, or select several items and choose **Install selected**.
4. Check only the Claude, Codex, and/or Gemini profiles that should receive those tools.
5. Supply requested inputs, such as Filesystem access folders or a PostgreSQL connection string. Choose **Review plan**.
6. Review versions, destinations, dependencies, skips, and conflicts, then apply the plan. Progress and installation history show the result.
7. Restart the affected agent. Hosted MCPs may still need OAuth or account setup inside that agent.

Global means **the current OS user, across projects**, not every account on the PC. The selector does not invoke an LLM or send prompts. Profiles can be preconfigured before their CLI is installed; using the tools still requires that agent.

Automatic recipes cover 330 public skills and all 18 MCP registrations. Seven skills from account-synced sources remain marked for manual setup. The catalog is a starting list, not an instruction to install everything. Downloads need internet; some services need accounts or subscriptions.

## Existing installations and updates

Detection reads global agent configurations, skill discovery folders, enabled Claude MCP plugins, and `~/.agents/skills` shared by Codex and Gemini. Existing tools remain visible and are skipped when already available.

| Agent | MCP configuration | New private skill destination |
| --- | --- | --- |
| Claude Code | `~/.claude.json` | `~/.claude/skills/<name>` |
| Codex | `~/.codex/config.toml` | `~/.codex/skills/<name>` |
| Gemini CLI | `~/.gemini/settings.json` | `~/.gemini/skills/<name>` |

An explicit `CODEX_HOME` is respected for the current native user. Test profiles do not inherit it.

**Managed** means the app recorded ownership. Only owned installations can be updated or uninstalled here. Existing external tools are inspected for update information and preserved. Local edits and concurrent configuration changes block replacement. Uninstall affects only selected owned registrations or skills; caches and private backups remain.

**Check for updates** contacts recorded publishers. With automatic updates enabled, eligible owned installations update on the configured schedule (24 hours by default). Pause automatic updates to inspect versions first. HTTP service deployments remain provider managed; updates do not authenticate accounts.

Shared skills are never silently moved. **Review migration** explicitly converts a shared skill into selected private copies. The plan identifies all affected agents and those losing shared visibility. Applying it backs up and removes the shared folder after creating selected copies. Review this if Codex and Gemini currently share a skill.

**Suggested additions** shows curated tools absent from the inventory, with publisher links. Default catalog tools support direct installation; suggestions outside the catalog link to setup instructions. Detection and suggestions never automatically reinstall removed tools.

## Optional Docker sandbox

Use the native application for the PC's actual global profiles. Docker is a separate developer environment with its own profiles and state; it does not manage host installations.

1. In **Runtimes & Docker**, choose **Check Docker**. If missing, use the official install link and start Docker Desktop or Docker Engine. Native features work without it.
2. Choose **Generate configuration**, then **Start sandbox**. The first build downloads the public versioned source and base image.
3. Open **http://127.0.0.1:8877/**. Installs there affect only the container's `/data/agent-home`.
4. Use **View logs** for diagnostics and **Stop sandbox** when finished. Stopping retains the named volume.

Developers with a checkout can instead run:

```sh
docker compose up --build -d --wait
# Open http://127.0.0.1:8877/
docker compose logs --tail 80
docker compose down
```

Generated configuration builds the public `v2.0.0` tag, uses a non-root user and a named volume, and publishes only on loopback. It mounts neither the host home nor the Docker socket. Explicitly removing the volume deletes sandbox data. See [Docker's Compose guide](https://docs.docker.com/compose/gettingstarted/).

## Themes, responsive layout, and PWA

Choose **System**, **Light**, or **Dark** in Preferences. Sidebar categories separate inventory, management, and settings; icons inherit their label color. Layouts adapt from 320px phones to large desktops.

The design adapts the [Linear reference in Awesome Design MD](https://github.com/voltagent/awesome-design-md/tree/main/design-md/linear.app), using local assets and system fonts. See [DESIGN.md](DESIGN.md).

Choose **Install app** when offered, or use your browser's install menu. A manifest, icons, and offline shell are included. After visiting successfully, the PWA displays a dated sanitized inventory and catalog while disconnected. Installation/update controls require the helper. Tokens, connection strings, and plans are excluded from browser caches.

A PWA does not replace the native helper. A physical phone's localhost refers to that phone, so it cannot reach this PC using that address. Responsive layouts work in local browser device emulation. Remote host management is outside this local architecture.

## State, backups, and recovery

| OS | Default state folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\AgentInventoryManager` |
| macOS | `~/Library/Application Support/MCPandSkills` |
| Linux | `$XDG_DATA_HOME/mcp-and-skills` or `~/.local/share/mcp-and-skills` |

State contains schedules, ownership receipts, publisher caches, versioned runtimes, sanitized snapshots in `exports/`, and private backups in `install-backups/`, `backups/`, and `skill-backups/`. Resources can be read-only; writes go to user state. The original Desktop HTML is refreshed only for the legacy Desktop installation that owns it.

Configurations can contain credentials. Keep backups private. To recover, stop the helper and affected agent, inspect the transaction record, and restore only affected files. Avoid overwriting unrelated newer changes. Uninstalling the dashboard retains agent tools and user state.

## Run from source or build installers

Source execution requires **Python 3.11+** with no third-party Python runtime packages:

```sh
git clone https://github.com/felixparejapmdit/MCP-and-Skills.git
cd MCP-and-Skills
python launcher.py
```

Windows users can also open **Open Dashboard.cmd**. An absolute path to `launcher.py` works from any directory.

```sh
python launcher.py status
python launcher.py stop --no-browser
python launcher.py install-startup --no-browser
python launcher.py uninstall-startup --no-browser
```

Use `--state-dir`, `--home`, and `--port` for isolated development. Use separate ports for separate profiles. `--no-scheduler` disables timed checks. Do not run concurrent updaters against the same state or profiles.

```sh
python -m unittest -q
python -m pip install -r requirements-build.txt
python packaging/build.py
```

The prepared `packaging/release-workflow.yml` tests and builds natively on Windows, macOS Intel/ARM, and Linux x64/ARM. It runs the bundled app against temporary profiles and checks the Docker sandbox before publishing tag assets. It is a template until copied into `.github/workflows/release.yml` by an authorized maintainer. Build dependencies are pinned. [PyInstaller bundles Python and builds for its current OS](https://pyinstaller.org/en/stable/operating-mode.html). No signing certificates are included; optional signing configuration is in `packaging/build.py`.

Maintainers can enable the prepared workflow after `gh auth refresh -h github.com -s workflow` and completing GitHub sign-in:

```sh
mkdir -p .github/workflows
cp packaging/release-workflow.yml .github/workflows/release.yml
git add .github/workflows/release.yml
git commit -m "ci: enable native packaging and Docker checks"
git push
```

On Windows, use `New-Item -ItemType Directory -Force .github/workflows` and `Copy-Item packaging/release-workflow.yml .github/workflows/release.yml`. After the main-branch checks pass, use the workflow's manual trigger to build artifacts; a new version tag publishes them automatically.

## Troubleshooting

Reopen the app and check `http://127.0.0.1:8766/health`. If another program owns that port, use the launcher's `--port` option. Inspect `helper.log` and `last-launch-error.json` in state. Publisher outages leave existing tools intact. Invalid profile JSON/TOML is preserved for repair. MCP sign-in happens in its agent; reinstalling a registration does not authenticate it.

See [IMPLEMENTATION.md](IMPLEMENTATION.md) for architecture and APIs.
