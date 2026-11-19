$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupPath = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupPath 'MCP and Skill Updates.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'
$shortcut.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $appRoot 'Start Dashboard.ps1') + '" -NoBrowser'
$shortcut.WorkingDirectory = $appRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Start the local global MCP and Agent Skills update manager at sign-in.'
$shortcut.Save()
Write-Output ('Enabled startup: ' + $shortcutPath)
