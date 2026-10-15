# 微服务模式：Gateway(8001) + Agent(8011) + Repo(8012)
# 含：服务发现 / Redis 分库 / 跨服务 Trace / 网关熔断 / Agent→Repo GitHub RPC
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\start-micro.ps1
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

$GwPort = 8001
$AgentPort = 8011
$RepoPort = 8012
$Api = "http://127.0.0.1:$GwPort"

Write-Host "== starting GitHub Agent (microservices+) ==" -ForegroundColor Cyan

foreach ($port in @($GwPort, $AgentPort, $RepoPort, 5173)) {
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

$feEnv = Join-Path $Frontend ".env"
Set-Content -Path $feEnv -Value "VITE_API_BASE=$Api" -Encoding ascii

$LogDir = Join-Path $Root "services\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$env:PYTHONPATH = "$Root;$Backend"
$env:GATEWAY_MODE = "micro"
$env:AGENT_SVC_URL = "http://127.0.0.1:$AgentPort"
$env:REPO_SVC_URL = "http://127.0.0.1:$RepoPort"
$env:REGISTRY_REDIS_URL = "redis://localhost:6379/0"
$env:INTERNAL_RPC_TOKEN = "dev-internal-rpc"

function Start-UvicornSvc($Module, $Port, $Name) {
  $out = Join-Path $LogDir "$Name.out.log"
  $err = Join-Path $LogDir "$Name.err.log"
  Start-Process -FilePath $Py -ArgumentList "-m","uvicorn",$Module,"--host","127.0.0.1","--port","$Port" `
    -WorkingDirectory $Root -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden | Out-Null
}

# Repo：Postgres + Redis/0 + 对外 GitHub RPC
$env:REDIS_URL = "redis://localhost:6379/0"
$env:SERVICE_NAME = "repo"
$env:SERVICE_ADVERTISE_URL = "http://127.0.0.1:$RepoPort"
$env:REPO_RPC_ENABLED = "false"
Start-UvicornSvc "services.repo_svc.main:app" $RepoPort "repo"

# Agent：Redis/1 独立库 + 经 Repo RPC 写 GitHub
$env:REDIS_URL = "redis://localhost:6379/1"
$env:SERVICE_NAME = "agent"
$env:SERVICE_ADVERTISE_URL = "http://127.0.0.1:$AgentPort"
$env:REPO_RPC_ENABLED = "true"
$env:REPO_RPC_URL = "http://127.0.0.1:$RepoPort"
Start-UvicornSvc "services.agent_svc.main:app" $AgentPort "agent"

Start-Sleep 2

# Gateway：发现优先，静态兜底
$env:REDIS_URL = "redis://localhost:6379/0"
$env:SERVICE_NAME = "gateway"
$env:SERVICE_ADVERTISE_URL = "http://127.0.0.1:$GwPort"
$env:REPO_RPC_ENABLED = "false"
Start-UvicornSvc "services.gateway.main:app" $GwPort "gateway"

if (Test-Path $Npm) {
  Start-Process -FilePath $Npm -ArgumentList "run","dev","--","--host","127.0.0.1","--port","5173" `
    -WorkingDirectory $Frontend -WindowStyle Hidden | Out-Null
} else {
  Write-Host "[!!] npm.cmd not found, start frontend manually" -ForegroundColor Yellow
}

Write-Host "waiting for gateway health..."
$ok = $false
for ($i = 1; $i -le 35; $i++) {
  Start-Sleep 1
  try {
    $h = Invoke-RestMethod "$Api/health" -TimeoutSec 2
    $disc = if ($h.discovery) { "disc=on" } else { "disc=?" }
    Write-Host ("[OK] mode=" + $h.mode + " agent=" + $h.services.agent + " repo=" + $h.services.repo + " rag=" + $h.rag_mode + " ckpt=" + $h.checkpointer + " " + $disc) -ForegroundColor Green
    $ok = $true
    break
  } catch {}
}
if (-not $ok) {
  Write-Host "[XX] gateway failed; see services\logs\*.err.log" -ForegroundColor Red
  exit 1
}

try {
  $idx = Invoke-RestMethod -Method Post "$Api/api/repos/guo-mz/-/index?path=README.md&ref=main" -TimeoutSec 60
  Write-Host ("[OK] " + ($idx.detail)) -ForegroundColor Green
} catch {
  Write-Host ("[!!] index: " + $_.Exception.Message) -ForegroundColor Yellow
}

Write-Host ""
Write-Host "UI:         http://127.0.0.1:5173/chat" -ForegroundColor Cyan
Write-Host "Gateway:    $Api/health  (discovery+breakers)" -ForegroundColor Cyan
Write-Host "Discovery:  $Api/internal/discovery" -ForegroundColor DarkGray
Write-Host "Agent RPC:  REPO_RPC_ENABLED → Repo /internal/v1/github/call" -ForegroundColor DarkGray
Write-Host "Redis:      Agent=/1  Repo+Registry=/0  Postgres=Repo" -ForegroundColor DarkGray
Write-Host "Monolith:   scripts\start-all.ps1 still works." -ForegroundColor DarkGray
Start-Process "http://127.0.0.1:5173/chat"
