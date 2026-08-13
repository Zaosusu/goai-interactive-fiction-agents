param(
    [int]$Port = 5173,
    [string]$ApiBase = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$FrontendRoot = Join-Path $ProjectRoot "剧本杀\qingmeng-agent-frontend"

if (-not (Test-Path -LiteralPath $FrontendRoot)) {
    throw "Cannot find Qingmeng frontend at: $FrontendRoot"
}

Set-Location -LiteralPath $FrontendRoot
$env:VITE_API_BASE = $ApiBase

npm run dev -- --host 127.0.0.1 --port $Port
