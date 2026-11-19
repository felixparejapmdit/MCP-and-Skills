# MCP & Skills

A local control center for people who use Claude Code, Codex, and Gemini CLI across multiple projects. It reads their global tool configuration, discovers installed skills, checks known publishers, and keeps supported installations current with backups.

The primary task is to understand what is available for a chosen agent and what needs attention. The agent selector chooses whose installations to inspect; the application does not send prompts to an inference provider. Update actions continue to affect their registered global source bundles.

The dashboard must distinguish installed capabilities, missing capabilities, tools removed since a successful scan, missing local runtimes, and suggested tools that have never been installed. Native agent-specific capabilities must not create misleading gaps for other agents. Unreadable configuration must be reported rather than treated as proof of removal.

The redesign uses the Linear reference selected from Awesome Design MD, adapted to a working application. Light and dark themes, an installable PWA shell, an offline saved view, and layouts from narrow phones to wide desktop screens are required.

The local helper remains the authority for global filesystem changes. Offline operation is read-only, with clearly dated saved data. Configuration secrets, account credentials, and update authorization tokens must never enter browser storage or service-worker caches. Suggestions and missing-item detection do not silently install new tools.
