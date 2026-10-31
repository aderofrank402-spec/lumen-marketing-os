[CmdletBinding()]
param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
$promptPath = Join-Path $workspace 'config\department1_scheduled_review_prompt.md'
$lmosPath = Join-Path $workspace '.venv\Scripts\lmos.exe'
$runtimePath = Join-Path $workspace 'runtime\department1'
$binaryRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
$reviewModel = 'gpt-6.1-sol'
trap {
    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
    Add-Content -LiteralPath (Join-Path $runtimePath 'runner-errors.log') `
        -Value "$(Get-Date -Format o) $($_ | Out-String)"
    break
}

if (-not (Test-Path -LiteralPath $promptPath -PathType Leaf)) { throw "Review prompt missing: $promptPath" }
if (-not (Test-Path -LiteralPath $lmosPath -PathType Leaf)) { throw "OS executable missing: $lmosPath" }
if (-not (Test-Path -LiteralPath $binaryRoot -PathType Container)) { throw 'Bundled Codex CLI is not installed' }

# The npm CLI on PATH may be older than the desktop app's agent configuration.
$candidates = @(Get-ChildItem -LiteralPath $binaryRoot -Filter codex.exe -File -Recurse |
    Sort-Object LastWriteTimeUtc -Descending)
$codexPath = $null
foreach ($candidate in $candidates) {
    $versionText = (& $candidate.FullName -V 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $versionText -match 'codex-cli (\d+)\.(\d+)\.(\d+)') {
        if ([int]$Matches[1] -gt 0 -or [int]$Matches[2] -ge 159) {
            $codexPath = $candidate.FullName
            break
        }
    }
}
if (-not $codexPath) { throw 'No compatible bundled Codex CLI (0.159 or newer) was found' }

if ($CheckOnly) {
    [pscustomobject]@{ workspace = $workspace; codex = $codexPath; prompt = $promptPath; model = $reviewModel; ready = $true }
    return
}
if (Test-Path -LiteralPath (Join-Path $workspace 'PAUSE_ALL.lock') -PathType Leaf) {
    throw 'Global pause is active; review cycle not started'
}

$mutex = [Threading.Mutex]::new($false, 'Local\LumenMarketingOSDepartment1Reviews')
$acquired = $false
try {
    try { $acquired = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { throw 'Another Department 1 review cycle is already running' }

    New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
    $logPath = Join-Path $runtimePath "codex-review-$stamp.log"
    $resultPath = Join-Path $runtimePath "codex-review-$stamp.result.txt"
    Push-Location -LiteralPath $workspace
    try {
        # Windows PowerShell 5 promotes native stderr to a terminating error under Stop.
        # Codex writes its normal startup banner to stderr, so preserve the exit code instead.
        $ErrorActionPreference = 'Continue'
        Get-Content -LiteralPath $promptPath -Raw |
            & $codexPath exec --ignore-user-config --skip-git-repo-check `
            -m $reviewModel -C $workspace --sandbox danger-full-access `
            --output-last-message $resultPath - `
            2>&1 | Tee-Object -FilePath $logPath
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = 'Stop'; Pop-Location }
    if ($exitCode -ne 0) { throw "Codex review cycle exited $exitCode; inspect $logPath" }
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
        throw "Codex gave no final result; inspect $logPath"
    }
    $result = (Get-Content -LiteralPath $resultPath -Raw).Trim()
    if (-not $result.EndsWith('CYCLE_COMPLETED')) {
        throw "Codex did not complete the cycle; inspect $resultPath and $logPath"
    }
    Write-Output "Department 1 review cycle completed; log: $logPath"
}
finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
