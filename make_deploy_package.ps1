$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$buildDir = Join-Path $root "deploy_package"
$zipPath = Join-Path $root "nexaflow-gateway-deploy.zip"

if (Test-Path $buildDir) {
    Remove-Item -LiteralPath $buildDir -Recurse -Force
}

New-Item -ItemType Directory -Path $buildDir | Out-Null

$files = @(
    "main.py",
    "requirements.txt",
    "Procfile",
    "runtime.txt",
    "README.md",
    "DEPLOYMENT.md",
    "STRIPE_SETUP.md",
    "LEGAL.md",
    ".env.example",
    ".gitignore"
)

foreach ($file in $files) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination $buildDir
}

if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

Compress-Archive -Path (Join-Path $buildDir "*") -DestinationPath $zipPath

Write-Output "Created $zipPath"
