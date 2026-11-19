# MCP and Agent Skills — local dashboard

View your global Claude Code, Codex, and Gemini CLI tools, check their publishers for updates, and keep supported installations current. The dashboard runs only on this computer at **http://127.0.0.1:8766/**.

## Open the project

Clone the repository. These PowerShell examples use a folder on your Desktop:

```powershell
git clone https://github.com/felixparejapmdit/MCP-and-Skills.git "$env:USERPROFILE\Desktop\MCP and Agent Skills"
```

1. Open the cloned **MCP and Agent Skills** folder.
2. Double-click **Open Dashboard.cmd**. It starts the hidden Python helper if needed and opens your browser.
3. Leave the helper running for automatic checks. You can close the browser.

From PowerShell, the equivalent command is:

```powershell
Set-Location (Join-Path $env:USERPROFILE 'Desktop\MCP and Agent Skills')
powershell -NoProfile -ExecutionPolicy Bypass -File '.\Start Dashboard.ps1'
```

Requirements: Windows, Python 3.11 or newer, and existing global Claude Code, Codex, or Gemini CLI installations to inspect. The launcher is configured for Python at `C:\Program Files\Python311`; adjust `$pythonWindowless` in `Start Dashboard.ps1` if your Python installation is elsewhere, or run `python .\server.py --serve` from this folder. The web app needs no Python packages, npm build, database, or API key. Updating npm MCP runtimes uses your installed Node.js/npm; Serena updates use the installed `uv` module and Python 3.13. Publisher checks and downloads require internet access.

This repository contains the dashboard and updater. It reads your existing global MCP configurations and skill metadata. Supported skills are matched to their recorded publisher provenance; unrecognized local skills are preserved. Your account configurations, installed skill files, generated inventory, and update history remain local.

## Use the dashboard

- **Overview / MCP servers / Agent skills:** browse the current global installations. Search by name and filter by Claude, Codex, or Gemini. The MCP connection badges show the saved connection check, not a new account login test.
- **Updates:** see installed and latest versions for each supported source. A skill collection shares one publisher check and update action; individual skills link to that source.
- **Check for updates:** contact the registered publishers. When automatic updates are enabled, available supported updates are applied after the check.
- **Update available sources / Update:** apply all eligible updates, or one source. The dashboard shows progress and the outcome.
- **Automatic updates:** enabled by default every **24 hours**. Change the interval or pause in Updates. To inspect available versions without applying them, pause automatic updates before checking.
- **Suggested additions:** reviewed MCPs and agent skills absent from the current inventory. Their source links let you inspect them; suggestions are not installed automatically. The list is curated in `suggestions.json` and filters against your actual installations.
- **Activity:** review completed checks, updates, and items requiring attention.

Restart an active Claude, Codex, or Gemini session after updates so it loads the new runtime or skill instructions.

## How updates work

The helper reads the global configs in `~/.claude.json`, `~/.codex/config.toml`, and `~/.gemini/settings.json`, enabled Claude plugin metadata, and the installed skill folders. Shared portable skills live in **`%USERPROFILE%\.agents\skills`**.

Supported local MCP packages are downloaded into versioned global runtime folders, validated, and connected to the existing client configurations. Arguments, environment variables, and account credentials are preserved. Hosted MCP services are labeled **Provider managed**, since their providers deploy updates.

If an MCP requires a newer Node version, the helper can download a separate Windows x64 Node runtime from the official release archive and verify its published SHA256 checksum. That runtime is used only for the managed MCP; the Node installation used by your other projects stays in place.

Skills track their recorded public publisher or their latest local Claude-synced source. Updates are staged and validated before replacement. Original files and configurations are backed up. Conflicting local edits are preserved and shown for review. App-bundled system skills remain managed by their app. Newly published skills are not silently added to an installed collection.

Credentials are excluded from the dashboard, saved snapshots, and activity messages. The helper accepts update commands only from its local dashboard.

## Automatic startup and stopping

Install the per-user **MCP and Skill Updates.lnk** shortcut using the command below. It starts the helper at Windows sign-in without opening a browser. Checks run while you are signed in and the helper is running; a missed interval is checked after the next start. Keep the project folder in place so the shortcut can find it.

Install or re-create the startup shortcut:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '.\Enable Startup.ps1'
```

Pause updates in the dashboard to keep browsing without automatic installation. To stop the helper completely:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File '.\Stop Dashboard.ps1'
```

Run the launcher again to resume. To disable startup, open `shell:startup` using Windows Run and delete **MCP and Skill Updates.lnk**. This leaves your installed MCPs and skills intact.

## Saved HTML, state, and backups

- `Desktop\MCP and Agent Skills.html` and this folder's `index.html` are self-contained saved inventories, refreshed when the helper starts and after a completed job. Open **Open live dashboard** inside them for update controls and suggestions.
- `web/` contains the live interface. Generated `index.html` and `web/snapshot.*` files are excluded from Git; start the helper to create your own snapshots.
- `%LOCALAPPDATA%\AgentInventoryManager\settings.json` stores the schedule.
- `%LOCALAPPDATA%\AgentInventoryManager\state.json` stores source check results and activity.
- `%LOCALAPPDATA%\AgentInventoryManager\backups` contains previous MCP configurations and the original Desktop HTML. `skill-backups` contains dated skill directory backups and transaction records.
- `%LOCALAPPDATA%\AgentInventoryManager\runtimes` contains versioned local MCP runtimes. Skill source caches and update receipts are kept in the same state folder.

For a manual recovery, stop the helper and close the affected agent first. Restore only the affected skill directory or configuration from its dated backup, then relaunch the agent. Configuration backups contain the original account credentials; keep them private.

## Troubleshooting and development

If the page does not open, run the launcher again. Confirm the helper responds:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8766/health'
```

If port 8766 belongs to another program, stop that conflicting program before launching. If a publisher is unavailable, the dashboard reports the failed source and retries on the next check. **Sign-in required** in the MCP table means that server needs account authorization in the relevant agent; updating it does not sign you in.

If GitHub's public metadata quota is exhausted, the helper can reuse your existing GitHub CLI sign-in. That credential stays in memory and is used only for GitHub API metadata; it is not written to the cache or sent with downloaded skill files.

Run in the foreground to inspect a startup error:

```powershell
& 'C:\Program Files\Python311\python.exe' .\server.py --serve
```

Stop the running helper before using another server instance or the one-time command below; concurrent instances should not update the same global folders.

```powershell
& 'C:\Program Files\Python311\python.exe' .\server.py --once
```

`--once` checks and applies supported updates when automatic updates are enabled, then exits. `--no-scheduler` starts a server without timed background checks for controlled verification. The local API and source ownership are documented in `IMPLEMENTATION.md`.

Regression checks use isolated temporary homes and do not update your real installations:

```powershell
& 'C:\Program Files\Python311\python.exe' -m unittest -v
```
