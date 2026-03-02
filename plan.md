# BetterFlow Sync — Install & Run Guide

## Prerequisites

- **Python 3.10+**
- **ActivityWatch** installed and running — download from https://activitywatch.net/
- **BetterFlow account** — sign up at https://betterflow.eu

## 1. Clone the Repository

```bash
git clone https://github.com/Better-Quality-Assurance/betterflow-sync.git
cd betterflow-sync
```

## 2. Create a Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate        # macOS / Linux
# venv\Scripts\activate          # Windows
```

## 3. Install Dependencies

```bash
# Production only
pip install -r requirements.txt

# Development (includes tests, linter, PyInstaller)
pip install -r requirements-dev.txt
```

## 4. (Optional) Configure Environment Overrides

Copy the example file and edit as needed:

```bash
cp .env.example .env
```

Available overrides:

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

## 5. Run the App

```bash
# Standard run
python3 -m src.main

# Or via Makefile
make run
```

On first launch a **setup wizard** will appear. After that the app runs in your **system tray** — there is no main window.

## 6. Sign In

1. The app opens your browser to the BetterFlow login page.
2. Authorize the device.
3. The tray icon turns green once syncing starts.

## Tray Icon Status Colors

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

## Running Tests

```bash
make test
# or directly
pytest tests/ -v --cov=src --cov-report=term-missing
```

## Linting & Formatting

```bash
make lint       # check with ruff
make format     # auto-format with ruff
```

## Building for Distribution

### macOS

```bash
make build-mac          # creates dist/BetterFlow Sync.app
make dmg                # creates dist/BetterFlow Sync.dmg (requires create-dmg)
```

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File build-windows.ps1
# or manually:
pyinstaller build.spec --clean
```

## Configuration Files

| File | Location (macOS) | Location (Windows) |
|---|---|---|
| Config | `~/Library/Application Support/BetterFlow Sync/config.json` | `%APPDATA%\BetterQA\BetterFlow Sync\config.json` |
| Logs | `~/Library/Logs/BetterFlow Sync/betterflow-sync.log` | `%APPDATA%\BetterQA\BetterFlow Sync\Logs\betterflow-sync.log` |
| Queue DB | `~/Library/Application Support/BetterFlow Sync/offline_queue.db` | `%APPDATA%\BetterQA\BetterFlow Sync\offline_queue.db` |
| Credentials | System keychain (Keychain Access) | Windows Credential Manager |

## Troubleshooting

### ActivityWatch not detected

1. Make sure `aw-server` is running on port 5600.
2. Visit http://localhost:5600 in your browser to verify.
3. Ensure `aw-watcher-window` is running.
4. On macOS, grant **Accessibility** permission to ActivityWatch in System Settings > Privacy & Security.

### Sync not working

1. Check the tray icon color for a quick status indicator.
2. Open the log file (see table above) for detailed errors.
3. Use **Quick Menu > Sync Now** in the tray to force a sync.

### Login issues

1. Verify your credentials at https://betterflow.eu.
2. Check your internet connection.
3. Try **Log Out** then sign back in from the tray menu.
