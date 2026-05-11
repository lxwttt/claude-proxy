@echo off
chcp 65001 >nul
title Claude Auto-Launcher

echo ============================================
echo   Claude Auto-Launcher
echo ============================================

REM 切换到脚本自身所在目录（支持放在任意位置）
cd /d "%~dp0"

REM 检查 config.yaml 是否存在
if not exist "config.yaml" (
    echo.
    echo [!!] config.yaml 不存在！
    echo     请复制 sample_config.yaml 为 config.yaml 并修改配置
    echo.
    echo     copy sample_config.yaml config.yaml
    echo.
    pause
    exit /b 1
)

REM 检查 Python 是否可用
python --version >nul 2>&1
if errorlevel 1 (
    echo [!!] 错误：Python 未安装或未添加到 PATH
    echo     请安装 Python：https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 启动 launch.py
echo.
echo [..] 正在启动 Claude 自动启动器...
echo.
python launch.py

REM 如果程序异常退出，暂停以便查看错误信息
if errorlevel 1 (
    echo.
    echo [!!] 程序异常退出，请检查上方错误信息
    pause
)