#define MyAppName "DramaStudio"
#define MyAppVersion "1.0.0"
#define MyAppExeName "DramaStudio.exe"

[Setup]
AppId={{72C719C5-AC47-46F5-B7EE-4854626C024A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Toannt
DefaultDirName={localappdata}\Programs\DramaStudio
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=releases
OutputBaseFilename=DramaStudio-Setup-v{#MyAppVersion}-GPU
SetupIconFile=assets\dramastudio-icon-v2.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "build\installer-app\DramaStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Icons]
Name: "{group}\DramaStudio"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\DramaStudio"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch DramaStudio"; Flags: nowait postinstall skipifsilent
