param(
    [int]$Port = 8000,
    [string]$HostName = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

Set-Location -LiteralPath $ProjectRoot
$env:PYTHONPATH = $ProjectRoot

python -m uvicorn app.main:app `
    --reload `
    --reload-dir "$ProjectRoot\app" `
    --reload-dir "$ProjectRoot\static" `
    --host $HostName `
    --port $Port
