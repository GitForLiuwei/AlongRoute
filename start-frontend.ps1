$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot 'frontend')
if (-not (Test-Path 'node_modules')) { throw '请先在项目根目录运行 setup-windows.ps1' }
Write-Host 'Web http://127.0.0.1:5173' -ForegroundColor Green
npm.cmd run dev
