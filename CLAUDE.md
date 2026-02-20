# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BetterFlow Sync is a Python desktop app that syncs ActivityWatch data to BetterFlow for automatic time tracking. It runs as a system tray application on macOS and Windows, polling ActivityWatch locally and sending events to the BetterFlow API.

## Commands

```bash
# Development
make run              # Run the app locally
make test             # Run tests with coverage
make lint             # Run ruff linter
make format           # Auto-format code with ruff

# Run a single test
pytest tests/test_sync_engine.py::TestSyncEngine::test_pause_resume -v

# Building
make build            # Build for current platform (requires pyinstaller)
make build-mac        # Build macOS .app bundle
make dmg              # Build macOS DMG installer (requires create-dmg)

# Windows build (run on Windows)
powershell -ExecutionPolicy Bypass -File build-windows.ps1
```

## Architecture

### Data Flow

```
ActivityWatch (localhost:5600)
        |
        v
    AWClient (src/sync/aw_client.py)
        |
        v
    SyncEngine (src/sync/sync_engine.py)
        |-- Privacy filtering (hash titles, strip URLs to domain)
        |-- Transform events to BetterFlow format
        v
    BetterFlowClient (src/sync/bf_client.py)
        |
        v (offline?)
    OfflineQueue (SQLite) ----> Retry when back online
```

### Core Components

**BetterFlowSyncApp** (`main.py`) - Main application orchestrator. Initializes all components, manages the APScheduler sync loop (default 60s), handles tray icon state transitions.

**SyncEngine** (`sync/sync_engine.py`) - Orchestrates AW -> BetterFlow data flow. Fetches events since last checkpoint, applies privacy transformations, batches and sends to API. On network failure, queues events to SQLite.

**OfflineQueue** (`sync/queue.py`) - SQLite-backed queue for offline resilience. Also stores per-bucket sync checkpoints (last synced timestamp/event ID). Thread-safe with per-thread connections.

**BrowserAuthFlow** (`auth/browser_auth.py`) - OAuth authorization via browser redirect. Implements PKCE (code_verifier/code_challenge) and state parameter for CSRF protection. Spins up a local HTTP server to receive the callback.

### Privacy Model

Privacy settings in `Config.privacy`:
- `hash_titles` (default: True) - SHA-256 hash window titles, send first 16 hex chars
- `title_allowlist` - Apps that send raw titles (IDEs, terminals)
- `domain_only_urls` (default: True) - Strip URLs to domain only
- `exclude_apps` - Apps never tracked (1Password, System Preferences)

### Configuration Storage

- **Config**: `~/Library/Application Support/BetterFlow Sync/config.json` (macOS) or `%APPDATA%\BetterQA\BetterFlow Sync\config.json` (Windows)
- **Credentials**: System keychain via `keyring` library
- **Queue/Checkpoints**: SQLite at `Config.get_data_dir() / "offline_queue.db"`
- **Logs**: `Config.get_log_dir() / "betterflow-sync.log"`

### Bucket Types

ActivityWatch events come from three bucket types:
- `BUCKET_TYPE_WINDOW` - Active window (app, title, url)
- `BUCKET_TYPE_AFK` - Away-from-keyboard status
- `BUCKET_TYPE_INPUT` - Keystrokes/clicks/scrolls (fraud detection)

### Import Pattern

The codebase supports both module execution (`python -m src.main`) and PyInstaller bundled execution. Files use try/except for imports:

```python
try:
    from .config import Config  # Module execution
except ImportError:
    from config import Config   # PyInstaller bundle
```

## Testing

Tests use pytest with mocking. The test fixtures create Mock objects for `AWClient`, `BetterFlowClient`, and `OfflineQueue` to test `SyncEngine` logic in isolation.

```bash
# Run with coverage
pytest tests/ -v --cov=src --cov-report=term-missing
```

## CI/CD

GitHub Actions workflow (`.github/workflows/build.yml`) builds for macOS and Windows on push to `main`. Tagged releases (`v*`) create draft GitHub releases with ZIP artifacts.
