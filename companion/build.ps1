<#
.SYNOPSIS
  Builds companion\dist\DeskMediaCompanion.exe (single file, no console window, no Python needed on the target PC).
  Includes the tray icon (pystray) and the Idle Screen settings window (tkinter / Tcl-Tk).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\companion\build.ps1
#>
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$py = Join-Path $here '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    python -m venv (Join-Path $here '.venv')
    & $py -m pip install --quiet -r (Join-Path $here 'requirements.txt')
}
& $py -m pip install --quiet --disable-pip-version-check pyinstaller

Push-Location $here
try {
    & $py -m PyInstaller --noconfirm --clean --onefile --noconsole `
        --name DeskMediaCompanion `
        --collect-submodules winrt `
        --collect-binaries winrt `
        --hidden-import winrt.windows.media.control `
        --hidden-import winrt.windows.storage.streams `
        --hidden-import winrt.windows.foundation `
        --hidden-import winrt.windows.foundation.collections `
        --collect-submodules pycaw `
        --collect-submodules comtypes `
        --hidden-import pystray._win32 --collect-submodules pystray `
        --exclude-module unittest --exclude-module pydoc `
        run_companion.py
} finally {
    Pop-Location
}
$exe = Join-Path $here 'dist\DeskMediaCompanion.exe'
if (Test-Path $exe) { "Built $exe ({0:N1} MB)" -f ((Get-Item $exe).Length / 1MB) } else { throw 'build failed' }
