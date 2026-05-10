@echo off
chcp 65001 >nul
title Claude Auto-Launcher

echo ============================================
echo   Claude Auto-Launcher (Non-Admin)
echo ============================================

REM 切换到脚本所在目录
cd /d "D:\claude-proxy"

REM 检查 Python 是否可用
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 错误：Python 未安装或未添加到 PATH
    echo 请安装 Python 或更新 config.yaml 中的 python 路径
    pause
    exit /b 1
)

REM 启动 launch.py
echo 🚀 正在启动 Claude 自动启动器...
python launch.py

REM 如果程序异常退出，暂停以便查看错误信息
if errorlevel 1 (
    echo.
    echo ❌ 程序异常退出，请检查错误信息
    pause
)