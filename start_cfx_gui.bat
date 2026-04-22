@echo off
setlocal

cd /d "%~dp0"
title CFX 扫描启动器 Version 1.0

set "GUI_SCRIPT=%~dp0cfx_gui.py"
set "PYTHON_CMD="
set "PYTHON_ARGS="
set "PYTHON_IS_PATH_CMD="

call :find_python
if not defined PYTHON_CMD goto :python_not_found

echo [INFO] Using Python: %PYTHON_CMD% %PYTHON_ARGS%
if /I "%PYTHON_IS_PATH_CMD%"=="1" (
    if defined PYTHON_ARGS (
        %PYTHON_CMD% %PYTHON_ARGS% "%GUI_SCRIPT%"
    ) else (
        %PYTHON_CMD% "%GUI_SCRIPT%"
    )
) else (
    if defined PYTHON_ARGS (
        "%PYTHON_CMD%" %PYTHON_ARGS% "%GUI_SCRIPT%"
    ) else (
        "%PYTHON_CMD%" "%GUI_SCRIPT%"
    )
)

if errorlevel 1 (
    echo.
    echo [ERROR] Python started but the GUI exited with an error.
    echo [ERROR] Command: "%PYTHON_CMD%" %PYTHON_ARGS% "%GUI_SCRIPT%"
    pause
)
goto :end

:find_python
call :try_path_command py -3
if defined PYTHON_CMD goto :eof

call :try_path_command python
if defined PYTHON_CMD goto :eof

call :try_path_command python3
if defined PYTHON_CMD goto :eof

call :probe_python_dir "%LocalAppData%\Programs\Python"
if defined PYTHON_CMD goto :eof

call :probe_python_dir "%ProgramFiles%\Python"
if defined PYTHON_CMD goto :eof

call :probe_python_dir "%ProgramFiles(x86)%\Python"
if defined PYTHON_CMD goto :eof

call :probe_python_dir "%UserProfile%\AppData\Local\Programs\Python"
if defined PYTHON_CMD goto :eof

call :probe_registry "HKCU\Software\Python\PythonCore"
if defined PYTHON_CMD goto :eof

call :probe_registry "HKLM\Software\Python\PythonCore"
if defined PYTHON_CMD goto :eof

call :probe_registry "HKLM\Software\WOW6432Node\Python\PythonCore"
if defined PYTHON_CMD goto :eof

if exist "%LocalAppData%\Microsoft\WindowsApps\python.exe" (
    call :try_absolute_python "%LocalAppData%\Microsoft\WindowsApps\python.exe"
    if defined PYTHON_CMD goto :eof
)

goto :eof

:try_path_command
set "CANDIDATE_CMD=%~1"
set "CANDIDATE_ARGS=%~2"
if not defined CANDIDATE_CMD goto :eof

where %CANDIDATE_CMD% >nul 2>nul
if errorlevel 1 goto :eof

if defined CANDIDATE_ARGS (
    %CANDIDATE_CMD% %CANDIDATE_ARGS% -c "import sys" >nul 2>nul
) else (
    %CANDIDATE_CMD% -c "import sys" >nul 2>nul
)
if errorlevel 1 goto :eof

set "PYTHON_CMD=%CANDIDATE_CMD%"
set "PYTHON_ARGS=%CANDIDATE_ARGS%"
set "PYTHON_IS_PATH_CMD=1"
goto :eof

:probe_python_dir
set "SEARCH_ROOT=%~1"
if not defined SEARCH_ROOT goto :eof
if not exist "%SEARCH_ROOT%" goto :eof

for /d %%D in ("%SEARCH_ROOT%\Python*") do (
    if exist "%%~fD\python.exe" (
        call :try_absolute_python "%%~fD\python.exe"
        if defined PYTHON_CMD goto :eof
    )
)

goto :eof

:probe_registry
set "REG_ROOT=%~1"
if not defined REG_ROOT goto :eof

for /f "tokens=*" %%K in ('reg query "%REG_ROOT%" /s /f python.exe 2^>nul ^| findstr /i "ExecutablePath"') do (
    for /f "tokens=2,*" %%A in ('reg query "%%K" /ve 2^>nul ^| findstr /r /c:"REG_SZ"') do (
        call :try_absolute_python "%%B"
        if defined PYTHON_CMD goto :eof
    )
)

for /f "tokens=*" %%K in ('reg query "%REG_ROOT%" /s /f InstallPath 2^>nul ^| findstr /i "InstallPath"') do (
    for /f "tokens=2,*" %%A in ('reg query "%%K" /ve 2^>nul ^| findstr /r /c:"REG_SZ"') do (
        call :try_absolute_python "%%Bpython.exe"
        if defined PYTHON_CMD goto :eof
    )
)

goto :eof

:try_absolute_python
set "ABS_PY=%~1"
if not defined ABS_PY goto :eof
if not exist "%ABS_PY%" goto :eof

"%ABS_PY%" -c "import sys" >nul 2>nul
if errorlevel 1 goto :eof

set "PYTHON_CMD=%ABS_PY%"
set "PYTHON_ARGS="
set "PYTHON_IS_PATH_CMD="
goto :eof

:python_not_found
echo [ERROR] No usable Python 3 launcher was found.
echo [ERROR] Checked commands: py, python, python3
echo [ERROR] Checked folders:
echo         %LocalAppData%\Programs\Python
echo         %ProgramFiles%\Python
echo         %ProgramFiles(x86)%\Python
echo [ERROR] Checked registry:
echo         HKCU\Software\Python\PythonCore
echo         HKLM\Software\Python\PythonCore
echo         HKLM\Software\WOW6432Node\Python\PythonCore
echo.
echo Please install Python 3 and enable "Add python.exe to PATH".
echo If Python is already installed, reopen this terminal after installation.
pause

:end
endlocal
