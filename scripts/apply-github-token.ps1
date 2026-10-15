# 用法: .\apply-github-token.ps1 -Token "github_pat_xxx" [-Repo "guo-mz/-"]
param(
  [Parameter(Mandatory = $true)][string]$Token,
  [string]$Repo = "guo-mz/-",
  [string]$Api = "http://127.0.0.1:8000"
)
$ErrorActionPreference = "Stop"
$body = @{ token = $Token.Trim(); repo = $Repo } | ConvertTo-Json
$r = Invoke-RestMethod -Method Post -Uri "$Api/api/github/token" -ContentType "application/json" -Body $body
$r | ConvertTo-Json -Depth 6
if (-not $r.ok) {
  Write-Host "Token 已写入，但写权限探测失败。请确认 Contents / Pull requests = Read and write。" -ForegroundColor Yellow
  exit 2
}
Write-Host "写权限正常，可以回编辑器点保存上传。" -ForegroundColor Green
