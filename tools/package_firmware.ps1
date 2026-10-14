<#
.SYNOPSIS
    Builds the PRODUCT firmware (no development tools, no Wi-Fi credentials) and collects what a flasher needs into release\firmware\.

.DESCRIPTION
    Uses a build folder of its own (firmware\build\product), so the development build in firmware\build is not disturbed and its cached
    options (-DAPP_DEV_TOOLS=1 ...) cannot leak into the product image. Output, git-ignored:

        release\firmware\bootloader.bin           flash at 0x0
        release\firmware\partition-table.bin      flash at 0x8000
        release\firmware\deskmedia_display.bin    flash at 0x10000
        release\firmware\SHA256SUMS.txt
        release\firmware\FLASHING.txt             the esptool command, see docs\updating.md

    The three files are flashed SEPARATELY (never as one merged image: that would blank the display's storage and with it the pairing).

.PARAMETER SetupAddress
    The address the "Install DeskMedia Companion" screen prints, e.g. github.com/<owner>/<repo> (see firmware\main\app_config.h).
    Without it the project default is used.

.EXAMPLE
    .\tools\package_firmware.ps1
    .\tools\package_firmware.ps1 -SetupAddress example.org/deskmedia
#>
param(
    [string]$SetupAddress = '',
    [string]$OutDir = (Join-Path $PSScriptRoot '..\release\firmware')
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$buildRel = 'build\product'
$buildDir = Join-Path $root "firmware\$buildRel"

$idfArgs = @('-B', $buildRel, '-DAPP_DEV_TOOLS=0', '-DAPP_NO_WIFI=1', '-DAPP_UI_DEMO=0')
if ($SetupAddress) { $idfArgs += "-DDM_SETUP_ADDRESS=$SetupAddress" }
$idfArgs += 'build'

Write-Host "Building the product firmware in $buildDir ..."
& (Join-Path $PSScriptRoot 'idf.ps1') @idfArgs
if ($LASTEXITCODE -ne 0) { throw "the firmware build failed (exit code $LASTEXITCODE)" }

$files = [ordered]@{
    'bootloader.bin'         = Join-Path $buildDir 'bootloader\bootloader.bin'
    'partition-table.bin'    = Join-Path $buildDir 'partition_table\partition-table.bin'
    'deskmedia_display.bin'  = Join-Path $buildDir 'deskmedia_display.bin'
}
foreach ($f in $files.Values) { if (-not (Test-Path $f)) { throw "missing build output: $f" } }

# The product image must not contain the development tools (their log text is a reliable marker).
$app = [System.IO.File]::ReadAllBytes($files['deskmedia_display.bin'])
$text = [System.Text.Encoding]::ASCII.GetString($app)
foreach ($marker in @('debug reboot', 'idle_cfg applied', 'screenshot sent')) {
    if ($text.Contains($marker)) { throw "the image contains development tools ('$marker'): refusing to package it" }
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$sums = @()
foreach ($name in $files.Keys) {
    Copy-Item -LiteralPath $files[$name] -Destination (Join-Path $OutDir $name) -Force
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutDir $name)).Hash.ToLower()
    $sums += "$hash  $name"
}
$sums | Set-Content -Encoding ascii -LiteralPath (Join-Path $OutDir 'SHA256SUMS.txt')

@'
DeskMedia Display firmware - how to flash (details: docs\updating.md)

1. Exit DeskMedia Companion (tray icon > Exit): it holds the display's COM port.
2. Find the display's COM port (Device Manager > Ports > "USB Serial Device (COMn)").
3. Flash the three files SEPARATELY (python -m pip install esptool):

   python -m esptool --chip esp32s3 -p COMn -b 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m 0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 deskmedia_display.bin

Never erase the whole flash: that deletes the pairing and the idle-screen picture.
Recovery if something goes wrong: hold BOOT while plugging the USB cable in, then flash again.
'@ | Set-Content -Encoding ascii -LiteralPath (Join-Path $OutDir 'FLASHING.txt')

Write-Host ''
Write-Host "Done: $OutDir"
Get-Content -LiteralPath (Join-Path $OutDir 'SHA256SUMS.txt')
