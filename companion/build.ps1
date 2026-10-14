<#
.SYNOPSIS
  Builds the packaged companion: companion\dist\DeskMediaCompanion\DeskMediaCompanion.exe (a folder with the exe and its runtime;
  no console window, no Python needed on the target PC). A thin entry: the work is done by tools\build_release.ps1, driven by
  companion\DeskMediaCompanion.spec. It stops before the Windows installer; run tools\build_release.ps1 without -SkipInstaller for that.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\companion\build.ps1
#>
[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$SkipSelfTest,
    [switch]$RunTests,
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$release = Join-Path (Split-Path -Parent $PSScriptRoot) 'tools\build_release.ps1'
& $release -SkipInstaller -Clean:$Clean -SkipSelfTest:$SkipSelfTest -RunTests:$RunTests -Python $Python
