# BetterFlow Sync - Windows Build Script (PowerShell)
# Run: powershell -ExecutionPolicy Bypass -File build-windows.ps1

Write-Host "=== BetterFlow Sync Windows Build ===" -ForegroundColor Cyan
Write-Host ""

# Check Python
try {
    $pythonVersion = python --version 2>&1
    Write-Host "Found: $pythonVersion" -ForegroundColor Green
} catch {
    Write-Host "ERROR: Python is not installed or not in PATH" -ForegroundColor Red
    Write-Host "Please install Python 3.10+ from https://python.org"
    Read-Host "Press Enter to exit"
    exit 1
}

# Create virtual environment if it doesn't exist
if (-not (Test-Path "venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv venv
}

# Activate virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Yellow
& .\venv\Scripts\Activate.ps1

# Install dependencies
Write-Host "Installing dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt
pip install pyinstaller

# Generate icons
Write-Host "Generating icons..." -ForegroundColor Yellow
python scripts\generate_icons.py

# Download ActivityWatch binaries
Write-Host "Downloading ActivityWatch binaries..." -ForegroundColor Yellow
python scripts\download_aw.py

# Build with PyInstaller
Write-Host "Building executable..." -ForegroundColor Yellow
pyinstaller build.spec --clean

# Check result
if (Test-Path "dist\BetterFlow Sync.exe") {
    Write-Host ""
    Write-Host "=== BUILD SUCCESSFUL ===" -ForegroundColor Green
    Write-Host "Executable created: dist\BetterFlow Sync.exe"
    Write-Host ""

    # Get file info
    $exe = Get-Item "dist\BetterFlow Sync.exe"
    Write-Host "Size: $([math]::Round($exe.Length / 1MB, 2)) MB"
    Write-Host ""

    # Offer to run
    $run = Read-Host "Run the app now? (y/n)"
    if ($run -eq "y") {
        Start-Process "dist\BetterFlow Sync.exe"
    }
} else {
    Write-Host ""
    Write-Host "=== BUILD FAILED ===" -ForegroundColor Red
    Write-Host "Check the output above for errors."
}

Read-Host "Press Enter to exit"
