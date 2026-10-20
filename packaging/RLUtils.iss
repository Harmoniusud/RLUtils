; Inno Setup script for the RLUtils installer (https://jrsoftware.org/isinfo.php, version 6).
; Built by packaging\release.ps1 (after build.ps1), which runs:  ISCC /DAppVersion=<version> packaging\RLUtils.iss
; Output: dist\RLUtils-Setup-<version>.exe
;
; Per-user install (no admin): %LOCALAPPDATA%\Programs\RLUtils, Start menu entry, optional desktop icon, and an
; entry in Windows' installed apps to uninstall it. The app keeps its data in %LOCALAPPDATA%\RLUtils (no
; data_dir.txt is written, unlike install.ps1, which is for development); uninstalling leaves that folder, so
; a reinstall keeps your history. A running RLUtils is asked to quit first (RLUtils.exe --quit), so vibrance
; is put back before files are replaced.

#define AppName "RLUtils"
#ifndef AppVersion
  #error Pass the version: ISCC /DAppVersion=0.11.0 packaging\RLUtils.iss (release.ps1 does)
#endif
#define AppExe "RLUtils.exe"

[Setup]
AppId={{6E7C2B1A-5D84-4F0B-9C3E-2A71B9D4E8F6}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=RLUtils-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
LicenseFile=..\LICENSE.txt
InfoBeforeFile=README-user.txt

[Tasks]
Name: "desktopicon"; Description: "Create a desktop icon"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\RLUtils\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "README-user.txt"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion

[InstallDelete]
; a development install (install.ps1) pointed the app at a project folder; an installed copy uses its own data
Type: files; Name: "{app}\data_dir.txt"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitApp"

[UninstallDelete]
; anything left in the program folder (the data folder, %LOCALAPPDATA%\RLUtils, is kept)
Type: filesandordirs; Name: "{app}"

[Registry]
; the start-with-Windows entry the app adds when that setting is on: removed with the app
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "RLUtils"; Flags: uninsdeletevalue dontcreatekey

[Code]
procedure QuitRunningApp();
var
  Code: Integer;
  Exe: String;
begin
  Exe := ExpandConstant('{app}\{#AppExe}');
  if FileExists(Exe) then
    Exec(Exe, '--quit', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitRunningApp();
  Result := '';
end;
