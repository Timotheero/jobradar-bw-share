param(
    [switch]$SeedDemo
)

$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Project ".venv\Scripts\python.exe"
$EnvironmentFile = Join-Path $Project ".env"
$DevelopmentTemplate = Join-Path $Project ".env.development.example"

if (-not (Test-Path $Python)) {
    python -m venv (Join-Path $Project ".venv")
}

& $Python -m pip install -e "${Project}[dev]"
if (-not (Test-Path -LiteralPath $EnvironmentFile)) {
    Copy-Item -LiteralPath $DevelopmentTemplate -Destination $EnvironmentFile
    Write-Host "Lokale, netzwerkfreie Entwicklungskonfiguration wurde als .env angelegt."
}

# Der Entwicklungshelfer darf auch bei einer bereits vorhandenen .env keine
# externen Quellen oder den Codex App Server aktivieren.
$env:CRAWLING_ENABLED = "false"
$env:CODEX_ENABLED = "false"

& $Python -m jobradar.cli init-db
if ($SeedDemo) {
    & $Python -m jobradar.cli seed-demo
}
& $Python -m jobradar.cli serve
