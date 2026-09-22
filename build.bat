@echo off
REM 兼容入口：实际打包逻辑在 build_win.bat
cd /d "%~dp0"
call "%~dp0build_win.bat"
exit /b %ERRORLEVEL%
