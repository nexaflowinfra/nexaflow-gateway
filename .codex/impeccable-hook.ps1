$ErrorActionPreference = "SilentlyContinue"

$root = (& git rev-parse --show-toplevel 2>$null)
if (-not $root) {
    $root = (Get-Location).Path
}

$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) {
    $candidate = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    if (Test-Path $candidate) {
        $node = $candidate
    }
}

if (-not $node) {
    Write-Output "Impeccable hook skipped: Node.js not found."
    exit 0
}

$script = Join-Path $root ".agents\skills\impeccable\scripts\hook.mjs"
if (-not (Test-Path $script)) {
    Write-Output "Impeccable hook skipped: skill script not found."
    exit 0
}

& $node $script
if ($LASTEXITCODE -ne $null) {
    exit $LASTEXITCODE
}
