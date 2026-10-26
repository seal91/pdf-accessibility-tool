@echo off
chcp 65001 >nul
REM Demo SAFETY NET: all publications already processed. Runs on port 8766 so it can run next to the live one.
cd /d "%~dp0.."
if not exist demo\data-ready (
  echo The prepared library does not exist yet. Run demo\reset-demo.bat first.
  pause
  exit /b 1
)
echo.
echo  Starting the prepared demo library on port 8766...
echo.
.venv\Scripts\python.exe -m pdfa11y web --data-dir demo\data-ready --port 8766
pause
