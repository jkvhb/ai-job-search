@echo off
cd /d "%~dp0"
title AI Job Search Tool
where python >nul 2>nul
if errorlevel 1 goto nopython
echo ============================================
echo   AI Job Search Tool
echo ============================================
echo   URL  : http://127.0.0.1:8000
echo   Usage: keep this window open while using
echo   Stop : press Ctrl+C
echo ============================================
echo.
python app.py
echo.
echo [i] Server stopped. Press any key to close this window.
pause >nul
exit /b 0

:nopython
echo [ERROR] Python not found in PATH.
echo         Please install Python 3 and enable "Add Python to PATH".
echo.
pause
exit /b 1
