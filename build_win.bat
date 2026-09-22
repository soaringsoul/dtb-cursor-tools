@echo off
REM cursor账号管理器 · Windows Nuitka 打包（onefile exe + 可选安装包）
REM 用法：build_win.bat
REM 产物：nuitka-out\SandClaimer-<版本>.exe
REM       若已装 Inno Setup 6，另出 installer\SandClaimer-Setup-<版本>.exe
REM 版本号取自 sand_patch.TOOL_VERSION。只能在 Windows 上跑。
setlocal
cd /d "%~dp0"

echo ===================================================
echo    Nuitka 打包 cursor账号管理器 → .exe
echo ===================================================

echo [0/5] 读取版本号 (sand_patch.TOOL_VERSION) ...
for /f "delims=" %%v in ('python -c "import sand_patch;print(sand_patch.TOOL_VERSION)"') do set "VER=%%v"
if "%VER%"=="" (
  echo [X] 读不到 TOOL_VERSION（确认已安装 Python，且 sand_patch.py 在同目录）
  exit /b 1
)
echo     VER=%VER%
set "EXENAME=SandClaimer-%VER%.exe"

echo [1/5] 安装依赖 ...
python -m pip install -r requirements.txt || exit /b 1

echo [2/5] 修补 Nuitka pywebview 插件（补 win32） ...
python patch_plugin.py || exit /b 1

echo [3/5] 生成 icon.ico ...
python make_icon.py || exit /b 1

echo [4/5] Nuitka 编译 onefile（较慢，请耐心等待）...
python -m nuitka --standalone --onefile --assume-yes-for-downloads --mingw64 --experimental=force-dependencies-pefile --windows-console-mode=disable --windows-icon-from-ico=icon.ico --company-name="夜雨微寒" --product-name="cursor账号管理器" --product-version=%VER% --file-version=%VER%.0 --include-data-dir=web=web --output-filename=%EXENAME% --output-dir=nuitka-out app.py || exit /b 1

echo [5/5] Inno Setup 打安装包 ...
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo     SKIP: 未安装 Inno Setup 6，安装包未生成。绿色版 exe 已可用。
  echo     安装包下载：https://jrsoftware.org/isdl.php
  echo     装好后再跑本脚本可得 installer\SandClaimer-Setup-%VER%.exe
  echo.
  echo 完成:
  echo   EXE:       nuitka-out\%EXENAME%
  exit /b 0
)
"%ISCC%" /DAppVer=%VER% /DExeName=%EXENAME% installer.iss || exit /b 1

echo.
echo 完成:
echo   EXE:       nuitka-out\%EXENAME%
echo   Installer: installer\SandClaimer-Setup-%VER%.exe
exit /b 0
