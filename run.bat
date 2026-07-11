@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 当前目录：
cd

echo.
echo 检查虚拟环境...
if not exist ".venv\Scripts\python.exe" (
    echo 没找到 .venv\Scripts\python.exe
    echo 请先双击 setup_env.bat 安装环境
    pause
    exit /b
)

echo.
echo 检查 main.py...
if not exist "main.py" (
    echo 没找到 main.py
    echo 请确认 run.bat 和 main.py 在同一个项目文件夹里
    pause
    exit /b
)

echo.
echo 启动程序...
".venv\Scripts\python.exe" main.py

echo.
echo 程序已退出，退出码：%errorlevel%
pause
