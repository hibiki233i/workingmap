@echo off
setlocal

cd /d "%~dp0"
title CFX 扫描启动器 Version 1.0

set "GUI_SCRIPT=%cd%\cfx_gui.py"

echo Launching Python source app...
call :detect_python
if errorlevel 1 goto :no_python

echo [INFO] Using Python: %PYTHON_CMD%
%PYTHON_CMD% "%GUI_SCRIPT%"
if errorlevel 1 (
  echo.
  echo [ERROR] Python started but the GUI exited with an error.
  echo [ERROR] Command: %PYTHON_CMD% "%GUI_SCRIPT%"
  pause
)
exit /b %errorlevel%

:detect_python
set "PYTHON_CMD="

if exist "%cd%\.venv\Scripts\python.exe" (
  set "PYTHON_CMD=\"%cd%\.venv\Scripts\python.exe\""
  goto :eof
)

if exist "%cd%\venv\Scripts\python.exe" (
  set "PYTHON_CMD=\"%cd%\venv\Scripts\python.exe\""
  goto :eof
)

call :check_python_cmd py -3
if defined PYTHON_CMD goto :eof

call :check_python_cmd py
if defined PYTHON_CMD goto :eof

call :check_python_cmd python
if defined PYTHON_CMD goto :eof

call :check_python_cmd python3
if defined PYTHON_CMD goto :eof

call :check_app_path "HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\python.exe"
if defined PYTHON_CMD goto :eof

call :check_app_path "HKLM\Software\Microsoft\Windows\CurrentVersion\App Paths\python.exe"
if defined PYTHON_CMD goto :eof

for /d %%D in ("%LocalAppData%\Programs\Python\Python*") do (
  if exist "%%~fD\python.exe" (
    set "PYTHON_CMD=\"%%~fD\python.exe\""
    goto :eof
  )
)

for /d %%D in ("%ProgramFiles%\Python*") do (
  if exist "%%~fD\python.exe" (
    set "PYTHON_CMD=\"%%~fD\python.exe\""
    goto :eof
  )
)

for /d %%D in ("%ProgramFiles(x86)%\Python*") do (
  if exist "%%~fD\python.exe" (
    set "PYTHON_CMD=\"%%~fD\python.exe\""
    goto :eof
  )
)

exit /b 1

:check_python_cmd
%* --version >nul 2>nul
if not errorlevel 1 (
  set "PYTHON_CMD=%*"
)
exit /b 0

:check_app_path
for /f "skip=2 tokens=2,*" %%A in ('reg query %1 /ve 2^>nul') do (
  if /i "%%A"=="REG_SZ" (
    if exist "%%B" (
      set "PYTHON_CMD=\"%%B\""
      goto :eof
    )
  )
)
exit /b 0

:no_python
echo [ERROR] No Python launcher was found.
echo [ERROR] Install Python or add it to PATH.
echo [ERROR] Checked: .venv, venv, py -3, py, python, python3, registry app paths, and common install directories.
pause
exit /b 1
