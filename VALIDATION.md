# Version 2.0.1 verification

Checked on Windows 11 x64, 20 November 2026 (Singapore time).

| Check | Evidence |
| --- | --- |
| Automated suite | 119 isolated tests pass under Python 3.13.18; source compatibility requires Python 3.11+. |
| Native Windows packaging | PyInstaller 6.22.3; per-user Inno Setup installer and portable ZIP; bundled Python 3.13.18; SHA256 manifests included. |
| Fresh-machine behavior | Actual packaged helper exposes 355 default items (18 MCPs, 337 skills); an empty temporary home reports no installed catalog items. |
| Target selection | Actual API and browser flows install/uninstall Context7 for Gemini only, preserving Claude/Codex profiles. |
| Actual public skills | Code review installed for Gemini; accessibility (ECC), brainstorming (Superpowers), Impeccable, Hallmark, Awesome Design MD, and Ponytail downloaded and installed for Codex in isolated profiles. |
| Actual npm MCP | Memory server staged and registered for Gemini only; its process returned a valid MCP initialization response. |
| Transaction protection | Tests cover changed configs, receipt-write failure rollback, shared-folder preservation, explicit migration, linked-profile rejection, and one-use plans. |
| Archive integrity | Impeccable CMD archive CRLF conversion is normalized only if canonical bytes match the complete pinned Git blob hash; tampered bytes remain rejected. |
| Browser interface | Actual install/uninstall flow, 320/375/768/1024/1440/1920px widths in light and dark themes, no page overflow or uncaught script errors. Manifest/service worker registered. |
| Installed launcher | Installed executable starts from outside the checkout; current-user dashboard and sign-in integration use the installed app. |
| Data boundaries | Source/staged-file checks exclude known real-profile secrets, personal snapshots, state, build outputs, and unrelated local files. No verification installs tools into real agent profiles. |

## Pending external verification

macOS Intel/Apple Silicon packages, Linux x64/ARM64 packages, and Docker integration have implementation/build definitions but have not been executed on those platforms in this session. Docker is not installed on the local test PC. The complete GitHub Actions definition is at `packaging/release-workflow.yml`.

GitHub rejected publishing `.github/workflows/release.yml` because the authenticated login lacks `workflow` scope. Application source and Windows release assets can be published with the existing repository permission. The README documents how an authorized maintainer can enable the workflow and run the outstanding native/container checks.

Windows/macOS signing certificates and Apple notarization credentials are not supplied. Windows artifacts are explicitly labeled unsigned. Only Windows binaries are provided for this release.
