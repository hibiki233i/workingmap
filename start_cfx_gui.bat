@echo off
setlocal

cd /d "%~dp0"
title CFX GUI Launcher

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0cfx_gui.py"
    goto :end
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0cfx_gui.py"
    goto :end
)

where python3 >nul 2>nul
if %errorlevel%==0 (
    python3 "%~dp0cfx_gui.py"
    goto :end
)

echo [ERROR] No Python launcher was found.
echo Please install Python 3 and make sure py or python is available in PATH.
pause

:end
endlocal
