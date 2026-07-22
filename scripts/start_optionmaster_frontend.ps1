param(
    [int]$Port = 5275
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

& $python -m http.server $Port --bind 127.0.0.1 --directory (Join-Path $projectRoot "frontend")
