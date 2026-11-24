@echo off
chcp 65001 >nul
if /I "%~1"=="--no-pause" goto run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-plugin.ps1"
pause
exit /b
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-plugin.ps1"
