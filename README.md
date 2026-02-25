# BetterFlow Sync

A lightweight companion app that syncs ActivityWatch data to BetterFlow for automatic time tracking.

## Overview

BetterFlow Sync reads activity data from your local [ActivityWatch](https://activitywatch.net/) installation and securely syncs it to your BetterFlow account. This enables automatic timesheet population based on your actual computer usage.

## Features

- **Automatic syncing** - Events sync every 60 seconds (configurable)
- **Privacy-first** - Window titles are hashed by default, only domains sent for URLs
- **Offline support** - Events are queued locally when offline and synced when back online
- **System tray** - Minimal footprint with status indicator
- **Cross-platform** - Works on macOS and Windows

## Requirements

- [ActivityWatch](https://activitywatch.net/) installed and running
- BetterFlow account (https://betterflow.eu)
- Python 3.10+ (for development)

## Installation

### Pre-built Binaries

Download the latest release for your platform:
- **macOS**: `BetterFlow Sync.dmg`
- **Windows**: `BetterFlow Sync.exe`

### From Source

```bash
# Clone the repository
git clone https://github.com/BetterQA/betterflow.git
cd betterflow/agent

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run
python -m src.main
```

## Usage

1. **Install ActivityWatch** if you haven't already (https://activitywatch.net/)
2. **Start ActivityWatch** - ensure aw-server and aw-watcher-window are running
3. **Launch BetterFlow Sync**
4. **Sign in** with your BetterFlow credentials
5. **Done!** - Activity data will automatically sync to BetterFlow

### System Tray

The app runs in your system tray with a colored indicator:
- 🟢 **Green** - Connected and syncing
- 🟡 **Yellow** - Offline, events queued
- 🔴 **Red** - Error (auth failed or ActivityWatch not running)
- ⚪ **Gray** - Paused

Right-click the tray icon for options:
- Pause/Resume tracking
- Open preferences
- View dashboard
- Sign out
- Quit

## Privacy

BetterFlow Sync is designed with privacy in mind:

- **Window titles** are hashed (SHA-256) by default - only a fingerprint is sent, not the actual title
- **URLs** are stripped to domain-only - no full paths or query parameters
- **Allowlist** for raw titles - IDEs and terminals can show real titles for project tracking
- **Exclude apps** - Sensitive apps (1Password, etc.) are never tracked
- **No keylogging** - We never capture what you type
- **No screenshots** - We never capture your screen

You can customize these settings in Preferences.

## Development

### Setup

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run tests
make test

# Run linter
make lint

# Run in development mode
make run
```

### Building

#### macOS

```bash
# Build app bundle
make build-mac
# Creates: dist/BetterFlow Sync.app

# Create DMG installer (requires create-dmg)
make dmg
```

#### Windows

**Option 1: Run the build script**
```powershell
# PowerShell (recommended)
powershell -ExecutionPolicy Bypass -File build-windows.ps1

# Or Command Prompt
build-windows.bat
```

**Option 2: Manual build**
```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
python scripts\generate_icons.py
pyinstaller build.spec --clean
```

Creates: `dist\BetterFlow Sync.exe`

**Option 3: Create installer**

1. Install [Inno Setup](https://jrsoftware.org/isinfo.php)
2. Build the exe first (Option 1 or 2)
3. Compile the installer:
```cmd
iscc installer\windows-installer.iss
```

Creates: `dist\BetterFlow-Sync-Setup-1.0.0.exe`

#### GitHub Actions (CI/CD)

Push to `main` branch or create a tag to trigger automatic builds:
- Artifacts available in GitHub Actions
- Tagged releases (`v1.0.0`) create draft GitHub releases

### Project Structure

```
agent/
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration management
│   ├── sync/
│   │   ├── aw_client.py     # ActivityWatch API client
│   │   ├── bf_client.py     # BetterFlow API client
│   │   ├── sync_engine.py   # Core sync logic
│   │   └── queue.py         # Offline queue (SQLite)
│   ├── auth/
│   │   ├── keychain.py      # Secure credential storage
│   │   └── login.py         # Login flow
│   └── ui/
│       ├── tray.py          # System tray icon
│       └── preferences.py   # Settings window
├── resources/
│   ├── icon.png             # App icon
│   ├── icon.icns            # macOS icon
│   └── icon.ico             # Windows icon
├── tests/
├── pyproject.toml
├── requirements.txt
├── requirements-dev.txt
├── Makefile
└── build.spec               # PyInstaller config
```

## Configuration

Configuration is stored in:
- **macOS**: `~/Library/Application Support/BetterFlow Sync/config.json`
- **Windows**: `%APPDATA%\BetterQA\BetterFlow Sync\config.json`

Credentials are stored securely in your system keychain.

### Environment Overrides (`.env`)

For development, you can define endpoint overrides in a local `.env` file (see `.env.example`):

- `BETTERFLOW_API_URL` - Agent sync API endpoint
- `BETTERFLOW_WEB_BASE_URL` - Web app base used for browser auth and dashboard links

Example split setup:
- `BETTERFLOW_API_URL=http://127.0.0.1:8001/api/agent`
- `BETTERFLOW_WEB_BASE_URL=https://app.betterflow.eu`

## Troubleshooting

### ActivityWatch not detected

Ensure ActivityWatch is running:
1. Check that aw-server is running (port 5600)
2. Visit http://localhost:5600 in your browser
3. Make sure aw-watcher-window is running

### Sync not working

1. Check the tray icon color for status
2. View logs at:
   - macOS: `~/Library/Logs/BetterFlow Sync/betterflow-sync.log`
   - Windows: `%APPDATA%\BetterQA\BetterFlow Sync\Logs\betterflow-sync.log`

### Login issues

1. Verify your BetterFlow credentials at https://betterflow.eu
2. Check your internet connection
3. Try signing out and signing back in

## License

Proprietary - BetterQA

## Support

- Documentation: https://betterflow.eu/docs/agent
- Email: support@betterqa.co
- Website: https://betterflow.eu
