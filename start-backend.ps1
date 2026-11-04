$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot 'backend')
if (-not (Test-Path '.venv\Scripts\python.exe')) {
  throw '请先在项目根目录运行 setup-windows.ps1'
}
Write-Host 'FastAPI http://127.0.0.1:8000/docs' -ForegroundColor Green
& '.\.venv\Scripts\python.exe' -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
