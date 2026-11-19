param(
    [ValidateSet('launch', 'start', 'stop', 'install-startup', 'uninstall-startup', 'status')]
    [string]$Command = 'launch',
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$installedLauncher = Join-Path $env:LOCALAPPDATA 'Programs\MCP and Skills\mcp-skills.exe'
$launcherOptions = @($Command)
if ($NoBrowser) { $launcherOptions += '--no-browser' }
if (Test-Path -LiteralPath $installedLauncher) {
    & $installedLauncher @launcherOptions
    exit $LASTEXITCODE
}
$pythonLauncher = Get-Command 'py.exe' -ErrorAction SilentlyContinue
if ($pythonLauncher) {
    & $pythonLauncher.Source -3 (Join-Path $appRoot 'launcher.py') @launcherOptions
    exit $LASTEXITCODE
}
$pythonExecutable = Get-Command 'python.exe' -ErrorAction SilentlyContinue
if ($pythonExecutable) {
    & $pythonExecutable.Source (Join-Path $appRoot 'launcher.py') @launcherOptions
    exit $LASTEXITCODE
}
throw 'Install the native MCP and Skills application, or install Python 3.11+ to run this source checkout.'
