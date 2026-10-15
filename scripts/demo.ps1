# GitHub Agent demo check - run before interview
# Usage: powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1
param(
  [string]$Api = "",
  [string]$Repo = "guo-mz/-"
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Api) {
  $feEnv = Join-Path $Root "frontend\.env"
  if (Test-Path $feEnv) {
    $line = Get-Content $feEnv | Where-Object { $_ -match '^VITE_API_BASE=' } | Select-Object -First 1
    if ($line) { $Api = ($line -replace '^VITE_API_BASE=', '').Trim() }
  }
}
if (-not $Api) { $Api = "http://127.0.0.1:8000" }

Write-Host "== GitHub Agent demo check ==" -ForegroundColor Cyan
Write-Host ("API  " + $Api)
Write-Host ("Repo " + $Repo)
Write-Host ""

function Ok([string]$msg) { Write-Host ("[OK] " + $msg) -ForegroundColor Green }
function Warn([string]$msg) { Write-Host ("[!!] " + $msg) -ForegroundColor Yellow }
function Bad([string]$msg) { Write-Host ("[XX] " + $msg) -ForegroundColor Red }

try {
  $h = Invoke-RestMethod ($Api + "/health") -TimeoutSec 5
  Ok ("health status=" + $h.status + " redis=" + $h.redis + " postgres/RAG=" + $h.postgres)
  if (-not $h.redis) { Warn "Redis offline -> in-memory fallback (ok for single-worker demo)" }
  if (-not $h.postgres) { Warn "Postgres offline -> RAG may use local fallback" }
} catch {
  Bad ("backend unreachable: " + $Api)
  exit 1
}

try {
  $permUrl = $Api + "/api/github/permissions?repo=" + [uri]::EscapeDataString($Repo)
  $p = Invoke-RestMethod $permUrl -TimeoutSec 30
  if ($p.can_write -or $p.can_create_branch -or $p.deploy_key_ok) {
    Ok ("write ok login=" + $p.login + " pat=" + $p.can_create_branch + " deploy=" + $p.deploy_key_ok)
  } else {
    Warn "no write access yet - add Deploy Key (Allow write access) or writable Token"
  }
} catch {
  Warn ("permission probe failed: " + $_.Exception.Message)
}

try {
  $paths = (Invoke-RestMethod ($Api + "/openapi.json") -TimeoutSec 10).paths.PSObject.Properties.Name
  $needs = @(
    "/api/chat/stream",
    "/api/github/permissions",
    "/api/github/deploy-key",
    "/api/repos/{owner}/{repo}/index"
  )
  foreach ($need in $needs) {
    if ($paths -contains $need) { Ok ("route " + $need) } else { Bad ("missing " + $need) }
  }
} catch {
  Warn "openapi read failed"
}

$parts = $Repo.Split("/")
if ($parts.Count -eq 2) {
  try {
    $idxUrl = $Api + "/api/repos/" + $parts[0] + "/" + $parts[1] + "/index?path=README.md&ref=main"
    $idx = Invoke-RestMethod -Method Post $idxUrl -TimeoutSec 60
    Ok ("RAG index README chunks=" + $idx.chunks)
  } catch {
    Warn ("index README failed (ignore if no Postgres): " + $_.Exception.Message)
  }
}
Write-Host ""
Write-Host "== paste these into Chat ==" -ForegroundColor Cyan
Write-Host "  1) review README.md with risk level and fix list"
Write-Host "  2) security scan README.md"
Write-Host "  3) apply last review fixes on a new branch and open PR"
Write-Host ""
Write-Host "UI:  http://127.0.0.1:5173/chat" -ForegroundColor Cyan
Write-Host "Doc: docs/面试讲点清单.md  (state machine / HITL / guardrails)" -ForegroundColor Cyan





