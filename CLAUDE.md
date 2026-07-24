# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BetterFlow is a Python desktop app that syncs ActivityWatch data to BetterFlow for automatic time tracking. It runs as a system tray application on macOS, Windows, and Linux (X11), polling ActivityWatch locally and sending events to the BetterFlow API.

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
make pkg              # Build signed+notarized macOS .pkg for MDM (see docs/SIGNING.md)

# Linux build (run on Linux; needs gobject-introspection + libnotify-bin)
make build-linux      # Build dist/BetterFlow/ one-dir bundle
make appimage         # Package as dist/BetterFlow-linux-x86_64.AppImage

# Windows build (run on Windows)
powershell -ExecutionPolicy Bypass -File build-windows.ps1
```

The Linux build mirrors the Windows path: it runs the external bundled
ActivityWatch trackers (server + window + idle) as subprocesses with no
in-process watchers and no permissions model. The in-app updater replaces the
running `.AppImage` in place via the `$APPIMAGE` env var.

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
        |-- Client-side privacy: drop excluded apps, strip URLs to domain
        |-- Transform events to BetterFlow format
        v
    BetterFlowClient (src/sync/bf_client.py)
        |
        v (offline?)
    OfflineQueue (SQLite) ----> Retry when back online
```

### Core Components

**BetterFlowApp** (`main.py`) - Main application orchestrator. Initializes all components, manages the APScheduler sync loop (default 60s), handles tray icon state transitions.

**SyncEngine** (`sync/sync_engine.py`) - Orchestrates AW -> BetterFlow data flow. Fetches events since last checkpoint, applies privacy transformations, batches and sends to API. On network failure, queues events to SQLite.

**OfflineQueue** (`sync/queue.py`) - SQLite-backed queue for offline resilience. Also stores per-bucket sync checkpoints (last synced timestamp/event ID). Thread-safe with per-thread connections.

**BrowserAuthFlow** (`auth/browser_auth.py`) - OAuth authorization via browser redirect. Implements PKCE (code_verifier/code_challenge) and state parameter for CSRF protection. Spins up a local HTTP server to receive the callback.

### Privacy Model

**Important:** window titles are sent **raw** to the backend, which applies
privacy server-side (title handling + categorization). The agent does **not**
hash titles before egress — there is no client-side title hashing today, and
implementing it would disable server-side categorization (titles are the
categorization signal). The settings in `Config.privacy`:

- `domain_only_urls` (default: True) - **enforced client-side** — URLs are
  reduced to their domain before egress. Full URLs require `collect_full_urls`
  (default False, opt-in, sensitive).
- `exclude_apps` (default: 1Password, Keychain, System Settings, ...) -
  **enforced client-side** — these apps' events never leave the device. This is
  the real "never send this app's titles" control. Enforced at the **egress
  chokepoint**, `BetterFlowClient.send_events` (via `src/sync/privacy_filter.py`),
  because that is the one function that puts events on the wire. Do NOT re-add
  the check to individual producers and do NOT add a new send path that bypasses
  `send_events`: enforcing per-producer is exactly how the in-process window and
  input sources shipped able to egress 1Password titles.
  `Config.update_from_server` may only **extend** this list (union with the
  shipped defaults), and only once `DEFER_UNAPPLIED_SERVER_SETTINGS` is lifted —
  a server row can never un-exclude an app.

**`hash_titles` and `title_allowlist` no longer exist here (removed 2026-07-23).**
They were declared on `PrivacySettings`, persisted to `config.json`, populated
from `/config` and carried in the tray model — and read by nothing. No capture or
transform path ever consumed them, so the agent's behaviour was identical with
them on or off. A field named `hash_titles` in a settings surface offered to
employees, next to controls that *are* enforced, misrepresents how their data is
handled; since we deliberately keep sending titles raw (they are the server's
categorisation signal), removal was the fix rather than implementation.

The agent never sent either value to the server — there is no outbound payload or
PATCH carrying them — so their removal changed nothing server-side. The server
still emits `privacy.hash_window_titles` / `privacy.title_allowlist` on `/config`;
`Config.update_from_server` drops both on purpose, with a comment saying why.
**Title handling is a server-side control** (`AgentDevice::shouldStoreRawTitle` in
internal-tool2, driven by the `agent_devices` row); the agent has no say in it and
must not appear to. `tests/test_hash_titles_control_removed.py` fails if either
symbol returns as live code in `src/`.

The OS keychain holds the auth token (never on disk / in config). If raw titles
must never leave the device, that requires client-side hashing/redaction, which
is **not implemented** — track it as a feature, not an assumed guarantee.

#### Device identifiers sent on the heartbeat

Alongside activity data the agent reports identifiers describing the *machine*,
not the person using it:

- `device_id` — a `sync:<uuid>` generated on this device at first run.
- `hardware_serial` (`src/hardware_serial.py`) — the machine's hardware serial:
  `IOPlatformSerialNumber` on macOS, `Win32_BIOS.SerialNumber` on Windows,
  `/sys/class/dmi/id/product_serial` on Linux. Read once at startup and cached;
  no permission is requested or required, and an unreadable serial is reported
  as `null` rather than retried. **Why it is collected:** the MDM asset
  inventory keys company laptops by serial while the agent keyed itself by a
  random UUID, so the two systems could not be joined and "is this machine
  managed?" could only be guessed at by matching people's names. **Scope:**
  asset correlation only. It identifies hardware, never an individual, it does
  not affect tracked or billed time, and it must not reach a client-facing
  surface. A VM, a container or a locked-down Linux box legitimately reports
  `null`.
- `timezone`, `agent_version`, and the tracker-health telemetry (restart counts,
  event ages, sync staleness, and the two capture-dead flags
  `tracker_download_failed` / `managed_components_unavailable`, which say the
  tracker binaries could not be installed and that this process has no managed
  watchers of its own). All of it describes whether the machine is capable of
  recording, never what was recorded.

The serial is shown back to the user in the tray under **Diagnostics > Device
serial**, next to the Privacy Policy link, and clicking it copies the value.
Displaying the literal string we hold is the disclosure that actually carries
weight — a first-run wizard bullet is read once and forgotten. It renders as
`Device serial: unavailable` when the probe found nothing, never blank and never
`None`. Keep that row in step with what is actually sent.

- `disclosure_acknowledgement` (`src/privacy_notice.py`) — `{version,
  acknowledged_at}`, the record that this user was shown the data-collection
  notice before monitoring continued (Romanian Law 190/2018 art. 5 lit. b).
  Carries no activity data and no device id: the server binds the record from
  the authenticated heartbeat and writes `agent_device_id` itself. The key name
  and shape are the server's contract (`AgentHeartbeatController` →
  `agent_disclosure_acknowledgements`), so neither end may be renamed alone.

`src/sync/bf_client.py`'s `HEARTBEAT_HEALTH_KEYS` is the complete, enforced list
of what the heartbeat forwards — a field missing from it never leaves the
machine. Treat that tuple as the source of truth when auditing egress, and
update this section whenever it changes.

#### The one-time privacy notice

`src/privacy_notice.py` holds the disclosure text, its version, and the
acknowledgement record; `src/ui/privacy_notice_window.py` is a rendering shell
with no copy of its own. It shows once per *text version*, on all three
platforms, from `BetterFlowApp.run` — deliberately NOT from the first-run
consent screen, which macOS never executes.

Two rules when editing it:

- **Do not hand-edit a version.** `NOTICE_VERSION` is a SHA-256 of the notice
  text, so changing a single character re-shows the notice to the whole fleet
  automatically. That is the feature: a new data category must never reach
  devices that acknowledged the old text.
- **The three qualifiers in `REQUIRED_QUALIFIERS` are legal, not editorial** —
  titles leave the machine *as recorded*, input is *counts only*, and the serial
  *identifies the machine*. Tests fail if any is removed.

The English notice is a rendering of Regulament Intern art. 64^1 alin. (4)-(5).
If the two disagree, the Romanian prevails — it is the version employees sign.

### Configuration Storage

- **Config**: `~/Library/Application Support/BetterFlow/config.json` (macOS) or `%APPDATA%\BetterQA\BetterFlow\config.json` (Windows)
- **Credentials**: System keychain via `keyring` library
- **Queue/Checkpoints**: SQLite at `Config.get_data_dir() / "offline_queue.db"`
- **Logs**: `Config.get_log_dir() / "betterflow.log"`

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

## Building DMG (IMPORTANT)

**Before every `make dmg` or `make build-mac`**, bump the version in `src/__init__.py`. The build spec reads `__version__` automatically for `CFBundleVersion` and the tray menu display.

```bash
# 1. Bump version in src/__init__.py
# 2. Build
make dmg
# 3. Install: drag from DMG to /Applications
```

### macOS build environment

`make build-mac` handles the three environment traps that used to fail a first
build (missing CA bundle, wrong-architecture `pyinstaller` off PATH, stale
`dist/`). See `docs/SIGNING.md` § "Build environment traps on macOS". Short
version:

- The tracker download resolves certifi's CA bundle itself — do **not** export
  `SSL_CERT_FILE` by hand.
- PyInstaller is resolved by `scripts/resolve-pyinstaller.sh`, which verifies the
  interpreter's architecture and fails loudly rather than producing a silently
  x86_64 app. Building from a **git worktree** has no venv, so pass
  `PYINSTALLER_PYTHON=/path/to/betterflow-sync/venv/bin/python3`.
- `make clean-dist` (a dependency of `build-mac`) removes only
  `dist/BetterFlow.app` and `dist/BetterFlow`.

Never capture a build's exit code through a pipeline
(`( make ... ; echo "EXIT: $?" )` reports success for a failed build — the echo
succeeded). Write the status to its own file and verify the artifact:
`lipo -archs dist/BetterFlow.app/Contents/MacOS/BetterFlow`.

## CI/CD

GitHub Actions workflow (`.github/workflows/build.yml`) builds for macOS and Windows on push to `main`. Tagged releases (`v*`) create draft GitHub releases with ZIP artifacts.
