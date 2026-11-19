# Local agent inventory manager

This Desktop application manages the existing global Claude Code, Codex, and Gemini installations. It is local-only. Preserve the incumbent inventory's typography, colors, search, table, and collapsible groups.

## Runtime

- Python 3.11 standard library backend, bound to 127.0.0.1:8766.
- Persistent state, caches, backups, logs and update runtimes under `%LOCALAPPDATA%/AgentInventoryManager`.
- Global skills remain in `~/.agents/skills`, with existing Claude/Gemini-specific copies and aliases preserved.
- Configuration updates preserve secrets and unrelated fields. Secrets never appear in API responses, browser data, logs, suggestions, or HTML exports.
- Automatic checks and supported updates default to every 24 hours; settings can pause or change the interval. The user explicitly requested automatic updates.
- Source bundles update together. Preserve local adapters and local modifications, stage and validate before replacement, back up every replacement, and report conflicts instead of overwriting them.
- Hosted MCP services and agent-bundled system skills are labeled provider-managed. Claude-synced private skills track their local publisher source.
- Suggested new tools are displayed, not automatically installed.

## Skill provider interface (skill_updates.py)

`SkillUpdater(home: pathlib.Path, state_dir: pathlib.Path, log=None)`

- `discover()` returns sanitized skill inventory rows.
- `check_all(progress=None)` returns a list of update-unit dictionaries.
- `apply(unit_id, progress=None)` returns a result dictionary. Update only source IDs known to this provider.

Each update unit uses `id`, `name`, `kind`, `installedVersion`, `latestVersion`, `status`, `itemCount`, `sourceUrl`, and `detail`; optional `targetNames` helps attach one source check to multiple skills. Statuses: `up-to-date`, `update-available`, `host-managed`, `local-changes`, `error`, `updated`. No setup scripts/hooks from downloaded repositories are executed.

## HTTP contract

- GET `/api/inventory`: `{mcp, skillGroups, stats, applications, generatedAt}`. MCP rows contain `id,name,description,apps,status,kind,sourceId`; groups contain `id,title,state,apps,names,count,kind,sourceId`.
- GET `/api/availability?app=all|claude|codex|gemini`: `{selectedApp, generatedAt, applications, summary, items, scanComplete, warnings}`. Items distinguish `installed`, `missing`, and `not-installed` and include compatibility, removal evidence, and missing-runtime flags. History stores sanitized installation identities outside the repository.
- GET `/api/session`: a fresh in-memory authorization token for reconnecting the PWA after a helper restart. This response is never cached or persisted in browser storage.
- GET `/api/status`: `{job, settings, lastCheck, lastUpdate, helperVersion}`.
- GET `/api/updates`: `{items, checkedAt}`.
- GET `/api/suggestions`: `{items, checkedAt}`; each item has `id,name,kind,description,reason,url,requiresAccount`.
- GET `/api/activity`: `{items}`.
- POST `/api/check-updates`: background version check, automatic apply when configured; returns HTTP 202.
- POST `/api/update`: `{ids?: [known-source-id]}`; updates only installed managed sources; returns HTTP 202.
- POST `/api/settings`: `{autoUpdate?: boolean, intervalHours?: 6|12|24|48|168}`.

Mutation endpoints accept JSON, require same-origin loopback requests and a generated `X-Inventory-Token` header. The backend injects the token into the served page as `window.INVENTORY_TOKEN`; no public CORS access. HTML loaded directly from disk retains its saved inventory and links to the local dashboard.

## Offline and PWA boundaries

`/manifest.webmanifest` declares standalone mode and local icons. `/service-worker.js` fingerprints the generic shell and public static assets, so source changes produce a new cache version. The worker caches `/offline.html` and its static dependencies, never live `/`, `/index.html`, API responses, exported snapshots, mutations, cross-origin requests, or query-bearing requests.

The offline shell has no bootstrap credentials. The frontend keeps sanitized inventory, per-agent availability, suggestions, and save timestamps in browser storage, while the theme and agent preferences have separate keys. Offline actions remain read-only. The loopback helper remains required for fresh detection and global updates.

## Files

- `server.py`: loopback HTTP server, request validation, and scheduler startup.
- `manager_core.py`: inventory, MCP runtime updates, jobs, settings, and snapshot exports.
- `skill_updates.py`: source discovery, provenance checks, staged skill updates, and rollback.
- `availability.py`: read-only installation scans, native compatibility, missing launchers, and durable removal evidence.
- `web/`: browser interface; snapshots are generated locally and excluded from Git.
- `suggestions.json`: curated additions filtered against the installed inventory.
- `test_*.py`: isolated regression checks with temporary homes and mocked publishers.
- PowerShell launchers: starting, stopping, and enabling per-user Windows startup.
