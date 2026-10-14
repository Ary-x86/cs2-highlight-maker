; Windows installer for the packaged app. Built by the release workflow:
;   iscc /DAppVersion=1.2.3 packaging\windows-installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E1B7C55-2C1F-4C38-9E52-3D0F4B0C2A71}
AppName=CS2 Highlight Maker
AppVersion={#AppVersion}
AppPublisher=CS2 Highlight Maker
DefaultDirName={localappdata}\Programs\CS2 Highlight Maker
DefaultGroupName=CS2 Highlight Maker
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=CS2HighlightMaker-Setup
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\CS2HighlightMaker.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\CS2HighlightMaker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\CS2 Highlight Maker"; Filename: "{app}\CS2HighlightMaker.exe"
Name: "{userdesktop}\CS2 Highlight Maker"; Filename: "{app}\CS2HighlightMaker.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\CS2HighlightMaker.exe"; Description: "Start CS2 Highlight Maker"; Flags: nowait postinstall skipifsilent
