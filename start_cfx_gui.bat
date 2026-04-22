@echo off
setlocal

cd /d "%~dp0"
title CFX 扫描启动器 Version 1.0

set "GUI_SCRIPT=%~dp0cfx_gui.py"
set "PYTHON_CMD="
set "PYTHON_ARGS="

call :find_python
if not defined PYTHON_CMD goto :python_not_found

echo [INFO] Using Python: %PYTHON_CMD% %PYTHON_ARGS%
if defined PYTHON_ARGS (
    "%PYTHON_CMD%" %PYTHON_ARGS% "%GUI_SCRIPT%"
) else (
    "%PYTHON_CMD%" "%GUI_SCRIPT%"
)

if errorlevel 1 (
    echo.
    echo [ERROR] Python started but the GUI exited with an error.
    echo [ERROR] Command: "%PYTHON_CMD%" %PYTHON_ARGS% "%GUI_SCRIPT%"
    pause
)
goto :end

:find_python
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=py"
    set "PYTHON_ARGS=-3"
    goto :eof
)

where python >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=python"
    goto :eof
)

where python3 >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON_CMD=python3"
    goto :eof
)

call :probe_python_dir "%LocalAppData%\Local\Python"
if defined PYTHON_CMD goto :eof

call :probe_python_dir "%ProgramFiles%\Python"
if defined PYTHON_CMD goto :eof

call :probe_python_dir "%ProgramFiles(x86)%\Python"
if defined PYTHON_CMD goto :eof

goto :eof

:probe_python_dir
set "SEARCH_ROOT=%~1"
if not defined SEARCH_ROOT goto :eof
if not exist "%SEARCH_ROOT%" goto :eof

for /d %%D in ("%SEARCH_ROOT%\Python*") do (
    if exist "%%~fD\python.exe" (
        set "PYTHON_CMD=%%~fD\python.exe"
        set "PYTHON_ARGS="
        goto :eof
    )
)

goto :eof

:python_not_found
echo [ERROR] No usable Python 3 launcher was found.
echo [ERROR] Checked commands: py, python, python3
echo [ERROR] Checked folders:
echo         %LocalAppData%\Programs\Python
echo         %ProgramFiles%\Python
echo         %ProgramFiles(x86)%\Python
echo.
echo Please install Python 3 and enable "Add python.exe to PATH".
echo If Python is already installed, reopen this terminal after installation.
pause

:end
endlocal
