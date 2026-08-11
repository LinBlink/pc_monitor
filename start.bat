@echo off
rem Start the PC Monitor stream server with a visible console (shows the URL).
cd /d "%~dp0"
python server.py %*
if errorlevel 1 pause
