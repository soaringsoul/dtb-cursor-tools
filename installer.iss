; cursor账号管理器 安装包脚本（Inno Setup 6）
; 版本与源 exe 名由 build.bat 通过 /DAppVer /DExeName 传入；单独跑 ISCC 时用下面默认值。
#ifndef AppVer
#define AppVer "1.2.1"
#endif
#ifndef ExeName
#define ExeName "SandClaimer-" + AppVer + ".exe"
#endif

[Setup]
AppName=cursor账号管理器
AppVersion={#AppVer}
AppPublisher=夜雨微寒
DefaultDirName={autopf}\SandClaimer
DefaultGroupName=cursor账号管理器
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=SandClaimer-Setup-{#AppVer}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\SandClaimer.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Languages]
Name: "cn"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; 分发的 onefile 名带版本号，但装到本机统一叫 SandClaimer.exe，快捷方式跨版本不失效。
Source: "nuitka-out\{#ExeName}"; DestDir: "{app}"; DestName: "SandClaimer.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\cursor账号管理器"; Filename: "{app}\SandClaimer.exe"
Name: "{group}\{cm:UninstallProgram,cursor账号管理器}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\cursor账号管理器"; Filename: "{app}\SandClaimer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SandClaimer.exe"; Description: "{cm:LaunchProgram,cursor账号管理器}"; Flags: nowait postinstall skipifsilent
