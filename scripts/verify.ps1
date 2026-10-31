$ErrorActionPreference = 'Stop'
$workspace = Split-Path -Parent $PSScriptRoot
$python = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create the local virtual environment first: python -m venv .venv'
}
Push-Location $workspace
try {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed' }
    & $python -m compileall -q src
    if ($LASTEXITCODE -ne 0) { throw 'Compilation failed' }
    & (Join-Path $workspace '.venv\Scripts\lmos.exe') check
    if ($LASTEXITCODE -ne 0) { throw 'Database integrity check failed' }
} finally {
    Pop-Location
}
