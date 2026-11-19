$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimePath = Join-Path $env:LOCALAPPDATA 'AgentInventoryManager\runtime.json'
if (-not (Test-Path -LiteralPath $runtimePath)) { Write-Output 'No helper runtime record was found.'; exit }
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
$helper = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$runtime.pid)
$expected = Join-Path $appRoot 'server.py'
if ($helper -and $helper.Name -match '^pythonw?\.exe$' -and $helper.CommandLine.Contains($expected)) {
    Stop-Process -Id $helper.ProcessId
    Write-Output 'The dashboard helper stopped. Automatic updates resume when you launch it again.'
} else { Write-Output 'The recorded helper process is no longer running.' }
