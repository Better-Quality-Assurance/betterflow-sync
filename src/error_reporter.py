"""Best-effort crash/failure reporting to the betterqa-bot error ingest.

The desktop agent runs on many users' machines with no server-side visibility,
so hard failures — an unhandled crash, a sync that keeps failing, a hung sync,
a dead tray — were invisible to us. This module POSTs *those* (and only those)
to betterqa-bot's ``/notify/error`` webhook, which fingerprints, coalesces, and
routes them to the Slack logs channel.

Design constraints
------------------
* **Never block or break the caller.** Reporting normally runs on a daemon
  thread, swallows its own transport errors (logged, never re-raised), and is a
  no-op when no DSN is configured. ``block=True`` is available only for the
  fatal/exit path, where the process is about to die and the POST must complete
  before it does.
* **Don't flood the channel.** Identical failures are de-duplicated client-side
  within a cooldown window before they ever leave the machine; the bot
  coalesces further on its side.
* **Authenticate with a project-scoped DSN** (Bearer), never the global secret.
  The DSN comes from the environment so it is never committed to source or
  written to the on-disk config.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
import time
import traceback
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

try:
    from .sync.http_client import resolve_ca_bundle
except ImportError:  # PyInstaller bundle (src/ is import root)
    from sync.http_client import resolve_ca_bundle

logger = logging.getLogger(__name__)

# betterqa-bot error ingest. Overridable for staging/self-host; the production
# default is safe to ship because reporting stays off until a DSN is present.
DEFAULT_ENDPOINT = "https://betterqa-bot-production.up.railway.app/notify/error"

PROJECT = "betterflow-sync"  # must match the DSN's bound project on the bot

_MAX_MESSAGE = 10_000
_MAX_STACK = 50_000


class ErrorReporter:
    """Sends selected failures to the betterqa-bot logs channel.

    A no-op unless constructed with a non-empty ``dsn`` and ``endpoint``.
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str],
        dsn: Optional[str],
        release: str,
        environment: str = "production",
        context_provider: Optional[Callable[[], dict]] = None,
        timeout: float = 5.0,
        dedup_window: float = 300.0,
    ) -> None:
        self._endpoint = (endpoint or "").strip()
        self._dsn = (dsn or "").strip()
        self._release = release
        self._environment = environment
        self._context_provider = context_provider
        self._timeout = timeout
        self._dedup_window = dedup_window

        # fingerprint -> monotonic timestamp of last send; guarded by _lock.
        # fingerprint -> (monotonic timestamp, the window it was recorded under)
        self._recent: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._endpoint and self._dsn)

    def capture(
        self,
        message: str,
        *,
        level: str = "error",
        exc: Optional[BaseException] = None,
        tags: Optional[dict] = None,
        context: Optional[dict] = None,
        fingerprint: Optional[str] = None,
        block: bool = False,
        dedup_window: Optional[float] = None,
    ) -> None:
        """Report a failure. Safe to call from any thread; never raises.

        Args:
            message: Short human-readable summary (truncated).
            level: "warning" | "error" | "fatal".
            exc: Optional exception whose traceback becomes the stack.
            tags: Small string map for grouping (component, platform, ...).
            context: Freeform diagnostic data (merged with user/device context).
            fingerprint: Stable dedup key; defaults to level + message.
            block: Send synchronously (only for the about-to-exit fatal path).
            dedup_window: Per-call cooldown override in seconds. ``None`` (the
                default, and what every caller gets unless it says otherwise)
                means "use the instance default". ``0`` disables suppression
                for this call, for reports whose OCCURRENCE COUNT is the
                measurement — a shared window throttles short-interval
                fingerprints harder than long-interval ones, which silently
                skews the very distribution such a report exists to publish.
        """
        if not self.enabled:
            return

        try:
            key = fingerprint or f"{level}:{message}"
            if not self._should_send(key, dedup_window):
                logger.debug("Error report suppressed by client-side dedup: %s", key)
                return

            payload = self._build_payload(message, level, exc, tags, context)
        except Exception as e:
            # Building the report must never take down the caller.
            logger.warning("Error report skipped (could not build payload): %s", e)
            return

        if block:
            self._post(payload)
        else:
            threading.Thread(
                target=self._post,
                args=(payload,),
                name="error-report",
                daemon=True,
            ).start()

    def _should_send(self, key: str, dedup_window: Optional[float] = None) -> bool:
        """True if this fingerprint hasn't been sent within the cooldown window.

        ``dedup_window`` overrides the instance default for this call only;
        ``None`` means use the default.

        Each entry stores the window it was recorded under, and pruning keeps it
        for the LONGER of that window and the instance default. Pruning on the
        instance default alone deleted a longer-lived entry before the
        suppression check below could read it, so a caller asking for 3600s got
        300s of suppression and then started sending again -- on a ~5-minute
        heartbeat that is one send per beat instead of one per hour.

        The `max` is not belt-and-braces, it is the other half of the contract:
        a `dedup_window=0` call still has to STAMP the fingerprint so the next
        caller -- one that asked for nothing -- is suppressed by the instance
        default. Pruning on the entry's own window alone would make a
        zero-window entry instantly stale and let that next call through, which
        is the override leaking into a caller that never used it.

        Pruning exists only to bound the dict's growth, so it must never evict
        something a later check could still need. Every entry does still expire.
        """
        window = self._dedup_window if dedup_window is None else dedup_window
        now = time.monotonic()
        with self._lock:
            # Prune stale entries so the dict can't grow without bound.
            stale = [
                k
                for k, (ts, w) in self._recent.items()
                if now - ts > max(w, self._dedup_window)
            ]
            for k in stale:
                del self._recent[k]

            last = self._recent.get(key)
            if last is not None and now - last[0] < window:
                return False
            self._recent[key] = (now, window)
            return True

    def _build_payload(
        self,
        message: str,
        level: str,
        exc: Optional[BaseException],
        tags: Optional[dict],
        context: Optional[dict],
    ) -> dict:
        merged_tags = {
            "component": "unknown",
            "platform": platform.system(),
            "platform_release": platform.release(),
            **(tags or {}),
        }

        merged_context: dict = {}
        if self._context_provider is not None:
            try:
                provided = self._context_provider()
                if isinstance(provided, dict):
                    merged_context.update(provided)
            except Exception as e:
                logger.debug("Error context provider failed: %s", e)
        if context:
            merged_context.update(context)

        payload: dict = {
            "project": PROJECT,
            "environment": self._environment,
            "level": level,
            "message": message[:_MAX_MESSAGE],
            "release": self._release,
            "tags": merged_tags,
            "context": merged_context,
        }

        if exc is not None:
            stack = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            payload["stack"] = stack[:_MAX_STACK]

        return payload

    def _post(self, payload: dict) -> None:
        """POST the report. Swallows all transport errors (logged, never raised)."""
        # The DSN rides in `Authorization: Bearer ...` on every report, so refuse
        # to send it over plaintext. The default endpoint is HTTPS, but
        # BETTERFLOW_ERROR_ENDPOINT (env or baked) could point elsewhere; mirror
        # bf_client's token-exchange guard (HTTPS, or loopback for dev).
        parsed = urlparse(self._endpoint)
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
            logger.warning(
                "Error report skipped — endpoint is not HTTPS (won't send the DSN "
                "in clear): %s",
                self._endpoint,
            )
            return
        try:
            resp = requests.post(
                self._endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self._dsn}"},
                timeout=self._timeout,
                verify=resolve_ca_bundle(),
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Error report rejected (%s): %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except Exception as e:
            logger.warning("Error report POST failed: %s", e)


def install_crash_hooks(reporter: ErrorReporter) -> Callable[[], None]:
    """Report unhandled exceptions (main thread + worker threads) as fatal.

    Installs ``sys.excepthook`` and ``threading.excepthook`` that send a fatal
    report (blocking, so it leaves the machine before the process dies) and
    then chain to whatever hooks were previously installed — preserving the
    existing logging/exit behaviour. Returns a callable that restores the
    previous hooks (used by tests to avoid leaking global state).
    """
    prev_excepthook = sys.excepthook
    prev_thread_hook = threading.excepthook

    def _handle_uncaught(exc_type, exc_value, exc_tb):
        if not issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            try:
                reporter.capture(
                    f"Unhandled exception: {exc_type.__name__}: {exc_value}",
                    level="fatal",
                    exc=exc_value,
                    tags={"component": "uncaught"},
                    block=True,
                )
            except Exception:
                logger.debug("Crash report from excepthook failed", exc_info=True)
        prev_excepthook(exc_type, exc_value, exc_tb)

    def _handle_thread_uncaught(args):
        if args.exc_type is not None and not issubclass(
            args.exc_type, (KeyboardInterrupt, SystemExit)
        ):
            try:
                reporter.capture(
                    f"Unhandled thread exception: {args.exc_type.__name__}: {args.exc_value}",
                    level="fatal",
                    exc=args.exc_value,
                    tags={
                        "component": "uncaught-thread",
                        "thread": getattr(args.thread, "name", "?"),
                    },
                    block=True,
                )
            except Exception:
                logger.debug("Crash report from thread excepthook failed", exc_info=True)
        prev_thread_hook(args)

    sys.excepthook = _handle_uncaught
    threading.excepthook = _handle_thread_uncaught

    def _restore() -> None:
        sys.excepthook = prev_excepthook
        threading.excepthook = prev_thread_hook

    return _restore


def _baked(name: str) -> Optional[str]:
    """Read a value stamped into the gitignored _build_secrets at build time.

    Absent when running from source (the module is only generated by the
    PyInstaller build), so this returns None and the env vars take over.
    """
    try:
        from . import _build_secrets  # type: ignore[attr-defined]
    except ImportError:
        try:
            import _build_secrets  # type: ignore[no-redef]
        except ImportError:
            return None
    value = getattr(_build_secrets, name, None)
    return value if isinstance(value, str) and value else None


def from_env(
    *,
    release: str,
    context_provider: Optional[Callable[[], dict]] = None,
) -> ErrorReporter:
    """Build an ErrorReporter from environment + baked build-time config.

    Precedence is env var first (dev/runtime override), then the value baked
    into the bundle at build time, then the default. Reads
    ``BETTERFLOW_ERROR_DSN`` (project-scoped DSN; reporting stays off until
    set), ``BETTERFLOW_ERROR_ENDPOINT`` (ingest URL), and
    ``BETTERFLOW_ERROR_ENV`` (environment label). The DSN never lives in source
    or config.json — only in the environment or the gitignored _build_secrets.
    """
    dsn = os.getenv("BETTERFLOW_ERROR_DSN") or _baked("ERROR_DSN")
    endpoint = (
        os.getenv("BETTERFLOW_ERROR_ENDPOINT") or _baked("ERROR_ENDPOINT") or DEFAULT_ENDPOINT
    )
    environment = (
        os.getenv("BETTERFLOW_ERROR_ENV") or _baked("ERROR_ENV") or "production"
    )
    return ErrorReporter(
        endpoint=endpoint,
        dsn=dsn,
        release=release,
        environment=environment,
        context_provider=context_provider,
    )
