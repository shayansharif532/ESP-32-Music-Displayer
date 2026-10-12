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

$activate = 'C:\Espressif\tools\Microsoft.v5.5.5.PowerShell_profile.ps1'
if (-not (Test-Path $activate)) {
    throw "ESP-IDF activation script not found: $activate`nInstall it with:  eim install -i v5.5.5 -t esp32s3 -p C:\esp -n true --do-not-track true"
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
