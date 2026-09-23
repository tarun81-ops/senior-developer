<#
.SYNOPSIS
    One-shot setup for senior-developer-agents on Windows.

.DESCRIPTION
    Creates the virtual environment, installs dependencies, copies
    .env.example to .env if you do not have one yet, then runs the offline
    checks (pytest + doctor + demo). Nothing here calls a paid API or needs
    a key: the mock providers prove the pipeline works first.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
#>
[CmdletBinding()]
param(
    # Python launcher to use, e.g. -PythonLauncher py -PythonVersion 3.12
    [string]$PythonLauncher = "py",
    [string]$PythonVersion = "3.12",
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($message) { Write-Host "`n== $message" -ForegroundColor Cyan }

# --- 1. Python -------------------------------------------------------------
Write-Step "Locating Python $PythonVersion+"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    # Try the requested launcher/version first, then fall back to whatever
    # `python` is on PATH (doctor will warn if it is too new or too old).
    $created = $false
    foreach ($attempt in @(
        @{ Exe = $PythonLauncher; Args = @($PythonVersion, "-m", "venv", ".venv") },
        @{ Exe = "python";       Args = @("-m", "venv", ".venv") }
    )) {
        try {
            & $attempt.Exe @($attempt.Args) | Out-Null
            if ($LASTEXITCODE -eq 0 -and (Test-Path $venvPython)) { $created = $true; break }
        } catch { }
    }
    if (-not $created) {
        Write-Error "Could not create .venv. Install Python 3.12 from https://python.org/downloads and re-run."
        exit 1
    }
} else {
    Write-Host "   .venv already exists, reusing it."
}

# --- 2. Dependencies -------------------------------------------------------
Write-Step "Installing dependencies"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }
Write-Host "   requirements.txt installed."

# --- 3. .env ---------------------------------------------------------------
Write-Step "Checking .env"
$envFile = Join-Path $repoRoot ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $repoRoot ".env.example") $envFile
    Write-Host "   created .env from .env.example - paste your keys into it." -ForegroundColor Yellow
    Write-Host "     GEMINI_API_KEY     https://aistudio.google.com/apikey"
    Write-Host "     GROQ_API_KEY       https://console.groq.com/keys"
    Write-Host "     OPENROUTER_API_KEY https://openrouter.ai/keys"
    Write-Host "   (optional: the offline mocks work with zero keys)"
} else {
    Write-Host "   .env already exists, left untouched."
}

# --- 4. folders the app writes to -----------------------------------------
Write-Step "Creating runtime folders"
foreach ($folder in @("data", "workspace")) {
    $path = Join-Path $repoRoot $folder
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path | Out-Null }
}
Write-Host "   data\ and workspace\ ready."

if ($SkipChecks) {
    Write-Host "`nSetup complete (checks skipped). Next: .\.venv\Scripts\python -m backend.cli doctor" -ForegroundColor Green
    exit 0
}

# --- 5. offline verification ----------------------------------------------
Write-Step "Running tests (offline, no keys needed)"
& $venvPython -m pytest
if ($LASTEXITCODE -ne 0) {
    Write-Error "Tests failed - fix the errors above before continuing."
    exit $LASTEXITCODE
}

Write-Step "Running doctor"
& $venvPython -m backend.cli doctor
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Step "Running the offline failover demo"
& $venvPython -m backend.cli demo --quiet
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`nSetup complete. Try it:" -ForegroundColor Green
Write-Host "  .\.venv\Scripts\python -m backend.cli ask `"explain git rebase in 3 lines`""
Write-Host "  .\.venv\Scripts\python -m backend.cli providers      # keys, quota, cooldowns"
Write-Host "  .\.venv\Scripts\python -m backend.cli demo           # watch failover happen"

