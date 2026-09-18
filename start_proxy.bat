@echo off
powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "%~dp0scripts\start-proxy.ps1"
exit /b %errorlevel%
