"""In-process macOS window watcher using JXA.

Replaces the bf-window-tracker subprocess on macOS so that window tracking
inherits the main process's Accessibility permission instead of requiring
a separate grant for the subprocess binary.

Follows the same daemon-thread pattern as display_info.py.
"""

import json
import logging
import os
import platform
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _find_jxa_script() -> Optional[str]:
    """Locate printAppStatus.jxa in resources.

    Search order:
    1. Development: project_root/resources/trackers/darwin/bf-window-tracker/aw_watcher_window/
    2. PyInstaller frozen bundle: sys._MEIPASS/resources/...
    3. Persistent install dir: ~/Library/Application Support/BetterFlow Sync/trackers/darwin/...
    """
    relative = os.path.join(
        "resources", "trackers", "darwin",
        "bf-window-tracker", "aw_watcher_window", "printAppStatus.jxa",
    )

    # Development layout
    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dev_path = os.path.join(src_dir, relative)
    if os.path.isfile(dev_path):
        return dev_path

    # PyInstaller frozen bundle
    if getattr(sys, "frozen", False):
        frozen_path = os.path.join(sys._MEIPASS, relative)
        if os.path.isfile(frozen_path):
            return frozen_path

    # Persistent install directory
    install_path = os.path.join(
        os.path.expanduser("~/Library/Application Support/BetterFlow Sync"),
        "trackers", "darwin",
        "bf-window-tracker", "aw_watcher_window", "printAppStatus.jxa",
    )
    if os.path.isfile(install_path):
        return install_path

    return None


class MacOSWindowWatcher:
    """Daemon thread that polls the active window via JXA and posts heartbeats to AW."""

    def __init__(self, aw_client, poll_interval: float = 1.0):
        self._aw = aw_client
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hostname = platform.node()
        self._bucket_id = f"aw-watcher-window_{self._hostname}"
        self._jxa_path: Optional[str] = None

    def start(self) -> bool:
        """Create the AW bucket and start the polling thread.

        Returns True if started successfully, False otherwise.
        """
        self._jxa_path = _find_jxa_script()
        if not self._jxa_path:
            logger.error("Cannot start MacOSWindowWatcher: printAppStatus.jxa not found")
            return False

        try:
            self._aw.create_bucket(self._bucket_id, "currentwindow", self._hostname)
        except Exception as e:
            logger.warning(f"Failed to create window bucket (will retry on heartbeat): {e}")

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="macos-window-watcher",
        )
        self._thread.start()
        logger.info(f"MacOSWindowWatcher started (bucket={self._bucket_id}, poll={self._poll_interval}s)")
        return True

    def stop(self) -> None:
        """Signal the thread to stop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("MacOSWindowWatcher stopped")

    def _run(self) -> None:
        """Poll loop: run JXA script, parse JSON, post heartbeat."""
        while not self._stop_event.wait(self._poll_interval):
            try:
                result = subprocess.run(
                    ["osascript", self._jxa_path],
                    capture_output=True, text=True, timeout=10,
                )

                if result.returncode != 0:
                    stderr = result.stderr.strip()
                    # Accessibility errors are expected when permission is missing
                    if stderr:
                        logger.debug(f"JXA script error: {stderr}")
                    continue

                output = result.stdout.strip()
                if not output:
                    continue

                data = json.loads(output)

                # Build heartbeat data matching AW window watcher format
                heartbeat_data = {
                    "app": data.get("app", "unknown"),
                    "title": data.get("title", ""),
                }
                if data.get("url"):
                    heartbeat_data["url"] = data["url"]
                if data.get("incognito") is not None:
                    heartbeat_data["incognito"] = data["incognito"]

                timestamp = datetime.now(timezone.utc).isoformat()
                self._aw.post_heartbeat(
                    self._bucket_id, timestamp, heartbeat_data,
                    pulsetime=self._poll_interval + 1.0,
                )

            except json.JSONDecodeError as e:
                logger.debug(f"JXA output parse error: {e}")
            except subprocess.TimeoutExpired:
                logger.debug("JXA script timed out")
            except Exception as e:
                logger.debug(f"Window watcher poll error: {e}")
