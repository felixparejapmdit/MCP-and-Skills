param([switch]$NoBrowser)
& (Join-Path $PSScriptRoot 'Invoke Launcher.ps1') -Command launch -NoBrowser:$NoBrowser
