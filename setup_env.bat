@echo off
setlocal
cd /d "%~dp0"

set "PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple"
set "PIP_OPTIONS=-i %PIP_INDEX% --timeout 600 --retries 10"
set "SETUP_ERROR=0"

echo Current directory:
cd

echo.
echo Checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    echo .venv was not found. Creating virtual environment...

    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 (
            python -m venv .venv
        ) else (
            echo Python was not found. Please install Python 3.10 or newer.
            set "SETUP_ERROR=1"
            goto done
        )
    )
)

if not exist ".venv\Scripts\activate.bat" (
    echo Failed to create virtual environment: .venv\Scripts\activate.bat was not found.
    set "SETUP_ERROR=1"
    goto done
)

echo.
echo Activating virtual environment...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate virtual environment.
    set "SETUP_ERROR=1"
    goto done
)

echo.
echo Upgrading pip, setuptools, and wheel from Tsinghua mirror...
python -m pip install --upgrade pip setuptools wheel %PIP_OPTIONS%
if errorlevel 1 (
    echo Failed to upgrade pip, setuptools, and wheel.
    set "SETUP_ERROR=1"
    goto done
)

echo.
echo Installing PySide6 from Tsinghua mirror...
python -m pip install PySide6 %PIP_OPTIONS%
if errorlevel 1 (
    echo Failed to install PySide6.
    set "SETUP_ERROR=1"
    goto done
)

echo.
echo Installing Pillow from Tsinghua mirror...
python -m pip install Pillow %PIP_OPTIONS%
if errorlevel 1 (
    echo Failed to install Pillow.
    set "SETUP_ERROR=1"
    goto done
)

echo.
echo Installing numpy from Tsinghua mirror...
python -m pip install numpy %PIP_OPTIONS%
if errorlevel 1 (
    echo Failed to install numpy.
    set "SETUP_ERROR=1"
    goto done
)

echo.
echo Installing pyinstaller from Tsinghua mirror...
python -m pip install pyinstaller %PIP_OPTIONS%
if errorlevel 1 (
    echo Failed to install pyinstaller.
    set "SETUP_ERROR=1"
    goto done
)

echo.
echo Setup completed successfully.

:done
echo.
if "%SETUP_ERROR%"=="1" (
    echo Setup failed. Please check the messages above.
) else (
    echo You can now run run.bat.
)
pause
endlocal
exit /b %SETUP_ERROR%
