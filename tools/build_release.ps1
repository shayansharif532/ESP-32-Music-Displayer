<#
.SYNOPSIS
  Builds the DeskMedia Companion release: the PyInstaller folder, a self-test of it, and the Inno Setup installer.

.DESCRIPTION
  1. creates / reuses companion\.venv and installs requirements.txt, requirements-build.txt (PyInstaller) and, until requirements.txt
     lists it, pyserial
  2. generates companion\version_info.txt and installer\version.iss from desk_media_companion.__version__
  3. runs PyInstaller on companion\DeskMediaCompanion.spec (onedir: companion\dist\DeskMediaCompanion\DeskMediaCompanion.exe + _internal\)
  4. checks the result: windowless x64 exe, manifest, version resource, icon, bundled assets, then runs --version and --selftest
     with a scrubbed environment
  5. converts THIRD_PARTY_NOTICES.md and compiles installer\DeskMediaCompanion.iss with Inno Setup 6
     -> installer\output\DeskMediaCompanion-Setup-<version>.exe  (+ a .sha256 file)

  Output goes to git-ignored folders only: companion\dist, companion\build, installer\output, installer\build.
  Deterministic where practical: SOURCE_DATE_EPOCH is the last commit's time, PYTHONHASHSEED is fixed, no UPX.

.PARAMETER SkipInstaller   Stop after the PyInstaller folder and its checks (what companion\build.ps1 does).
.PARAMETER SkipSelfTest    Do not run the packaged exe's --version / --selftest.
.PARAMETER RunTests        Run the unit tests (companion\tests) before building.
.PARAMETER Clean           Delete companion\build and companion\dist first (PyInstaller is run with --clean in any case).
.PARAMETER Python          Interpreter used to CREATE companion\.venv when it does not exist (default: py -3.12, then python).
.PARAMETER ProjectUrl      Home page shown in Programs and Features. Default: the https URL of the 'origin' remote; '' = none.
.PARAMETER SignTool        Optional code signing. The command Inno Setup runs for every file to sign, with $f for the file, e.g.
                           'signtool.exe sign /fd sha256 /td sha256 /tr http://timestamp.example.org /f C:\keys\code.pfx /p secret $f'
                           Signs the installer, the uninstaller and DeskMediaCompanion.exe. Not needed: unsigned builds work.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_release.ps1
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_release.ps1 -SkipInstaller -ProjectUrl ''
#>
[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [switch]$SkipSelfTest,
    [switch]$RunTests,
    [switch]$Clean,
    [string]$Python = '',
    [string]$ProjectUrl = $null,
    [string]$SignTool = ''
)
$ErrorActionPreference = 'Stop'

$repo      = Split-Path -Parent $PSScriptRoot
$companion = Join-Path $repo 'companion'
$installer = Join-Path $repo 'installer'
$tools     = Join-Path $repo 'tools'
$venv      = Join-Path $companion '.venv'
$py        = Join-Path $venv 'Scripts\python.exe'

function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }

# Runs a native command; stderr output of tools such as PyInstaller is progress text, not an error, so only the exit code counts.
function Invoke-Tool([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($Exe)) failed with exit code $LASTEXITCODE" }
}

function Find-BasePython {
    if ($Python) { return $Python }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $found = & $launcher.Source -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $found) { return $found.Trim() }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw 'No Python found. Install Python 3.12 (64-bit) or pass -Python <path to python.exe>.'
}

# ---- 1. the venv ---------------------------------------------------------------------------------------------------------------
Step 'Python environment'
if (-not (Test-Path $py)) {
    $base = Find-BasePython
    Write-Host "creating $venv with $base"
    Invoke-Tool $base @('-m', 'venv', $venv)
}
$info = & $py -c "import sys, struct; print(sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8)"
$parts = $info.Trim().Split(' ')
if ([int]$parts[2] -ne 64) { throw "companion\.venv is a $($parts[2])-bit Python; the release needs 64-bit Python 3.12." }
if ([int]$parts[0] -ne 3 -or [int]$parts[1] -lt 10) { throw "companion\.venv has Python $($parts[0]).$($parts[1]); 3.10 or newer is required (3.12 recommended)." }
Invoke-Tool $py @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-r', (Join-Path $companion 'requirements.txt'),
                  '-r', (Join-Path $companion 'requirements-build.txt'))
if (-not (Select-String -Path (Join-Path $companion 'requirements.txt') -Pattern '^\s*pyserial' -Quiet)) {
    Write-Host 'pyserial is not in requirements.txt yet: installing it for the build anyway (the USB link needs it)'
    Invoke-Tool $py @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', 'pyserial>=3.5')
}

if ($RunTests) {
    Step 'Unit tests'
    Push-Location $companion
    try { Invoke-Tool $py @('-m', 'unittest', 'discover', '-s', 'tests') } finally { Pop-Location }
}

# ---- 2. version files ----------------------------------------------------------------------------------------------------------
Step 'Version files'
$metaArgs = @((Join-Path $tools 'release_meta.py'))
if ($PSBoundParameters.ContainsKey('ProjectUrl')) { $metaArgs += "--project-url=$ProjectUrl" }       # one token: PowerShell 5.1 drops an empty argument
Invoke-Tool $py $metaArgs
$version = (& $py (Join-Path $tools 'release_meta.py') --print-version).Trim()

# ---- 3. PyInstaller ------------------------------------------------------------------------------------------------------------
Step "PyInstaller (onedir) - version $version"
if ($Clean) {
    foreach ($d in @('build', 'dist')) {
        $p = Join-Path $companion $d
        if (Test-Path $p) { Remove-Item -LiteralPath $p -Recurse -Force }
    }
}
$epoch = ''
try { $epoch = (git -C $repo log -1 --format=%ct 2>$null) } catch { $epoch = '' }
if ($epoch) { $env:SOURCE_DATE_EPOCH = "$epoch".Trim() }       # reproducible timestamps in the PE headers
$env:PYTHONHASHSEED = '0'
Push-Location $companion
try {
    Invoke-Tool $py @('-m', 'PyInstaller', '--noconfirm', '--clean', '--log-level', 'WARN', '--distpath', 'dist', '--workpath', 'build',
                      'DeskMediaCompanion.spec')
} finally { Pop-Location }

$dist = Join-Path $companion 'dist\DeskMediaCompanion'
$exe  = Join-Path $dist 'DeskMediaCompanion.exe'
if (-not (Test-Path $exe)) { throw "PyInstaller did not produce $exe" }

# ---- 4. checks -----------------------------------------------------------------------------------------------------------------
Step 'Checks of the packaged app'
$assetsSrc = Join-Path $companion 'desk_media_companion\assets'
if ((Test-Path $assetsSrc) -and (Get-ChildItem $assetsSrc -File -ErrorAction SilentlyContinue)) {
    $assetsDst = Join-Path $dist '_internal\desk_media_companion\assets'
    foreach ($f in Get-ChildItem $assetsSrc -File) {
        if (-not (Test-Path (Join-Path $assetsDst $f.Name))) { throw "asset $($f.Name) is missing from the packaged app" }
    }
    Write-Host "assets bundled: $((Get-ChildItem $assetsSrc -File | ForEach-Object Name) -join ', ')"
} else {
    Write-Host 'no assets folder in the source tree: nothing to bundle'
}
if ($SkipSelfTest) {
    Write-Host 'self-test skipped'
} else {
    Invoke-Tool $py @((Join-Path $tools 'packaging_checks.py'), 'exe', $exe, '--version', $version)
}
$distBytes = (Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ('app folder: {0:N1} MB in {1} files; DeskMediaCompanion.exe {2:N1} MB' -f ($distBytes / 1MB), (Get-ChildItem $dist -Recurse -File).Count, ((Get-Item $exe).Length / 1MB))

if ($SkipInstaller) {
    Write-Host ''
    Write-Host "Built $exe" -ForegroundColor Green
    return
}

# ---- 5. installer --------------------------------------------------------------------------------------------------------------
Step 'Installer (Inno Setup)'
$iscc = $null
$candidates = @($env:ISCC,
                (Get-Command ISCC.exe -ErrorAction SilentlyContinue | ForEach-Object Source),
                (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
                (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'),
                (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'))
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $iscc = $c; break } }
if (-not $iscc) { throw 'Inno Setup 6 (ISCC.exe) was not found. Install it (https://jrsoftware.org/isinfo.php) or set $env:ISCC.' }
Write-Host "compiler: $iscc"

Invoke-Tool $py @((Join-Path $installer 'make_notices.py'))
foreach ($f in @('assets\DeskMediaCompanion.ico', 'assets\wizard-large-100.bmp', 'assets\wizard-small-100.bmp')) {
    if (-not (Test-Path (Join-Path $installer $f))) { throw "installer\$f is missing; run installer\make_assets.py" }
}
$isccArgs = @('/Q')
if ($SignTool) {
    $isccArgs += @('/DSignEnabled=1', "/Ssigntool=$SignTool")
    Write-Host 'code signing: enabled (SignTool command given)'
} else {
    Write-Host 'code signing: none (unsigned build; Windows SmartScreen will show "unknown publisher", see installer\README.md)'
}
Push-Location $installer
try { Invoke-Tool $iscc ($isccArgs + @('DeskMediaCompanion.iss')) } finally { Pop-Location }

$setup = Join-Path $installer "output\DeskMediaCompanion-Setup-$version.exe"
if (-not (Test-Path $setup)) { throw "Inno Setup did not produce $setup" }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLower()
Set-Content -LiteralPath "$setup.sha256" -Value "$hash *$([IO.Path]::GetFileName($setup))" -Encoding Ascii

Write-Host ''
Write-Host ('Installer: {0} ({1:N1} MB)' -f $setup, ((Get-Item $setup).Length / 1MB)) -ForegroundColor Green
Write-Host "SHA-256:   $hash"
Write-Host 'Next: python tools\installer_smoke_test.py "<installer>" on a clean-machine-style run (see installer\README.md).'
