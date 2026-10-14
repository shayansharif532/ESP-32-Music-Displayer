; =====================================================================================================================
;  DeskMedia Companion - Windows installer (Inno Setup 6.3 or newer; built by tools\build_release.ps1)
;
;  Per-user install, no administrator rights, no reboot, no firewall or security setting touched.
;  Needs before it compiles:  version.iss (tools\release_meta.py), build\THIRD_PARTY_NOTICES.txt (installer\make_notices.py)
;  and the PyInstaller folder companion\dist\DeskMediaCompanion\  - tools\build_release.ps1 produces all three.
;
;  Command line (see installer\README.md):
;    /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR="..." /TASKS="autostart,!desktopicon" /LOG="..."
;    /NOLAUNCH         do not start the companion after installing (also when silent)
;    /NOICONS          do not create the Start menu shortcut
;    /SKIPMUTEXCHECK   do not refuse to run while another copy of the companion is running (automated tests only)
;  Uninstaller:  unins000.exe /VERYSILENT /SUPPRESSMSGBOXES [/DELETEDATA]
; =====================================================================================================================

#include "version.iss"

#define AppName        "DeskMedia Companion"
#define AppExeName     "DeskMediaCompanion.exe"
#define AppPublisher   "DeskMedia Display project"

; Overridable with ISCC /D... only for tests (a throw-away build that must not collide with, or touch, a real installation: another
; AppId, another auto-start value, another data folder). tools\installer_flow_test.py builds such a variant; the shipped
; installer always uses the defaults below.
#ifndef AppIdValue
  #define AppIdValue   "73454063-9F9F-4C5A-9818-1D7B6B45C5F7"      ; fixed for ever: it identifies the installation across upgrades
#endif
#ifndef RunValueName
  #define RunValueName "DeskMediaCompanion"      ; the HKCU\...\Run value; the same name autostart.py (the tray toggle) uses
#endif
#ifndef DataDirExpr
  #define DataDirExpr  "{localappdata}\DeskMediaCompanion"       ; settings, pairing and log live here, outside the install folder
#endif
#ifndef SourceDir
  #define SourceDir    "..\companion\dist\DeskMediaCompanion"
#endif
#ifndef OutputBase
  #define OutputBase   "DeskMediaCompanion-Setup-" + AppVersion
#endif

[Setup]
AppId={{{#AppIdValue}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppCopyright={#AppCopyright}
#ifdef AppURL
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
#endif
VersionInfoVersion={#AppVersionInfo}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
VersionInfoProductName=DeskMedia Display
VersionInfoProductVersion={#AppVersionInfo}
VersionInfoCopyright={#AppCopyright}

; --- who, where, for what -------------------------------------------------------------------------------------------
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
; Pages: Welcome - Third-party notices - Options (tasks) - install - Finish. No folder, Start-menu-group or "ready" page: three clicks on Next.
DisableWelcomePage=no
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The media-session API (Windows.Media.Control) first shipped with Windows 10 1809.
MinVersion=10.0.17763

; --- upgrade / running copy -----------------------------------------------------------------------------------------
; The companion's single-instance lock is the mutex Local\DeskMediaCompanion (taken by every instance that may use USB; since the
; USB-first release it does not depend on an HTTP port, and a LAN-only development instance takes only Local\DeskMediaCompanion-lan-<port>,
; which is deliberately not checked here). InitializeSetup first asks a running companion of this installation to quit
; (a named event, not HTTP), so Setup's "the application is running" check only stops an install when some other copy is running;
; Setup's Restart Manager step (CloseApplications) is the safety net for files still in use.
AppMutex={code:GetAppMutex}
CloseApplications=yes
RestartApplications=no
; Setup's own memory of the last task selection is switched off: on an upgrade it would override /TASKS (a silent upgrade could
; not switch a task off) and it cannot know what the tray menu changed since. The code below starts from the current state instead.
UsePreviousTasks=no
SetupMutex=DeskMediaCompanionSetup

; --- look ---------------------------------------------------------------------------------------------------------------
WizardStyle=modern
WizardImageFile=assets\wizard-large-100.bmp,assets\wizard-large-150.bmp,assets\wizard-large-200.bmp
WizardSmallImageFile=assets\wizard-small-100.bmp,assets\wizard-small-150.bmp,assets\wizard-small-200.bmp
SetupIconFile=assets\DeskMediaCompanion.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
; The project has no licence file yet: show the third-party notices as information (no "I accept" page).
InfoBeforeFile=build\THIRD_PARTY_NOTICES.txt

; --- output -------------------------------------------------------------------------------------------------------------
OutputDir=output
OutputBaseFilename={#OutputBase}
Compression=lzma2/max
SolidCompression=yes
TimeStampsInUTC=yes
#ifdef SignEnabled
; Code signing is optional: tools\build_release.ps1 -SignTool "<command with $f>" defines SignEnabled and the "signtool" command.
SignTool=signtool
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This installs [name/ver], the small program that lets your DeskMedia display show what is playing on this PC.%n%nNo administrator rights are needed.
WizardInfoBefore=Third-party notices
InfoBeforeLabel=DeskMedia Companion includes open-source software. Their notices:
InfoBeforeClickLabel=Click Next to continue.
FinishedHeadingLabel=DeskMedia Companion is ready
FinishedLabel=Plug the display into a USB port of this PC. It is detected automatically; the first time, confirm the pairing code on the display and on the PC.
FinishedLabelNoIcons=Plug the display into a USB port of this PC. It is detected automatically; the first time, confirm the pairing code on the display and on the PC.

[Tasks]
Name: "autostart";   Description: "Start {#AppName} when I sign in to Windows"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut";                   GroupDescription: "Options:"; Flags: unchecked

[Files]
#ifdef SignEnabled
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion sign
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "{#AppExeName}"; Flags: ignoreversion recursesubdirs createallsubdirs
#else
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif
Source: "build\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSES\*"; DestDir: "{app}\LICENSES"; Flags: ignoreversion

[InstallDelete]
; An upgrade replaces the whole runtime folder, so files a newer version no longer ships do not pile up.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Comment: "Shows what is playing on this PC on your DeskMedia display"; Check: WantStartMenuIcon
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; Same value name and command as the tray menu's "Start with Windows" (autostart.py), so the two always agree.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#RunValueName}"; \
  ValueData: """{app}\{#AppExeName}"" --quiet"; Flags: uninsdeletevalue; Tasks: autostart; Check: WriteAutostart

[Run]
; Not skipped when silent: an unattended install ends with a running companion (use /NOLAUNCH to prevent it).
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; Flags: nowait postinstall; Check: WantLaunch

[UninstallRun]
Filename: "{app}\{#AppExeName}"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitCompanion"

[UninstallDelete]
; The program writes nothing into its own folder; these are only here so a leftover runtime folder cannot block removing {app}.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\LICENSES"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppIdValue}}_is1';

var
  IsUpgrade: Boolean;       { an installation of this product is already registered }
  TasksSynced: Boolean;

{ True when the command line contains the switch (case-insensitive), e.g. /NOLAUNCH. Works in Setup and in the uninstaller. }
function HasSwitch(const Name: string): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

function WantLaunch: Boolean;
begin
  Result := not HasSwitch('/NOLAUNCH');
end;

{ The single-instance mutex of the companion (see the [Setup] comment; the same name is created by __main__.py as Local\DeskMediaCompanion).
  /SKIPMUTEXCHECK switches the check off: it is for automated tests on a machine where some other copy (e.g. a developer's) is running
  on purpose. }
function GetAppMutex(Param: string): string;
begin
  if HasSwitch('/SKIPMUTEXCHECK') then
    Result := ''
  else
    Result := 'DeskMediaCompanion';
end;

function WantStartMenuIcon: Boolean;
begin
  Result := not HasSwitch('/NOICONS');
end;

{ Asks a running companion (started from the exe at ExePath) to quit and waits for it. False = it is still running. }
function StopCompanion(const ExePath: string): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if FileExists(ExePath) then
  begin
    Log('Asking a running DeskMedia Companion to quit: ' + ExePath);
    if Exec(ExePath, '--quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      Result := (ResultCode = 0)
    else
      Log('Could not run --quit (' + SysErrorMessage(ResultCode) + ')');
  end;
end;

function InitializeSetup: Boolean;
var
  Dir: string;
begin
  Result := True;
  IsUpgrade := RegQueryStringValue(HKCU, UninstallKey, 'Inno Setup: App Path', Dir);
  { Upgrade of a running copy: end it before Setup's own "application is running" check. }
  if IsUpgrade then
    StopCompanion(AddBackslash(Dir) + '{#AppExeName}');
end;

{ ---- "start with Windows" -------------------------------------------------------------------------------------------------
  Setup remembers what it did last time, but the tray menu's "Start with Windows" may have changed it since: an upgrade must
  not switch back on what the user switched off (or off what they switched on).
    wizard   the checkbox starts as the entry is now; unticking it on an upgrade removes our entry
    silent   with an explicit /TASKS or /MERGETASKS the command line decides; without one the entry is left as it is }

function HasSwitchPrefix(const Prefix: string): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(Copy(ParamStr(I), 1, Length(Prefix)), Prefix) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

function CommandLineTasks: Boolean;
begin
  Result := HasSwitchPrefix('/TASKS=') or HasSwitchPrefix('/MERGETASKS=');
end;

function ExplicitTaskChoice: Boolean;
begin
  Result := (not WizardSilent) or CommandLineTasks;
end;

{ A Run value exists and starts the exe of this installation (not, say, a development copy). }
function OurRunValue: Boolean;
var
  Cmd: string;
begin
  Result := RegQueryStringValue(HKCU, RunKey, '{#RunValueName}', Cmd) and
            (Pos(Lowercase(ExpandConstant('{app}\{#AppExeName}')), Lowercase(Cmd)) > 0);
end;

{ Check for the [Registry] entry of the "autostart" task. }
function WriteAutostart: Boolean;
begin
  Result := True;
  if IsUpgrade and (not ExplicitTaskChoice) then
    Result := RegValueExists(HKCU, RunKey, '{#RunValueName}');
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  { (also called while a silent Setup steps through its pages, which is why the command line is checked first) }
  if (CurPageID = wpSelectTasks) and IsUpgrade and (not TasksSynced) and (not CommandLineTasks) then
  begin
    TasksSynced := True;
    if RegValueExists(HKCU, RunKey, '{#RunValueName}') then
      WizardSelectTasks('autostart')
    else
      WizardSelectTasks('!autostart');
    if FileExists(ExpandConstant('{autodesktop}\{#AppName}.lnk')) then
      WizardSelectTasks('desktopicon');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    Log('upgrade=' + IntToStr(Ord(IsUpgrade)) + ' silent=' + IntToStr(Ord(WizardSilent)) +
        ' explicit task choice=' + IntToStr(Ord(ExplicitTaskChoice)) +
        ' autostart task selected=' + IntToStr(Ord(WizardIsTaskSelected('autostart'))) +
        ' current Run value present=' + IntToStr(Ord(RegValueExists(HKCU, RunKey, '{#RunValueName}'))));
  if (CurStep = ssPostInstall) and ExplicitTaskChoice and (not WizardIsTaskSelected('autostart')) and OurRunValue then
    RegDeleteValue(HKCU, RunKey, '{#RunValueName}');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  { The companion of the folder being installed into: ask it to quit (a no-op when nothing runs). Setup's Restart Manager
    step comes next and is the fallback. }
  StopCompanion(ExpandConstant('{app}\{#AppExeName}'));
end;

{ ---- uninstall ---------------------------------------------------------------------------------------------------------- }

function InitializeUninstall: Boolean;
begin
  Result := True;
  StopCompanion(ExpandConstant('{app}\{#AppExeName}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: string;
  DeleteData: Boolean;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Remove the auto-start entry if it points at this installation, also when it was switched on from the tray menu later
      (an entry that belongs to something else, e.g. a development copy, is left alone). }
    if OurRunValue then
      RegDeleteValue(HKCU, RunKey, '{#RunValueName}');
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{#DataDirExpr}');
    if DirExists(DataDir) then
    begin
      if UninstallSilent then
        DeleteData := HasSwitch('/DELETEDATA')
      else
        DeleteData := MsgBox('Do you also want to delete your DeskMedia Companion settings and the pairing with your display?' + #13#10 + #13#10 +
                             'Choose No to keep them: they are picked up again if you reinstall.',
                             mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
      if DeleteData then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
