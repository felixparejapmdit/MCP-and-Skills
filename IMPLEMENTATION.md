# Local manager architecture

Version 2 uses a Python 3.11 standard-library HTTP helper with a static browser interface. Native packages bundle Python with PyInstaller. The default binding is `127.0.0.1:8766`. A controlled Docker sandbox binds internally on `0.0.0.0` and publishes only `127.0.0.1:8877`.

## Catalog and target ownership

`catalog.json` contains public descriptions and pinned recipes for 18 MCPs and 337 skills. It contains no installed profiles, generated inventories, credentials, or private skill contents. Seven account-synced skills have no automatic recipe. `catalog.py` validates the schema, identities, source allowlists, target compatibility, and versions.

`agent_adapters.py` reads Claude JSON, Codex TOML, and Gemini JSON. It writes only selected current-user profiles, preserves unrelated data, validates parse equality, and rejects linked destinations. New skills go to agent-private user roots. Shared `.agents/skills` installations are detected, not silently converted.

`installer.py` is the lifecycle authority. A plan is a one-use opaque identifier backed by process-memory options, destination fingerprints, and exact versions. Browser-visible plans contain no launch credentials. Execution stages immutable downloads, checks destinations again, backs up affected files, commits selected mutations and ownership receipts, and rolls back failures. Local edits block replacement. Existing external installations are skipped and cannot be uninstalled here.

Migration is an explicit operation. Its public plan lists affected agents, removed visibility, and shared-folder removal. Execution backs up the shared source, creates selected private copies, then removes the original shared source. Ineligible local sources retain manual update status.

`install_runtime.py` stages pinned npm packages with lifecycle scripts disabled and validates launcher paths. It can provision official checksum-verified Node/uv runtimes into user state without changing system Node. Skills use pinned GitHub archives validated against Git blob metadata. Repository hooks and setup scripts are not executed. Authentication is handled by the receiving agent/provider.

Owned per-target updates use IDs `install:<catalog-id>:<agent>`. Legacy discovery remains available for inventory and publisher checks with nonactionable external source units. Timed checks default to 24 hours and mutate only eligible owned installations. Provider-hosted deployments and app-bundled skills remain provider managed.

## API

All JSON API responses are `no-store`. Mutations require JSON, an exact allowed Origin/Host, and `X-Inventory-Token` from `/api/session`. Tokens live only in process/browser memory. There is no public CORS access.

| Method | Route | Contract |
| --- | --- | --- |
| GET | `/health` | Application identity, version, readiness |
| GET | `/api/session` | Fresh in-memory session token |
| GET | `/api/inventory` | Sanitized installed MCP and skill groups |
| GET | `/api/availability?app=all` | Installed/missing/removed evidence; accepts all, claude, codex, gemini |
| GET | `/api/catalog?app=all` | Default items with detected/managed/shared states per agent |
| POST | `/api/install/plan` | `{ids, targets, operation, options?}`; operation install, update, uninstall, migrate |
| POST | `/api/install/execute` | `{planId}`; accepts only an executable server-held plan; returns 202 |
| GET | `/api/install/status` | Background job, progress, sanitized result |
| GET | `/api/install/history` | Safe transaction summaries |
| GET | `/api/updates`, `/api/status` | Publisher versions, schedule, update job |
| POST | `/api/check-updates`, `/api/update` | Check/apply eligible owned updates; optional `{ids}` for update |
| POST | `/api/settings` | `{autoUpdate?, intervalHours?}` |
| GET | `/api/suggestions`, `/api/activity` | Curated absent tools; sanitized activity |
| GET | `/api/runtime` | OS, environment, prerequisites, startup state |
| POST | `/api/runtime/startup` | `{enabled: boolean}`; current native user only |
| GET | `/api/docker`, `/api/docker/status`, `/api/docker/logs` | Readiness, owned sandbox job, redacted logs |
| POST | `/api/docker/generate` | `{mode: "sandbox"}`; creates app-owned Compose configuration |
| POST | `/api/docker/start`, `/api/docker/stop` | `{}`; app-owned sandbox only |
| POST | `/api/helper/stop` | `{}`; refuses while a mutation job runs |

Installation options use catalog IDs, e.g. `{"mcp:filesystem":{"roots":["/absolute/folder"]}}` and `{"mcp:postgres":{"connectionString":"..."}}`. Secrets stay out of public plans, logs, and browser persistence. Original credential-bearing configs are kept only in private local backups. Jobs use status `running`, `done`, or `error`.

## Distribution and state

`platform_support.py` resolves resources independently of cwd, OS-native state paths, and native x64/ARM64 runtime archives. Windows preserves `%LOCALAPPDATA%/AgentInventoryManager`; macOS uses Application Support/MCPandSkills; Linux uses XDG data/mcp-and-skills. Snapshots write into state/exports, never installed application resources.

`launcher.py` provides launch/start/serve/stop/status and startup integration. A file lock prevents two launcher helpers sharing state. Startup uses a Windows VBS, macOS LaunchAgent, or Linux user systemd/XDG entry. The Windows installer is per-user; uninstall retains agent profiles and user state.

`packaging/build.py` packages a strict public resource whitelist, native setup/DMG/AppImage/DEB artifacts, and checksums. It excludes personal snapshots and state. Build natively on each OS/architecture. `packaging/release-workflow.yml` is the prepared GitHub Actions definition for tests, native bundle API smoke checks, and Docker integration before publishing tag assets. Activation requires copying it into `.github/workflows` with a workflow-authorized login. Windows builds are verified locally; macOS/Linux and Docker integration remain pending that activation. Windows/macOS binaries are unsigned unless signing credentials are supplied.

`docker_support.py` controls only a hash-owned generated Compose project. Its named volume contains isolated profiles and state. No host-home or Docker-socket mount is offered. Docker errors and logs are redacted. Docker is optional for native users.

## PWA and verification

The service worker fingerprints and caches only the generic shell/static assets. Live pages, session payloads, API responses, plans, mutations, and exports are excluded. Browser local storage retains only sanitized inventory/catalog/availability and preferences. Offline controls remain read-only.

`test_*.py` use temporary homes and mocked publishers. `packaging/smoke.py` runs the actual bundled helper or a fresh container and verifies catalog detection, Gemini-only installation/uninstallation, and history. Browser checks cover real plan/apply flows, theme variants, and widths 320–1920px. No verification should mutate real agent profiles.
