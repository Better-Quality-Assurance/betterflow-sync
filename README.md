# BetterFlow Sync

A lightweight companion app that syncs ActivityWatch data to BetterFlow for automatic time tracking.

## Overview

BetterFlow Sync reads activity data from your local [ActivityWatch](https://activitywatch.net/) installation and securely syncs it to your BetterFlow account. This enables automatic timesheet population based on your actual computer usage.

## Features

- **Automatic syncing** - Events sync every 60 seconds (configurable)
- **Privacy-first** - Window titles are hashed by default, only domains sent for URLs
- **Offline support** - Events are queued locally when offline and synced when back online
- **Activity analysis** - Engagement detection distinguishes active work from idle time
- **Break & private time reminders** - Configurable notifications to take breaks or end private mode
- **Project tracking** - Switch between projects from the tray menu
- **Auto-categorization** - Apps are automatically categorized using server-synced mappings
- **Multi-monitor awareness** - Tracks which display and virtual desktop you're working on
- **Auto-updates** - Checks for new releases with stable/beta/canary channels
- **System tray** - Minimal footprint with color-coded status indicator
- **Cross-platform** - Works on macOS and Windows

## Prerequisites

- **Python 3.10+**
- [ActivityWatch](https://activitywatch.net/) installed and running
- BetterFlow account — sign up at https://betterflow.eu

## Installation

### Pre-built Binaries

Download the latest release for your platform:
- **macOS**: `BetterFlow Sync.dmg`
- **Windows**: `BetterFlow Sync.exe`

### From Source

```bash
# Clone the repository
git clone https://github.com/Better-Quality-Assurance/betterflow-sync.git
cd betterflow-sync

# Create virtual environment
python3 -m venv venv
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate          # Windows

# Install dependencies
pip install -r requirements.txt

# Run
python3 -m src.main
```

## Usage

1. **Start ActivityWatch** - ensure `aw-server` and `aw-watcher-window` are running
2. **Launch BetterFlow Sync** - `python3 -m src.main` or `make run`
3. **Sign in** - the app opens your browser to the BetterFlow login page
4. **Done!** - the tray icon turns green once syncing starts

On first launch a **setup wizard** will appear. After that the app runs in your **system tray** — there is no main window.

### Tray Icon Status Colors

| Color | Meaning |
|---|---|
| Green | Connected and syncing |
| Yellow | Offline — events queued locally |
| Orange | Offline — queue nearing capacity |
| Red | Error (auth failure or ActivityWatch not running) |
| Gray | Paused |
| Dark gray | Private time — nothing recorded |
| Blue | Starting up |
| Amber | Waiting for browser login |

Right-click the tray icon for options including pause/resume, private time, project switching, preferences, diagnostics, and more.

## Privacy

BetterFlow Sync is designed with privacy in mind:

- **Window titles** are hashed (SHA-256) by default - only a fingerprint is sent, not the actual title
- **URLs** are stripped to domain-only - no full paths or query parameters
- **Allowlist** for raw titles - IDEs and terminals can show real titles for project tracking
- **Exclude apps** - Sensitive apps (1Password, etc.) are never tracked
- **No keylogging** - We never capture what you type
- **No screenshots** - We never capture your screen

You can customize these settings in Preferences.

## Configuration

### Config Files

| File | Location (macOS) | Location (Windows) |
|---|---|---|
| Config | `~/Library/Application Support/BetterFlow Sync/config.json` | `%APPDATA%\BetterQA\BetterFlow Sync\config.json` |
| Logs | `~/Library/Logs/BetterFlow Sync/betterflow-sync.log` | `%APPDATA%\BetterQA\BetterFlow Sync\Logs\betterflow-sync.log` |
| Queue DB | `~/Library/Application Support/BetterFlow Sync/offline_queue.db` | `%APPDATA%\BetterQA\BetterFlow Sync\offline_queue.db` |
| Credentials | System keychain (Keychain Access) | Windows Credential Manager |

### Environment Overrides (`.env`)

For development, copy the example and edit as needed:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `BETTERFLOW_API_URL` | Agent sync API endpoint | `https://app.betterflow.eu/api/agent` |
| `BETTERFLOW_WEB_BASE_URL` | Web app base URL (for auth & dashboard links) | derived from API URL |
| `BETTERFLOW_SYNC_ENV_FILE` | Explicit path to a `.env` file | — |

Example for local backend development:

```env
BETTERFLOW_API_URL=http://127.0.0.1:8001/api/agent
BETTERFLOW_WEB_BASE_URL=https://app.betterflow.eu
```

## Development

### Setup

```bash
# Install dev dependencies (includes pytest, ruff, pyinstaller)
pip install -r requirements-dev.txt

# Run tests
make test

# Run linter
make lint

# Auto-format code
make format

# Run in development mode
make run
```

### Running Tests

```bash
make test
# or directly
pytest tests/ -v --cov=src --cov-report=term-missing
```

### Building

#### macOS

```bash
make build-mac          # creates dist/BetterFlow Sync.app
make dmg                # creates dist/BetterFlow Sync.dmg (requires create-dmg)
```

#### Windows

**Option 1: Run the build script**
```powershell
powershell -ExecutionPolicy Bypass -File build-windows.ps1
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
src/
├── __init__.py
├── main.py                  # Entry point & app orchestrator
├── config.py                # Configuration management
├── autostart.py             # Launch-at-login (macOS/Windows)
├── notifications.py         # Native OS notifications
├── reminders.py             # Break & private time reminders
├── display_info.py          # Multi-monitor & virtual desktop tracking
├── update_checker.py        # GitHub Releases update checker
├── aw_manager.py            # ActivityWatch process manager
├── system_events.py         # Sleep/wake/network/lock listeners
├── sync/
│   ├── aw_client.py         # ActivityWatch API client
│   ├── bf_client.py         # BetterFlow API client
│   ├── sync_engine.py       # Core sync logic
│   ├── queue.py             # Offline queue (SQLite)
│   ├── http_client.py       # Base HTTP client with retry
│   ├── activity_analyzer.py # Engagement/fraud detection
│   ├── daily_time_tracker.py# Per-day active time tracking
│   └── protocols.py         # Protocol interfaces
├── auth/
│   ├── keychain.py          # Secure credential storage
│   ├── login.py             # Login flow
│   ├── pkce.py              # PKCE for OAuth
│   └── browser_auth.py      # Browser-based OAuth flow
└── ui/
    ├── tray.py              # System tray icon & menu
    ├── setup_wizard.py      # First-run setup wizard
    └── permissions.py       # macOS permission prompts
```

## Troubleshooting

### ActivityWatch not detected

1. Make sure `aw-server` is running on port 5600.
2. Visit http://localhost:5600 in your browser to verify.
3. Ensure `aw-watcher-window` is running.
4. On macOS, grant **Accessibility** permission to ActivityWatch in System Settings > Privacy & Security.

### Sync not working

1. Check the tray icon color for a quick status indicator.
2. Open the log file (see config table above) for detailed errors.
3. Use **Quick Menu > Sync Now** in the tray to force a sync.
4. Check **Diagnostics** submenu for AW/API/queue status.

### Login issues

1. Verify your credentials at https://betterflow.eu.
2. Check your internet connection.
3. Try **Log Out** then sign back in from the tray menu.

## License

Proprietary - BetterQA

## Support

- Documentation: https://betterflow.eu/docs/agent
- Email: support@betterqa.co
- Website: https://betterflow.eu
