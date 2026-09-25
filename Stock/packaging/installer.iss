#define AppVersion "1.9.1"
#define AppName "Stock Downloader"

[Setup]
AppId={{D5999937-BCB4-4574-883C-08DA0938D788}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
VersionInfoVersion=1.9.1.0
DefaultDirName={localappdata}\Programs\Stock Downloader
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=Stock-Downloader-Setup-v{#AppVersion}-x64
SetupIconFile=..\assets\stock-check.ico
UninstallDisplayIcon={app}\StockDownloader.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=yes
CloseApplicationsFilter=StockDownloader.exe,flet.exe
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\StockDownloader\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\StockDownloader.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\StockDownloader.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\StockDownloader.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; User data is outside {app}. Uninstall/upgrade must never delete it.
