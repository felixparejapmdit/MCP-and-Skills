param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonWindowless = 'C:\Program Files\Python311\pythonw.exe'
$serverScript = Join-Path $appRoot 'server.py'
$dashboardAddress = 'http://127.0.0.1:8766/'
$ready = $false
try {
    $health = Invoke-RestMethod -Uri ($dashboardAddress + 'health') -TimeoutSec 2
    $ready = $health.application -eq 'AgentInventoryManager'
} catch { }
if (-not $ready) {
    if (-not (Test-Path -LiteralPath $pythonWindowless)) { throw 'Python 3.11 was not found at the installed location.' }
    Start-Process -FilePath $pythonWindowless -ArgumentList ('"' + $serverScript + '" --serve') -WorkingDirectory $appRoot -WindowStyle Hidden
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        try {
            $health = Invoke-RestMethod -Uri ($dashboardAddress + 'health') -TimeoutSec 1
            if ($health.application -eq 'AgentInventoryManager') { $ready = $true; break }
        } catch { }
    }
}
if (-not $ready) { throw 'The local helper did not start. Check whether port 8766 is already in use.' }
if (-not $NoBrowser) { Start-Process $dashboardAddress }
