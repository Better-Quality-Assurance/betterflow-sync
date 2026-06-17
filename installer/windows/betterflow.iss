; Inno Setup script for BetterFlow Sync (Windows).
;
; Produces a per-user installer (no admin/UAC) that:
;   - installs the PyInstaller one-dir BetterFlow build to %LOCALAPPDATA%\Programs\BetterFlow,
;   - registers an entry in "Installed apps" with a working uninstaller
;     (the missing-from-Installed-apps fix — a bare zipped .exe never does this),
;   - creates a Start Menu shortcut (and an optional desktop shortcut),
;   - offers to launch the app at the end.
;
; The version is injected by CI:
;   ISCC.exe /DMyAppVersion=1.5.x installer\windows\betterflow.iss
; Run from the repo root so the relative Source/SetupIconFile paths resolve.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "BetterFlow"
#define MyAppPublisher "Better Quality Assurance SRL"
#define MyAppURL "https://betterqa.co"
#define MyAppExeName "BetterFlow.exe"

[Setup]
; Stable AppId so future versions upgrade in place instead of stacking up.
AppId={{8F3B2C7A-1E4D-4B9A-9C2E-BF10D5E28A44}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Per-user install — no admin prompt, matches a user-level tray agent.
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=BetterFlow-Windows-Setup
SetupIconFile=resources\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
; One-dir build: ship the whole dist\BetterFlow folder (exe + _internal\...).
Source: "dist\BetterFlow\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The exe is removed automatically; user data/config under %APPDATA% is left
; intact deliberately (so reinstalling keeps the user's settings/login).
