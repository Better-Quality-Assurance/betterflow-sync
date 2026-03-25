"""Update lifecycle: check, notify, auto-install, and apply updates."""

import logging
import threading
from typing import Optional

try:
    from .notifications import send_notification
except ImportError:
    from notifications import send_notification

logger = logging.getLogger(__name__)


class UpdateHandler:
    """Manages the update check/install lifecycle for BetterFlowApp.

    Owns _pending_update_lock and _update_jobs_lock (leaf locks).
    """

    def __init__(self, tray, config, coordinator, version: str) -> None:
        self.tray = tray
        self.config = config
        self.coordinator = coordinator
        self._version = version

        self._pending_update_asset_url: Optional[str] = None
        self._pending_update_lock = threading.Lock()
        self._update_jobs_started = False
        self._update_jobs_lock = threading.Lock()

    def ensure_update_checks_started(self) -> None:
        """Kick off update checks once after the tray is already visible."""
        if not self.config.check_updates:
            return
        with self._update_jobs_lock:
            if self._update_jobs_started:
                return
            self._update_jobs_started = True

        try:
            from .version_check import check_for_update
        except ImportError:
            from version_check import check_for_update

        from apscheduler.triggers.interval import IntervalTrigger

        try:
            check_for_update(
                self._version,
                channel=self.config.update_channel,
                callback=self._on_update_available,
            )
            if self.coordinator.scheduler.running:
                self.coordinator.scheduler.add_job(
                    self._periodic_update_check,
                    trigger=IntervalTrigger(hours=6),
                    id="update_check_job",
                    replace_existing=True,
                )
        except Exception:
            logger.exception("Failed to start update checker")

    def try_auto_install(self) -> None:
        """Auto-install a pending update if conditions are met."""
        with self.tray.model.lock:
            update_in_progress = self.tray.model.update_in_progress
        if update_in_progress:
            return
        with self._pending_update_lock:
            url = self._pending_update_asset_url
            if not url or not self.config.auto_install_updates:
                return
            self._pending_update_asset_url = None
        logger.info("Auto-installing pending update (user is idle)")
        send_notification("BetterFlow Update", "Downloading update, app will restart when complete.")
        self.on_install_update(url)

    def _on_update_available(self, version: str, url: str, asset_url: Optional[str] = None) -> None:
        """Handle update available notification."""
        logger.info(f"Update available: v{version} | {url} (asset: {asset_url})")
        self.tray.set_update_available(version, url, asset_url)
        if asset_url and self.config.auto_install_updates:
            with self._pending_update_lock:
                self._pending_update_asset_url = asset_url
            if self.coordinator.idle_paused:
                self.try_auto_install()
            else:
                send_notification(
                    "BetterFlow Update",
                    f"Version {version} available. Will install when you're away.",
                )
        elif asset_url:
            send_notification(
                "BetterFlow Update",
                f"Version {version} is available. Click 'Install & Restart' in the menu.",
            )
        else:
            send_notification(
                "BetterFlow Update",
                f"Version {version} is available.",
            )

    def _periodic_update_check(self) -> None:
        """Re-check for updates (called periodically by scheduler)."""
        if not self.config.check_updates:
            return
        with self.tray.model.lock:
            if self.tray.model.update_in_progress:
                return

        try:
            from .version_check import check_for_update
        except ImportError:
            from version_check import check_for_update

        try:
            check_for_update(
                self._version,
                channel=self.config.update_channel,
                callback=self._on_update_available,
            )
        except Exception:
            logger.debug("Periodic update check failed", exc_info=True)

    def on_install_update(self, asset_url: str) -> None:
        """Handle self-update: download, replace, relaunch."""
        try:
            from .self_updater import apply_update_async
        except ImportError:
            from self_updater import apply_update_async

        def on_progress(status: str) -> None:
            self.tray.set_update_progress(status)

        def on_complete(success: bool) -> None:
            if not success:
                with self.tray.model.lock:
                    self.tray.model.update_in_progress = False
                self.tray._update_menu()
                if self.config.auto_install_updates:
                    with self._pending_update_lock:
                        self._pending_update_asset_url = asset_url
                    send_notification("Update Failed", "Will retry next idle period.")
                else:
                    send_notification("Update Failed", "Self-update failed. Try again later.")

        apply_update_async(asset_url, on_progress=on_progress, on_complete=on_complete)
