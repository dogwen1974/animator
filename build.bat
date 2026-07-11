@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [build] .venv was not found. Run setup_env.bat first.
    exit /b 1
)

if not exist "outputs" mkdir "outputs"
if not exist "work" mkdir "work"

echo [build] Building executable into outputs\PngFrameViewer ...
".venv\Scripts\python.exe" -m PyInstaller ^
    --noconfirm ^
    --windowed ^
    --name PngFrameViewer ^
    --distpath outputs ^
    --workpath work\pyinstaller_build ^
    --specpath work ^
    main.py

if errorlevel 1 exit /b 1

echo [build] Done: outputs\PngFrameViewer\PngFrameViewer.exe
endlocal
