@echo off
chcp 65001 >nul
title Claude Auto-Launcher

echo ============================================
echo   Claude Auto-Launcher
echo ============================================

cd /d "%~dp0"

REM ===== 1. 检查必需文件 =====
if not exist "config.yaml" (
    echo.
    echo [!!] config.yaml 不存在
    echo     请复制 sample_config.yaml 为 config.yaml 并修改配置
    echo.
    pause
    exit /b 1
)

if not exist "launch.py" (
    echo.
    echo [!!] launch.py 不存在
    pause
    exit /b 1
)

REM ===== 2. 从 config.yaml 提取 Python 路径 =====
set PYTHON_CFG=
for /f "tokens=2" %%i in ('findstr /b /c:"  python:" config.yaml') do set "PYTHON_CFG=%%i"

set PYTHON_PATH=python
if defined PYTHON_CFG set "PYTHON_PATH=%PYTHON_CFG:"=%"

REM ===== 3. 校验 Python 可执行 =====
set PYTHON_OK=0
if "%PYTHON_PATH%"=="python" (
    python --version >nul 2>&1 && set PYTHON_OK=1
) else (
    if exist "%PYTHON_PATH%" (
        "%PYTHON_PATH%" --version >nul 2>&1 && set PYTHON_OK=1
    )
)

if %PYTHON_OK%==0 (
    echo.
    echo [!!] Python 不可用: %PYTHON_PATH%
    echo     请检查 config.yaml 中的 python 路径配置
    pause
    exit /b 1
)

echo.
echo [..] Python: %PYTHON_PATH%
echo [..] 正在启动 Claude 自动启动器...
echo.

REM ===== 4. 启动 launch.py =====
%PYTHON_PATH% launch.py

if errorlevel 1 (
    echo.
    echo [!!] 程序异常退出，请检查上方信息或查看 logs\ 目录
    pause
    exit /b 1
)

echo.
echo [OK] 所有组件已成功启动
echo     日志: logs\ 目录
pause
