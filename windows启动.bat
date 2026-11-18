@echo off
chcp 65001 >nul
rem 切到 bat 所在目录，从别的地方启动也能找到脚本
cd /d "%~dp0"
title 自动看广告脚本

:menu
echo.
echo ==============================
echo   1. 汽水音乐  看广告领 VIP
echo   2. 抖音商城  看广告视频
echo   3. 抖音商城  天天拆红包
echo ==============================
set "choice="
set "script="
set /p choice=请输入 1、2 或 3，然后回车: 
if "%choice%"=="1" set "script=qishui_script.py"
if "%choice%"=="2" set "script=douyin_store\script_activity.py"
if "%choice%"=="3" set "script=douyin_store\script_red.py"
if not defined script (
    echo 输入有误，请输入 1、2 或 3
    goto menu
)

echo 启动 %script% ...
python %script%
echo.
echo 脚本已结束，按任意键回到菜单
pause >nul
goto menu
