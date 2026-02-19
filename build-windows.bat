@echo off
REM BetterFlow Sync - Windows Build Script
REM Run this on a Windows machine to build the executable

echo === BetterFlow Sync Windows Build ===
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

REM Create virtual environment if it doesn't exist
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate.bat

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt
pip install pyinstaller

REM Generate icons
echo Generating icons...
python scripts\generate_icons.py

REM Download ActivityWatch binaries
echo Downloading ActivityWatch binaries...
python scripts\download_aw.py

REM Build with PyInstaller
echo Building executable...
pyinstaller build.spec --clean

REM Check result
if exist "dist\BetterFlow Sync.exe" (
    echo.
    echo === BUILD SUCCESSFUL ===
    echo Executable created: dist\BetterFlow Sync.exe
    echo.
    echo To run: dist\BetterFlow Sync.exe
) else (
    echo.
    echo === BUILD FAILED ===
    echo Check the output above for errors.
)

pause
