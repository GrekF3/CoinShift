@echo off
setlocal

cd /d "%~dp0"

python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Failed to install requirements.
    pause
    exit /b 1
)

python main.py
if errorlevel 1 (
    echo.
    echo Application exited with an error.
    pause
    exit /b 1
)

endlocal
