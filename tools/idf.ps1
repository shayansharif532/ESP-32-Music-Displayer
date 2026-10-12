<#
.SYNOPSIS
    Run idf.py inside the ESP-IDF v5.5.5 environment that Espressif's EIM installed, from anywhere.

.DESCRIPTION
    Dot-sources EIM's generated activation profile (sets IDF_PATH, the toolchain PATH, ccache, ...),
    switches to ./firmware and runs idf.py with whatever arguments you pass.

.EXAMPLE
    .\tools\idf.ps1 --version
    .\tools\idf.ps1 build
    .\tools\idf.ps1 -p COM5 flash
    .\tools\idf.ps1 -DM1_PROBE=0 build          # pristine Waveshare demo, no diagnostic sidecar

    To run something other than idf.py in the same environment:  $env:IDF_RUN = 'cmake --version'; .\tools\idf.ps1
#>
$ErrorActionPreference = 'Stop'

$IdfVersion = 'v5.5.5'          # keep in sync with README.md / CLAUDE.md

# Find EIM's activation script without assuming where ESP-IDF was installed:
#   1. $env:IDF_ACTIVATE_SCRIPT (explicit override)
#   2. EIM's registry of installs, eim_idf.json (default C:\Espressif\tools, or $env:ESP_IDF_JSON_PATH)
#   3. the conventional file name in C:\Espressif\tools
function Find-IdfActivationScript {
    if ($env:IDF_ACTIVATE_SCRIPT -and (Test-Path $env:IDF_ACTIVATE_SCRIPT)) { return $env:IDF_ACTIVATE_SCRIPT }
    $dirs = @($env:ESP_IDF_JSON_PATH, 'C:\Espressif\tools', (Join-Path $env:USERPROFILE '.espressif\tools')) | Where-Object { $_ }
    foreach ($dir in $dirs) {
        $json = Join-Path $dir 'eim_idf.json'
        if (Test-Path $json) {
            $cfg = Get-Content $json -Raw | ConvertFrom-Json
            $hit = $cfg.idfInstalled | Where-Object { $_.name -eq $IdfVersion -and $_.activationScript -and (Test-Path $_.activationScript) } | Select-Object -First 1
            if ($hit) { return $hit.activationScript }
        }
    }
    $conventional = "C:\Espressif\tools\Microsoft.$IdfVersion.PowerShell_profile.ps1"
    if (Test-Path $conventional) { return $conventional }
    return $null
}

$activate = Find-IdfActivationScript
if (-not $activate) {
    throw "ESP-IDF $IdfVersion is not installed (or EIM's registry was not found).`nInstall it with:`n  winget install --id Espressif.EIM-CLI --exact`n  (open a NEW terminal)  eim install -i $IdfVersion -t esp32s3 -p C:\esp -n true --do-not-track true --cleanup true --skip-components-download true`nor set `$env:IDF_ACTIVATE_SCRIPT to the activation script of an existing install."
}

# EIM's profile prints a long banner; keep our output readable.
. $activate *> $null

$firmware = Join-Path $PSScriptRoot '..\firmware'
Push-Location -LiteralPath $firmware     # restored below: this script runs in the caller's session
try {
    if ($env:IDF_RUN) {
        Invoke-Expression $env:IDF_RUN
    } else {
        & "$env:IDF_PYTHON_ENV_PATH\Scripts\python.exe" "$env:IDF_PATH\tools\idf.py" @args
    }
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
