@echo off
chcp 65001 >nul
REM Demo, LIVE part: an empty library. Drag the "samples" folder in during the demo.
cd /d "%~dp0.."
if not exist demo\data-live mkdir demo\data-live
echo.
echo  Starting the demo (empty library) on port 8765...
echo  Leave this window open. Close it, or use Quit in the page, when you are done.
echo.
.venv\Scripts\python.exe -m pdfa11y web --data-dir demo\data-live --port 8765
pause
