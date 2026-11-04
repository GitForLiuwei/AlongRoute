# AlongRoute Windows setup. No administrative rights or policy change required.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
Write-Host '== AlongRoute: Windows setup ==' -ForegroundColor Cyan
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw '找不到 python 命令，请先安装或激活 Python 3.11+。' }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw '找不到 npm，请检查 Node.js 安装。' }

if (-not (Test-Path 'backend\.venv\Scripts\python.exe')) {
  Write-Host '创建 backend Python 虚拟环境...'
  python -m venv backend\.venv
  if ($LASTEXITCODE -ne 0) { throw '创建虚拟环境失败' }
}
Write-Host '安装 FastAPI 后端依赖...'
& 'backend\.venv\Scripts\python.exe' -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { throw '后端依赖安装失败' }

Write-Host '安装 Vue 前端依赖...'
Push-Location frontend
try {
  npm.cmd install
  if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败，请检查网络' }
} finally { Pop-Location }

if (-not (Test-Path 'backend\.env')) {
  Copy-Item 'backend\.env.example' 'backend\.env'
  Write-Host '已创建 backend\.env (Key 待填写)' -ForegroundColor Yellow
}
if (-not (Test-Path 'frontend\.env.local')) {
  Copy-Item 'frontend\.env.example' 'frontend\.env.local'
  Write-Host '已创建 frontend\.env.local (Key 待填写)' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '完成！接下来分别运行 start-backend.ps1 与 start-frontend.ps1。' -ForegroundColor Green
Write-Host '在未配置高德 Key 的情况下，演示模式可以直接使用。'
