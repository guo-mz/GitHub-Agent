# 一键启动 GitHub Agent（本机无 Docker 也能用：Redis/RAG 自动降级）
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Py = Join-Path $Backend ".venv\Scripts\python.exe"
$Npm = "D:\nodjs\nodejs\npm.cmd"
if (-not (Test-Path $Npm)) {
  $cmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
  if ($cmd) { $Npm = $cmd.Source }
}
$ApiPort = 8001
$Api = "http://127.0.0.1:$ApiPort"

Write-Host "== starting GitHub Agent ==" -ForegroundColor Cyan

# free ports
foreach ($port in @($ApiPort, 5173)) {
  Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.OwningProcess -and $_.OwningProcess -ne 0) {
      Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
    }
  }
}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
  $_.CommandLine -match 'uvicorn'
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

# frontend env
$feEnv = Join-Path $Frontend ".env"
Set-Content -Path $feEnv -Value "VITE_API_BASE=$Api" -Encoding ascii

# backend
$env:PYTHONPATH = $Backend
$out = Join-Path $Backend "uvicorn.out.log"
$err = Join-Path $Backend "uvicorn.err.log"
Start-Process -FilePath $Py -ArgumentList "-m","uvicorn","app.main:app","--host","127.0.0.1","--port","$ApiPort" `
  -WorkingDirectory $Backend -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden | Out-Null

# frontend
if (Test-Path $Npm) {
  Start-Process -FilePath $Npm -ArgumentList "run","dev","--","--host","127.0.0.1","--port","5173" `
    -WorkingDirectory $Frontend -WindowStyle Hidden | Out-Null
} else {
  Write-Host "[!!] npm.cmd not found, start frontend manually" -ForegroundColor Yellow
}

Write-Host "waiting for health..."
$ok = $false
for ($i = 1; $i -le 20; $i++) {
  Start-Sleep 1
  try {
    $h = Invoke-RestMethod "$Api/health" -TimeoutSec 2
    Write-Host ("[OK] health redis=" + $h.redis + " postgres=" + $h.postgres + " rag=" + $h.rag + " mode=" + $h.rag_mode) -ForegroundColor Green
    $ok = $true
    break
  } catch {}
}
if (-not $ok) {
  Write-Host "[XX] backend failed, see backend\uvicorn.err.log" -ForegroundColor Red
  exit 1
}

# warm RAG local + index README
try {
  $idx = Invoke-RestMethod -Method Post "$Api/api/repos/guo-mz/-/index?path=README.md&ref=main" -TimeoutSec 60
  Write-Host ("[OK] " + ($idx.detail)) -ForegroundColor Green
} catch {
  Write-Host ("[!!] index: " + $_.Exception.Message) -ForegroundColor Yellow
}

Write-Host ""
Write-Host "UI:  http://127.0.0.1:5173/chat" -ForegroundColor Cyan
Write-Host "API: $Api/docs" -ForegroundColor Cyan
Write-Host "Tip: Redis/Postgres optional; RAG uses local store when Docker missing." -ForegroundColor DarkGray
Start-Process "http://127.0.0.1:5173/chat"
