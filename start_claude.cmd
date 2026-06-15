@echo off
chcp 65001 >nul
title Claude Auto-Launcher

echo ============================================
echo   Claude Auto-Launcher
echo ============================================

cd /d "%~dp0"

REM ===== Resolve which Python to run launch.py with =====
REM This is the only job the .cmd must do itself: you need a Python to start launch.py.
REM Everything else (config / model_config existence, required fields) is left to launch.py.
REM This file is intentionally pure ASCII: cmd.exe parses .cmd in the system OEM codepage,
REM so any non-ASCII text here would be mis-decoded and corrupt the script on some machines.
REM Tolerant parse: any indentation, optional quotes, trailing # comment, forward/back slashes.
set "PYTHON_PATH=python"
for /f "tokens=1,* delims=:" %%a in ('findstr /r /c:"^ *python *:" "config\config.yaml" 2^>nul') do call :set_python %%b
set "PYTHON_PATH=%PYTHON_PATH:/=\%"
goto :python_resolved

:set_python
REM %~1 strips surrounding quotes and leading spaces; an inline # comment falls into later args
if not "%~1"=="" set "PYTHON_PATH=%~1"
goto :eof

:python_resolved

REM ===== Only validate that Python itself runs (config contents are checked by launch.py) =====
"%PYTHON_PATH%" --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [!!] Python not available: %PYTHON_PATH%
    echo     Check paths.python in config\config.yaml ^(absolute path, quotes recommended^)
    echo.
    pause
    exit /b 1
)

echo.
echo [..] Python: %PYTHON_PATH%
echo [..] Starting... press q in this window to quit cleanly ^(Ctrl+C also works^)
echo.

REM ===== Start launch.py (missing config / bad fields are reported by launch.py itself) =====
"%PYTHON_PATH%" launch.py
set "LAUNCH_EXIT=%errorlevel%"

if "%LAUNCH_EXIT%"=="0" (
    echo.
    echo [OK] All components exited normally. See logs\ directory.
) else (
    echo.
    echo [!!] Exited with code %LAUNCH_EXIT%. See logs\ directory.
)
pause
