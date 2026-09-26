; cursorAdmin 安装包脚本（Inno Setup 6）
; 版本与源 exe 名由 build_win.bat 通过 /DAppVer /DExeName 传入；单独跑 ISCC 时用下面默认值。
#ifndef AppVer
#define AppVer "1.2.1"
#endif
#ifndef ExeName
#define ExeName "cursorAdmin-" + AppVer + ".exe"
#endif

[Setup]
AppName=cursorAdmin
AppVersion={#AppVer}
AppPublisher=夜雨微寒
DefaultDirName={autopf}\cursorAdmin
DefaultGroupName=cursorAdmin
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=cursorAdmin-Setup-{#AppVer}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\cursorAdmin.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Languages]
Name: "cn"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 分发的 onefile 名带版本号，但装到本机统一叫 cursorAdmin.exe，快捷方式跨版本不失效。
Source: "nuitka-out\{#ExeName}"; DestDir: "{app}"; DestName: "cursorAdmin.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\cursorAdmin"; Filename: "{app}\cursorAdmin.exe"
Name: "{group}\{cm:UninstallProgram,cursorAdmin}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\cursorAdmin"; Filename: "{app}\cursorAdmin.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\cursorAdmin.exe"; Description: "{cm:LaunchProgram,cursorAdmin}"; Flags: nowait postinstall skipifsilent
