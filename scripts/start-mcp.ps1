# MCP Server：HTTP JSON-RPC :8013（验收）+ 打印 Cursor stdio 配置提示

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Py = Join-Path $Backend ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
  $Py = "python"
}

Write-Host "== GitHub Agent MCP =="
Write-Host "HTTP:  http://127.0.0.1:8013/health  /tools  POST /mcp"
Write-Host "stdio: cd backend; .\.venv\Scripts\python.exe -m app.mcp_server"
Write-Host "Cursor config example: scripts\cursor-mcp.example.json"
Write-Host ""

$env:PYTHONPATH = "$Root;$Backend"
Set-Location $Root
& $Py -m uvicorn services.mcp_svc.main:app --host 127.0.0.1 --port 8013
