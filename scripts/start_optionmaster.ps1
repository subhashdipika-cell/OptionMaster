Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "OptionMaster virtual environment not found. Create it with: python -m venv .venv"
}

Set-Location $root
& $python -m uvicorn optionmaster.main:app --app-dir backend --host 127.0.0.1 --port 8300
