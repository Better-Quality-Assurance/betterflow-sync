"""Microphone-in-use meeting detection.

STATUS: WINDOWS-ONLY, and mic detection is USAGE-ONLY by policy — the agent
reads whether the mic is IN USE (a hardware/consent-store flag), never the
audio itself. ``create_mic_detector`` returns None on macOS/Linux: the macOS
signals below (CoreAudio device-level "running somewhere" + a process-existence
conferencing gate) over-credit idle media playback as active work. macOS is NOT
to be re-enabled via per-process CoreAudio tap APIs — those reach into the audio
stream, which the usage-only policy forbids (decision 2026-07-17). macOS stays
on window-title ``CallDetector`` for meeting credit. The macOS probe/gate are
kept only as usage-only reference. See ``create_mic_detector`` and workspace
memory macos-mic-credit-overbilling-killswitch.

The window-title path (``CallDetector``) only sees a meeting while the call
window is FRONTMOST and titled like a call. In real usage the huddle runs in
the background — the user reads docs, or just listens with some other window
focused — the title match drops after the 30s grace, and a hands-off meeting
is painted idle again (Ecaterina's Slack huddle, 2026-07-15).

The microphone is the system-level signal the window title can't fake or miss:
it is hot for the entire meeting regardless of focus. This detector samples
"is the default input device in use" once per sync cycle and turns it into the
same three consumer feeds every engagement detector here provides:

  * ``get_last_active_at()`` → folded into the in-process AFK stream
    (``AfkSource``) so the *uploaded* stream stays not-afk and the server
    bills the meeting as active.
  * ``is_active()``          → consulted by ``IdleManager`` next to
    ``is_in_call()`` so the local idle pause doesn't fire mid-meeting.
  * ``observe()/snapshot()`` → an auditable ``call``-type span
    (``call_type: "mic"``) uploaded live each cycle, so the SERVER can see,
    validate, or override every credited mic session. Rides the existing call
    bucket/event type — no new server-side event shape.

Anti-fraud guardrails:

  * **Conferencing-gated** — the mic must be held by (Windows: attributed via
    the CapabilityAccessManager consent store) or coexist with (macOS: process
    scan; per-app attribution isn't exposed) a known conferencing app. A bare
    open mic on a machine with no conferencing software never counts.
  * **Capped** — AFK credit stops ``max_credit_seconds`` after the session
    started, mirroring ``CallDetector``; a wedged mic can't farm hours.
  * **Server-visible** — every session ships as an upserted call event, the
    same audit path window-detected calls use.

Platform probes:
  * macOS  — CoreAudio ``kAudioDevicePropertyDeviceIsRunningSomewhere`` on the
    default input device (no TCC prompt; it reads a hardware property, not
    audio).
  * Windows — ``HKCU ...\\CapabilityAccessManager\\ConsentStore\\microphone``:
    a subkey with ``LastUsedTimeStop == 0`` is an app holding the mic right
    now, exe name included.
  * Linux — no probe; the detector is not constructed.
"""

import logging
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol

try:
    from .aw_client import BUCKET_TYPE_CALL, CALL_STATUS_COMPLETED, CALL_STATUS_ONGOING
    from .engagement_credit import capped_credit
except ImportError:  # PyInstaller bundle (src/ is import root)
    from sync.aw_client import BUCKET_TYPE_CALL, CALL_STATUS_COMPLETED, CALL_STATUS_ONGOING
    from sync.engagement_credit import capped_credit

logger = logging.getLogger(__name__)


@dataclass
class MicSample:
    """A single observation of the default input device.

    ``in_use`` is None when the probe couldn't read the device state (never
    treated as "in use"). ``apps`` carries per-app attribution where the
    platform exposes it (Windows consent store); empty on macOS.
    """

    in_use: Optional[bool]
    apps: tuple = ()


class MicProbe(Protocol):
    """Samples microphone state. Injected so the session logic is testable
    without CoreAudio / the Windows registry."""

    def sample(self) -> MicSample: ...


# Tokens identifying conferencing-capable apps. Browsers are included: a
# mic-hot browser is almost always a web meeting (Meet, browser Zoom/Teams).
# Matched case-insensitively ON A WORD BOUNDARY against process/exe names —
# bare substrings bleed into unrelated apps ("arc" in "SearchApp"/"archive",
# "edge" in "ledger"), the exact lesson call_detector's app aliases already
# encode. Compound process names that a boundary would break get their own
# token (msedge).
_CONFERENCING_TOKENS = (
    "zoom",
    "teams",
    "slack",
    "discord",
    "webex",
    "facetime",
    "chrome",
    "safari",
    "firefox",
    "edge",
    "msedge",
    "brave",
    "arc",
    "opera",
    "vivaldi",
)

_CONFERENCING_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _CONFERENCING_TOKENS) + r")\b",
    re.IGNORECASE,
)

# Keep a session open across brief mic drops (device switch, app re-grabbing
# the stream). Well under any real between-meetings gap at a 60s sample cadence.
_MIC_OFF_GRACE_SECONDS = 90.0


def _matches_conferencing(name: str) -> bool:
    return _CONFERENCING_RE.search(name) is not None


class MicActivityDetector:
    """Tracks contiguous mic-in-use spans and turns them into AFK credit and
    an auditable call-type span.

    Thread-safety: ``observe`` runs on the sync thread; ``get_last_active_at``
    (inside ``AfkSource.record_sample``) and ``is_active`` (idle thread) read
    concurrently — all shared state is behind ``_lock``.
    """

    def __init__(
        self,
        *,
        hostname: str,
        probe: MicProbe,
        conferencing_gate=None,
        min_session_seconds: float = 30.0,
        max_credit_seconds: float = 14400.0,
        off_grace_seconds: float = _MIC_OFF_GRACE_SECONDS,
    ) -> None:
        self._hostname = hostname
        self._probe = probe
        # () -> bool: "is a conferencing app present" fallback when the probe
        # can't attribute the mic to an app (macOS). None → attribution-only.
        self._conferencing_gate = conferencing_gate
        self._min_session_seconds = float(min_session_seconds)
        self._max_credit_seconds = float(max_credit_seconds)
        self._off_grace_seconds = float(off_grace_seconds)
        self._lock = threading.Lock()

        # Open session state.
        self._session_start: Optional[datetime] = None
        self._session_last_hot: Optional[datetime] = None
        self._session_app: str = "Microphone"
        # Span of the most recently ENDED session: credit stays truthful after
        # the meeting ends (freezes at its end), mirroring CallDetector.
        self._last_session_start: Optional[datetime] = None
        self._last_session_end: Optional[datetime] = None
        # Latched after a force-close at the credit cap: a mic that never goes
        # cold must NOT reopen a fresh session every cycle (each one would
        # suppress the idle pause for another full cap and mint another 4h
        # audit row — a wedged mic looping forever). Cleared by the first
        # genuinely cold observation.
        self._cap_latched = False

    # -- Sampling ----------------------------------------------------------

    def observe(self, now: datetime) -> Optional[dict]:
        """Sample the mic and update session state. Call once per sync cycle.

        Returns a finished call-type event dict when this sample closes a
        session that was long enough; otherwise None.
        """
        try:
            sample = self._probe.sample()
        except Exception as e:
            # A probe failure must never break a sync cycle and is never read
            # as "in use" — but it doesn't insta-close an open session either;
            # the off-grace handles a transient blind read.
            logger.debug("mic probe failed: %s", e)
            sample = MicSample(None)

        hot = bool(sample.in_use) and self._passes_conferencing_gate(sample)

        with self._lock:
            if hot:
                if self._cap_latched:
                    # A cap-force-closed session must not reopen while the mic
                    # never went cold — that loop would suppress the idle
                    # pause for a fresh cap period every reopen, forever.
                    return None
                if self._session_start is not None and (
                    (now - self._session_start).total_seconds()
                    >= self._max_credit_seconds
                ):
                    # Force-close at the credit cap even while still hot: past
                    # the cap the session earns nothing, its growing snapshot
                    # would eventually exceed the server's max event duration
                    # (batch poison), and is_active() would suppress the local
                    # idle pause indefinitely. Latched: reopening requires the
                    # mic to genuinely go cold first.
                    self._cap_latched = True
                    return self._close_session_locked()
                if self._session_start is None:
                    self._session_start = now
                    self._session_app = self._attributed_app(sample)
                    logger.info(
                        "Mic meeting session started (%s)", self._session_app
                    )
                self._session_last_hot = now
                return None

            # Only a DEFINITE cold read clears the latch. A probe error
            # (in_use=None) or a gate failure also lands here with hot=False,
            # but neither proves the mic released — unlatching on those would
            # let a wedged-hot mic reopen a fresh cap period off every
            # transient flap, the exact loop the latch exists to stop.
            if sample.in_use is False:
                self._cap_latched = False
            if self._session_start is None:
                return None
            last_hot = self._session_last_hot or self._session_start
            if (now - last_hot).total_seconds() < self._off_grace_seconds:
                return None  # brief drop — keep the session open
            return self._close_session_locked()

    def _passes_conferencing_gate(self, sample: MicSample) -> bool:
        """A hot mic counts only in a conferencing context.

        Windows attributes the mic per app — require a conferencing match.
        macOS can't attribute (``apps`` empty), so fall back to "a conferencing
        app is running" via the injected gate. A gate ERROR fails CLOSED: this
        signal feeds billing, so an error must never become an unconditional
        pass (a reproducible gate crash would otherwise be a free bypass). The
        cost is bounded — a transient error delays session open by one cycle
        (the mic is still hot next cycle) and a mid-session blip is absorbed by
        the off-grace window.
        """
        if sample.apps:
            return any(_matches_conferencing(app) for app in sample.apps)
        if self._conferencing_gate is None:
            return True
        try:
            return bool(self._conferencing_gate())
        except Exception as e:
            logger.warning("mic conferencing gate failed (failing closed): %s", e)
            return False

    @staticmethod
    def _attributed_app(sample: MicSample) -> str:
        for app in sample.apps:
            if _matches_conferencing(app):
                return app
        return "Microphone"

    # -- Consumer feeds ----------------------------------------------------

    def snapshot(self) -> Optional[dict]:
        """Emit the open session as an event WITHOUT closing it (uploaded each
        cycle; deterministic id → the server upserts one growing row)."""
        with self._lock:
            if self._session_start is None:
                return None
            start = self._session_start
            end = self._session_last_hot or start
            app = self._session_app
        duration = min((end - start).total_seconds(), self._max_credit_seconds)
        if duration < self._min_session_seconds:
            return None
        return self._make_event(app, start, duration, status=CALL_STATUS_ONGOING)

    def flush(self) -> Optional[dict]:
        """Force-close any open session — shutdown, kill-switch, and capture
        boundaries (pause / private time / working-hours suppression)."""
        with self._lock:
            if self._session_start is None:
                return None
            return self._close_session_locked()

    def is_active(self) -> bool:
        """True while a mic meeting session is open — consumed by
        ``IdleManager`` to suppress the idle pause, like ``is_in_call``."""
        with self._lock:
            return self._session_start is not None

    def get_last_active_at(
        self, base_last_input: Optional[datetime], now: datetime
    ) -> Optional[datetime]:
        """Most recent mic-engaged instant for ``AfkSource`` (activity-source
        contract).

        While a session is open, credit tracks the last HOT observation plus
        the off-grace allowance (not bare ``now`` — after the mic actually
        goes cold, credit must stop growing during the grace window, mirroring
        CallDetector's leave-instant rule). After close it freezes at the
        session end. Both are capped at ``max_credit_seconds`` past
        ``base_last_input`` — the real-input anchor — so cycling the mic
        off/on can't re-arm the cap (see ``engagement_credit``)."""
        with self._lock:
            if self._session_start is not None:
                last_hot = self._session_last_hot or self._session_start
                engaged_until = min(
                    now, last_hot + timedelta(seconds=self._off_grace_seconds)
                )
                return capped_credit(
                    engaged_until,
                    anchor=base_last_input,
                    engagement_start=self._session_start,
                    cap_seconds=self._max_credit_seconds,
                    now=now,
                )
            if (
                self._last_session_start is not None
                and self._last_session_end is not None
            ):
                return capped_credit(
                    self._last_session_end,
                    anchor=base_last_input,
                    engagement_start=self._last_session_start,
                    cap_seconds=self._max_credit_seconds,
                    now=now,
                )
            return None

    # -- Internals ---------------------------------------------------------

    def _close_session_locked(self) -> Optional[dict]:
        """Close the open session; caller holds ``_lock``."""
        start = self._session_start
        end = self._session_last_hot or start
        app = self._session_app
        self._session_start = None
        self._session_last_hot = None
        self._session_app = "Microphone"
        # Credit memory survives the reset — engagement until `end` stays true.
        self._last_session_start = start
        self._last_session_end = end

        duration = min((end - start).total_seconds(), self._max_credit_seconds)
        if duration < self._min_session_seconds:
            logger.debug(
                "Mic session too short (%.0fs < %.0fs), discarding",
                duration,
                self._min_session_seconds,
            )
            return None
        logger.info("Mic meeting session ended: %s duration=%.0fs", app, duration)
        return self._make_event(app, start, duration, status=CALL_STATUS_COMPLETED)

    def _make_event(
        self, app: str, start: datetime, duration: float, *, status: str
    ) -> dict:
        return {
            # Deterministic id → the server upserts one row per session across
            # per-cycle snapshots and the final close, mirroring call events.
            "id": f"micsession_{self._hostname}_{int(start.timestamp())}",
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_id": f"bf-call-detector_{self._hostname}",
            "bucket_type": BUCKET_TYPE_CALL,
            "data": {
                "app": app,
                "call_type": "mic",
                # "ongoing" on live snapshots, "completed" on close/flush: the
                # server must be able to tell a growing live row from a final
                # one (a queued stale snapshot replayed after the final event
                # must not masquerade as completed), and monotonic-growth
                # enforcement server-side needs the distinction.
                "status": status,
                "synthetic": True,
            },
        }


# -- Platform probes -----------------------------------------------------


class MacosMicProbe:
    """Default-input-device "running somewhere" via CoreAudio (ctypes).

    Known caveat: ``kAudioDevicePropertyDeviceIsRunningSomewhere`` is
    DEVICE-level, not input-stream-level. On single-device input+output
    hardware (USB/BT headsets, audio interfaces) playback alone can mark the
    default input device "running" — music through such a headset reads as a
    hot mic. This device-level inaccuracy is why macOS mic credit is disabled
    (``create_mic_detector`` returns None). It is NOT fixed by macOS 14+ tap
    APIs: those attribute the stream by reaching into the audio itself, which
    the usage-only policy forbids. This probe is usage-only (a hardware flag,
    no audio) and kept for reference only.
    """

    _SYSTEM_OBJECT = 1  # kAudioObjectSystemObject

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        self._ca = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )

        class _PropertyAddress(ctypes.Structure):
            _fields_ = [
                ("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32),
            ]

        self._PropertyAddress = _PropertyAddress

    @staticmethod
    def _fourcc(code: str) -> int:
        return int.from_bytes(code.encode("ascii"), "big")

    def _get_uint32(self, object_id: int, selector: str) -> Optional[int]:
        ctypes = self._ctypes
        addr = self._PropertyAddress(
            self._fourcc(selector), self._fourcc("glob"), 0
        )
        value = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = self._ca.AudioObjectGetPropertyData(
            ctypes.c_uint32(object_id),
            ctypes.byref(addr),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(value),
        )
        if status != 0:
            return None
        return value.value

    def sample(self) -> MicSample:
        try:
            # 'dIn ' = kAudioHardwarePropertyDefaultInputDevice
            device = self._get_uint32(self._SYSTEM_OBJECT, "dIn ")
            if not device:
                return MicSample(None)
            # 'gone' = kAudioDevicePropertyDeviceIsRunningSomewhere
            running = self._get_uint32(device, "gone")
            if running is None:
                return MicSample(None)
            return MicSample(bool(running))
        except Exception as e:
            logger.debug("MacosMicProbe failed: %s", e)
            return MicSample(None)


class WindowsMicProbe:
    """Apps currently holding the mic, via the CapabilityAccessManager consent
    store (``LastUsedTimeStop == 0`` means "in use right now")."""

    _ROOT = (
        r"Software\Microsoft\Windows\CurrentVersion"
        r"\CapabilityAccessManager\ConsentStore\microphone"
    )

    def sample(self) -> MicSample:
        import winreg

        apps: list[str] = []
        try:
            for base in (self._ROOT, self._ROOT + r"\NonPackaged"):
                try:
                    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
                except OSError:
                    continue
                with key:
                    index = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        index += 1
                        if sub == "NonPackaged":
                            continue
                        try:
                            with winreg.OpenKey(key, sub) as sub_key:
                                stop, _ = winreg.QueryValueEx(
                                    sub_key, "LastUsedTimeStop"
                                )
                                start, _ = winreg.QueryValueEx(
                                    sub_key, "LastUsedTimeStart"
                                )
                        except OSError:
                            continue
                        # In use right now = a real usage started (Start != 0)
                        # and hasn't stopped (Stop == 0). Start==0 entries are
                        # granted-but-never-used — not "in use". Known residual:
                        # an app that CRASHED holding the mic leaves Stop==0
                        # until its next clean release; the conferencing gate,
                        # input-anchored cap, and cap latch bound the damage.
                        if stop == 0 and start != 0:
                            # NonPackaged keys encode the exe path with '#'
                            # separators; the last segment is the exe name.
                            apps.append(sub.rsplit("#", 1)[-1])
            return MicSample(bool(apps), tuple(apps))
        except Exception as e:
            logger.debug("WindowsMicProbe failed: %s", e)
            return MicSample(None)


def _conferencing_process_running() -> bool:
    """psutil scan for a running conferencing-capable process (macOS gate,
    where the mic can't be attributed per app)."""
    import psutil

    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name") or ""
        except psutil.Error:
            continue
        if _matches_conferencing(name):
            return True
    return False


def create_mic_detector(config, hostname: str) -> Optional[MicActivityDetector]:
    """Build a mic detector from config, or None when disabled or unsupported.

    Mic credit is WINDOWS-ONLY. macOS and Linux return None — see the platform
    gate below for why macOS is off (over-billing via CoreAudio's device-level
    signal + process-scan gate)."""
    cd = config.call_detection
    if not (getattr(cd, "enabled", False) and getattr(cd, "mic_signal", False)):
        return None

    if sys.platform != "win32":
        # macOS (and every non-win32 platform) is intentionally OFF. On macOS
        # the only available signals over-credit: CoreAudio's
        # kAudioDevicePropertyDeviceIsRunningSomewhere is DEVICE-level, so
        # playback through a combined input+output device (AirPods, most USB/BT
        # headsets) marks the *input* device "running" even with no capture;
        # and the conferencing gate can only fall back to a process-existence
        # scan (no per-app mic attribution on macOS), which nearly any running
        # browser/Slack process satisfies. Together those bill idle media
        # playback as active work. Windows attributes the mic per app and is
        # safe. Do NOT re-enable macOS via per-process CoreAudio tap APIs
        # (macOS 14+): taps attribute the mic by reaching into the audio stream,
        # and mic detection is usage-only by policy (no audio path) — decision
        # 2026-07-17. macOS meeting credit stays on the window-title
        # CallDetector. MacosMicProbe / _conferencing_process_running are kept
        # as usage-only reference. See workspace memory
        # macos-mic-credit-overbilling-killswitch.
        return None

    return MicActivityDetector(
        hostname=hostname,
        probe=WindowsMicProbe(),
        conferencing_gate=None,
        min_session_seconds=cd.min_call_duration,
        max_credit_seconds=cd.max_credit_minutes * 60,
    )
