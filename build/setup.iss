; Inno Setup安装脚本
; 进程监控助手安装程序配置

#define MyAppName "进程监控助手"
; 版本号优先由外部命令行传入 /DMyAppVersion=x.y.z（build.bat 已实现，读取
; config.APP_VERSION 单源）；此处 #ifndef 仅作直接用 Inno Setup IDE 打开
; 编译时的兜底，避免忘传参数时编译出错误版本号的安装包
#ifndef MyAppVersion
#define MyAppVersion "1.2.0"
#endif
#define MyAppPublisher "软件测试工程师"
#define MyAppExeName "进程监控助手.exe"

[Setup]
; 应用基本信息
AppId={{8F5A9C2D-6B3E-4F1A-9D2C-7E4B8A1F3C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppVerName={#MyAppName} {#MyAppVersion}

; 默认安装路径（用户目录，避免权限问题）
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

; 输出配置
OutputDir=..\dist\installer
OutputBaseFilename=进程监控助手_v{#MyAppVersion}_Setup
; 应用图标（在build目录下）
SetupIconFile=app_green_icon.ico

; 压缩配置
Compression=lzma2/max
SolidCompression=yes

; 界面配置
WizardStyle=modern
DisableProgramGroupPage=yes
DisableWelcomePage=no

; 权限配置（普通用户即可安装）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 语言
ShowLanguageDialog=no

; 许可协议（可选）
; LicenseFile=

; 其他配置
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
; 使用默认英文语言包（如果需要中文，需要单独下载安装中文语言包）
; Name: "english"; MessagesFile: "compiler:Default.isl"
; 使用已安装的中文语言包，可以取消下面的注释：
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
; onedir 打包产物（主exe + _internal依赖目录），整体复制到安装目录
; 必须保留 ignoreversion：onedir 内含大量 Qt/依赖 DLL，若走版本比较，
; 相同文件名但内容不同的 DLL 会被 Inno Setup 判定"无需覆盖"而跳过，
; 导致覆盖安装后新旧版本 DLL 混用
Source: "..\dist\进程监控助手\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; 图标文件（用于任务栏和窗口显示）- 从build目录复制到安装目录根
Source: "app_green_icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; 开始菜单快捷方式（明确指定图标）
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"

; 桌面快捷方式（可选，明确指定图标）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 安装完成后询问是否运行
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    { 安装完成后的处理 }
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;

  { 卸载前提示用户是否删除数据。静默卸载（/VERYSILENT等）下不弹这个自定义
    MsgBox：Inno Setup的/SUPPRESSMSGBOXES只抑制Setup自身内置提示，不会自动
    应答[Code]里手写的MsgBox，实测会导致无人值守卸载（脚本化/CI场景）永久
    挂起等待点击。静默场景下默认保留数据（更安全的默认行为，不悄悄删用户
    数据），交互场景行为不变。 }
  if not UninstallSilent() then
  begin
    if MsgBox('是否同时删除所有监控数据？' + #13#10 +
              '选择"是"将删除所有历史数据。' + #13#10 +
              '选择"否"将保留数据，以便将来重新安装后继续使用。',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      { 用户选择删除数据 }
      DelTree(ExpandConstant('{localappdata}\{#MyAppName}'), True, True, True);
    end;
  end;
end;
