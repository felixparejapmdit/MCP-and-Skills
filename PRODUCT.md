# MCP & Skills

A local control center for people who use Claude Code, Codex, and Gemini CLI across multiple projects. It reads their global tool configuration, discovers installed skills, checks known publishers, and keeps supported installations current with backups.

The primary task is to understand what is available for a chosen agent and what needs attention. The view selector chooses whose installations to inspect; it does not send prompts to an inference provider. Installation plans independently select Claude, Codex, and/or Gemini. Lifecycle changes affect only owned installations for the reviewed targets.

The dashboard must distinguish installed capabilities, missing capabilities, tools removed since a successful scan, missing local runtimes, and suggested tools that have never been installed. Native agent-specific capabilities must not create misleading gaps for other agents. Unreadable configuration must be reported rather than treated as proof of removal.

The redesign uses the Linear reference selected from Awesome Design MD, adapted to a working application. Light and dark themes, an installable PWA shell, an offline saved view, and layouts from narrow phones to wide desktop screens are required.

The local helper remains the authority for global filesystem changes. Offline operation is read-only, with clearly dated saved data. Configuration secrets, account credentials, and update authorization tokens must never enter browser storage or service-worker caches. Suggestions and missing-item detection do not silently install new tools.

Every PC starts with a public catalog of 18 MCPs and 337 skills. Existing installations are detected and preserved. New skills use agent-private global folders. Shared skills require an explicit migration plan listing every affected agent before conversion. Account-synced items without public recipes remain visible with manual setup guidance.

Native Windows, macOS, and Linux packages include the helper runtime and launch independently of a source checkout. Per-user data lives outside application resources. Optional Docker support provides a separate sandbox, never implicit host-profile mounting. Unsigned builds are clearly labeled; signing credentials are not part of the project.
