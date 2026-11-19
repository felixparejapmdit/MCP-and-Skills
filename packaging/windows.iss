#ifndef AppVersion
  #define AppVersion "2.0.1"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\mcp-skills"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist\installers"
#endif
#ifndef OutputName
  #define OutputName "MCP-and-Skills-" + AppVersion + "-windows-x64-unsigned-setup"
#endif

[Setup]
AppId={{6504589F-0497-46DA-818C-83619BD47816}
AppName=MCP & Skills
AppVersion={#AppVersion}
AppPublisher=MCP and Skills contributors
AppPublisherURL=https://github.com/felixparejapmdit/MCP-and-Skills
AppSupportURL=https://github.com/felixparejapmdit/MCP-and-Skills/issues
DefaultDirName={localappdata}\Programs\MCP and Skills
DefaultGroupName=MCP and Skills
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\mcp-skills.exe
SetupLogging=yes

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
Name: autostart; Description: "Start the local helper when I sign in"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\MCP and Skills"; Filename: "{app}\mcp-skills.exe"
Name: "{autodesktop}\MCP and Skills"; Filename: "{app}\mcp-skills.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\mcp-skills.exe"; Parameters: "install-startup --no-browser"; Tasks: autostart; Flags: runhidden waituntilterminated
Filename: "{app}\mcp-skills.exe"; Description: "Open MCP and Skills"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\mcp-skills.exe"; Parameters: "uninstall-startup --no-browser"; Flags: runhidden waituntilterminated; RunOnceId: RemoveMCPandSkillsStartup
Filename: "{app}\mcp-skills.exe"; Parameters: "stop --no-browser"; Flags: runhidden waituntilterminated; RunOnceId: StopMCPandSkillsHelper

[Messages]
WelcomeLabel2=This installs the local MCP and Skills dashboard for your Windows account.%n%nChoose which agent profiles and tools to manage after opening the app. Agent configuration, MCP servers, skills, and saved data are retained when this application is uninstalled.
