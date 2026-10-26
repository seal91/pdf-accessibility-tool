@echo off
chcp 65001 >nul
REM Rebuild BOTH demo libraries from scratch (takes about a minute). Do this before every demo.
cd /d "%~dp0.."
.venv\Scripts\python.exe demo\prepare_demo.py reset
echo.
echo  Demo libraries are ready. Now run start-demo-live.bat and start-demo-ready.bat.
pause
