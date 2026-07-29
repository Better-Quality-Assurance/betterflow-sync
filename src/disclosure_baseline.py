"""The declared baseline of what this agent collects — the code side of the
monitoring disclosure employees sign.

===========================================================================
LEGAL COUNTERPART: BetterQA Regulament Intern, art. 68^1 alin. (6)
                   ("Monitorizarea activitatii la statia de lucru")
CHANGES HERE REQUIRE A DISCLOSURE REVIEW: dpo@betterqa.co
===========================================================================

Read this first if you are here because a test failed.

Several collection behaviours in this agent are **server-flippable at
runtime**. Nothing about that is visible in a release: a row changes in the
backend, the next `/config` poll applies it, and the document every employee
signed becomes false without a build, a changelog entry, or anybody being
re-informed. That is not hypothetical — it is how `privacy.exclude_apps`
came to have no server handler at all while `sync.in_process_window`, which
bypassed the exclusion list entirely, could be switched on from a database
row with no deferral gate (see PR #159).

`tests/test_disclosure_baseline.py` compares the agent's *effective*
collection configuration against what is declared below and fails on any
divergence. It is deliberately a **baseline comparison, not a denylist of
forbidden settings**. A denylist only catches the flags somebody already
thought of; it goes stale the day a new field is added, which is exactly the
failure this guard exists to prevent. Comparing against a declared baseline
catches anything new *by construction* — including a field added next year by
someone who has never read this file.

Three kinds of divergence fail:

1. **A watched value changed.** `track_display_info` flipped to True, an app
   dropped out of `exclude_apps`, and so on.
2. **A watched setting became reachable from the server, or its gate
   changed.** A field moving from the deferred `privacy` block into the
   ungated `sync` block changes nothing about the shipped default and
   everything about who can change it, silently, tomorrow.
3. **A new setting appeared that nobody has classified.** This is the one
   that makes the guard outlive its author. Every field of every watched
   settings group must be classified DISCLOSED or INTERNAL here. A field in
   neither is a failure, and the fix is one line plus a decision about
   whether employees need to be told.

Classifying a field INTERNAL is a legitimate, cheap act for a knob that is
genuinely about billing or transport — it keeps the guard from crying wolf,
which is how guards get deleted. Reclassifying a DISCLOSED field as INTERNAL
to silence a failure is not; it is the same act as deleting the test, and it
is reviewable precisely because it happens in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------
# Where the legal text lives, and who owns it. The failure message quotes
# these, so a developer who trips the guard is told what to update and whom
# to tell without having to find this file first.
# --------------------------------------------------------------------------

REGULAMENT_ARTICLE = "Regulament Intern, art. 68^1 alin. (6)"
REGULAMENT_TITLE = "Monitorizarea activitatii la statia de lucru"
DISCLOSURE_CONTACT = "dpo@betterqa.co"

# --------------------------------------------------------------------------
# Classification of a settings field.
# --------------------------------------------------------------------------

#: The field governs WHAT is collected or WHETHER it leaves the device. It is
#: described, directly or in substance, in the Regulament article above. Its
#: value and its server reachability are both pinned.
DISCLOSED = "disclosed"

#: The field does not change what is collected — it tunes billing, transport,
#: or presentation. Not pinned, but it must still be listed, so that a NEW
#: field cannot slip in unclassified.
INTERNAL = "internal"

# --------------------------------------------------------------------------
# Server reachability, as computed from `Config.update_from_server`.
#
# These are not declarations of intent; they are what a parse of the real
# function reports. Declaring one wrongly fails the guard.
# --------------------------------------------------------------------------

#: Not assignable from a server `/config` payload at all. Changing it requires
#: shipping a build — which means a release, and a chance to notice.
SERVER_NONE: tuple[str, ...] = ()

#: Assignable only inside a `not DEFER_UNAPPLIED_SERVER_SETTINGS` branch. The
#: gate is True on main, so these are inert today and cannot change behaviour
#: until the gate is lifted in a deliberate release.
GATED = "gated"

#: Assignable with NO deferral gate. A backend row changes it on the next
#: poll, on the whole fleet, with no release. This is the highest-risk
#: classification and every DISCLOSED field carrying it needs the Regulament
#: to be honest about the fact that it can change without notice.
LIVE = "live"

#: Assignable only in the deferral-EXEMPT branch, which accepts the disabling
#: direction only (currently `call_detection.mic_signal=false`). A remote
#: off-switch for a probe is a privacy improvement and is allowed to bypass
#: the staged rollout; it can never turn collection ON.
KILL_ONLY = "kill-only"


_UNSET = object()


@dataclass(frozen=True)
class Declared:
    """One field of one watched settings group, as declared to employees."""

    kind: str
    #: For DISCLOSED: what the employee was told, in the terms they were told
    #: it. For INTERNAL: why this field does not change what is collected.
    why: str
    #: DISCLOSED only — the shipped default, pinned exactly.
    value: Any = _UNSET
    #: DISCLOSED only — the sorted reachability tuple the AST probe must
    #: report for this field. `SERVER_NONE` means "not remotely settable".
    server: tuple[str, ...] = SERVER_NONE

    def __post_init__(self) -> None:
        if self.kind not in (DISCLOSED, INTERNAL):
            raise ValueError(f"unknown classification {self.kind!r}")
        if self.kind == DISCLOSED and self.value is _UNSET:
            raise ValueError("a DISCLOSED field must pin its value")
        if not self.why:
            raise ValueError("every field must carry a reason")


def is_pinned(d: Declared) -> bool:
    """True when this declaration pins a value (i.e. it is DISCLOSED)."""
    return d.kind == DISCLOSED and d.value is not _UNSET


# ==========================================================================
# THE BASELINE
#
# Keyed by the attribute name on `Config`. Every field of every group listed
# here must appear; the guard fails on a field that is present in the
# dataclass and missing here, and on one present here and missing from the
# dataclass.
# ==========================================================================

BASELINE: dict[str, dict[str, Declared]] = {
    # ----------------------------------------------------------------------
    # PrivacySettings — the group that exists to decide what leaves the
    # device. Every field here is collection-relevant unless it is dead code.
    # ----------------------------------------------------------------------
    "privacy": {
        "domain_only_urls": Declared(
            DISCLOSED,
            value=True,
            server=(GATED,),
            why=(
                "Employees are told browsing is recorded as the SITE, not the "
                "page: URLs are cut to their domain on the device before "
                "anything is sent. Flipping this to False sends full URLs, "
                "including query strings, which routinely carry document "
                "names, search terms and record identifiers."
            ),
        ),
        "collect_full_urls": Declared(
            DISCLOSED,
            value=False,
            server=(GATED,),
            why=(
                "The opt-in that overrides domain-only stripping. It is the "
                "same disclosure as domain_only_urls approached from the "
                "other side, and it must stay off for the sentence about "
                "domains to remain true."
            ),
        ),
        "exclude_apps": Declared(
            DISCLOSED,
            value=[
                "1Password",
                "Keychain Access",
                "System Preferences",
                "System Settings",
                # macOS system agents
                "SecurityAgent",
                "UserNotificationCenter",
                "loginwindow",
                "ScreenSaverEngine",
                "CoreServicesUIAgent",
                "AirPlayUIAgent",
                "SystemUIServer",
                # Auto-updaters
                "Microsoft AutoUpdate",
                "Software Update",
            ],
            server=(GATED,),
            why=(
                "The named promise: activity in these applications never "
                "leaves the device. Password managers and the OS credential "
                "and security prompts are the whole point. REMOVING an entry "
                "narrows a guarantee that was made by name — that is a "
                "disclosure change even though nothing about the mechanism "
                "moved. Adding one only ever sends less."
            ),
        ),
        "track_display_info": Declared(
            DISCLOSED,
            value=False,
            server=(GATED,),
            why=(
                "Records which monitor and which virtual desktop the active "
                "window was on. That is workstation-layout information about "
                "the person, it is disclosed nowhere today, and it is off. "
                "Turning it on requires a Regulament change first."
            ),
        ),
        "track_browser_urls": Declared(
            DISCLOSED,
            value=False,
            server=(GATED,),
            why=(
                "Reads the active browser tab's address without an extension "
                "(AppleScript on macOS, UI Automation on Windows). It is a "
                "second, independent route to browsing data that does not go "
                "through the browser at all, and it is off."
            ),
        ),
        "collect_page_category": Declared(
            DISCLOSED,
            value=True,
            server=(GATED,),
            why=(
                "Attaches a coarse category to browsing activity. Employees "
                "are told activity is categorised; this is the browsing half "
                "of that sentence."
            ),
        ),
        "auto_categorize": Declared(
            DISCLOSED,
            value=True,
            server=(GATED,),
            why=(
                "Attaches an app category from server-side mappings. The "
                "application half of the same sentence."
            ),
        ),
        "default_categories": Declared(
            INTERNAL,
            why=(
                "A local app-name to category lookup used for labelling. It "
                "changes how already-collected activity is described, never "
                "what is collected or sent."
            ),
        ),
    },
    # ----------------------------------------------------------------------
    # SyncSettings — mostly transport, but it carries the flags that decide
    # WHICH SOURCE produces activity data. Those are collection decisions
    # wearing a transport group's name, and they are the ungated ones.
    # ----------------------------------------------------------------------
    "sync": {
        "in_process_window": Declared(
            DISCLOSED,
            value=False,
            server=(LIVE,),
            why=(
                "Captures the active window (application and title) inside "
                "the agent itself rather than via the external ActivityWatch "
                "watcher. It is a distinct capture mechanism, it is remotely "
                "switchable with NO deferral gate, and until PR #159 lands "
                "its output did not pass the excluded-apps filter at all. "
                "Any change to its default or its gate is a disclosure event."
            ),
        ),
        "in_process_input": Declared(
            DISCLOSED,
            value=False,
            server=(LIVE,),
            why=(
                "Counts keystrokes, clicks and scrolls using OS-level input "
                "hooks in this process. It records counts, never characters, "
                "but employees are told about keyboard and mouse ACTIVITY "
                "measurement and this is the mechanism. Remotely switchable "
                "with no deferral gate."
            ),
        ),
        "in_process_afk": Declared(
            DISCLOSED,
            value=True,
            server=SERVER_NONE,
            why=(
                "Derives the away-from-keyboard stream in-process from the OS "
                "idle clock. This is what decides whether a period counts as "
                "worked, which is the part of the disclosure employees care "
                "about most."
            ),
        ),
        "stop_external_afk_tracker": Declared(
            DISCLOSED,
            value=False,
            server=SERVER_NONE,
            why=(
                "Selects which process produces the AFK stream. It does not "
                "change what is measured, but it changes which component is "
                "running on the machine, and the disclosure names the "
                "bundled ActivityWatch trackers."
            ),
        ),
        "min_window_event_seconds": Declared(
            DISCLOSED,
            value=5.0,
            server=(LIVE,),
            why=(
                "The granularity floor: window and browsing events shorter "
                "than this are discarded on the device and never sent. "
                "Lowering it to 0 turns a coarse record of what someone "
                "worked on into a near-keystroke-resolution log of every "
                "window they glanced at. Remotely settable with no deferral "
                "gate, which is why it is pinned here."
            ),
        ),
        "interval_seconds": Declared(
            INTERNAL,
            why="How often already-collected events are uploaded. Transport cadence.",
        ),
        "batch_size": Declared(
            INTERNAL,
            why="How many events go in one upload request. Transport shape.",
        ),
        "compress": Declared(
            INTERNAL,
            why="gzip on the upload body. Encoding, not content.",
        ),
        "idle_pause_minutes": Declared(
            INTERNAL,
            why=(
                "Pauses the SYNC LOOP after this long AFK. It stops uploads "
                "of an idle machine; it does not change what the trackers "
                "record while it is paused."
            ),
        ),
    },
    # ----------------------------------------------------------------------
    # CallDetectionSettings — the microphone probe. The only sensor-shaped
    # thing in the agent, and the one with the sharpest disclosure edge.
    # ----------------------------------------------------------------------
    "call_detection": {
        "enabled": Declared(
            DISCLOSED,
            value=True,
            server=(GATED,),
            why=(
                "Master switch for meeting detection. Employees are told that "
                "time in calls is recognised as work; this is what does it."
            ),
        ),
        "mic_signal": Declared(
            DISCLOSED,
            value=True,
            server=(GATED, KILL_ONLY),
            why=(
                "Whether the agent asks the OS if the MICROPHONE IS IN USE. "
                "It reads a boolean device state and never audio, but 'the "
                "employer's software checks my microphone' is exactly the "
                "sentence that must appear in the disclosure and be true. The "
                "kill-only reachability is deliberate and must be preserved: "
                "the probe can be switched OFF fleet-wide without waiting for "
                "a release, and can never be switched ON that way."
            ),
        ),
        "min_call_duration": Declared(
            INTERNAL,
            why=(
                "Discards call sessions shorter than this so an accidental "
                "app launch is not billed. Billing threshold on data already "
                "observed."
            ),
        ),
        "max_credit_minutes": Declared(
            INTERNAL,
            why=(
                "Caps how much active time a single detected call may credit. "
                "Purely a billing bound."
            ),
        ),
    },
    # ----------------------------------------------------------------------
    # ForegroundActivitySettings — infers activity from CPU use of the
    # frontmost app when there is no input. Default off for billing-safety
    # reasons, but it is also an inference about the person.
    # ----------------------------------------------------------------------
    "foreground_activity": {
        "enabled": Declared(
            DISCLOSED,
            value=False,
            server=(GATED,),
            why=(
                "Credits activity from the frontmost application's CPU use "
                "when no keyboard or mouse input is present — i.e. it infers "
                "'working' without any input evidence. Off, and the "
                "disclosure does not claim this inference is made."
            ),
        ),
        "cpu_threshold_percent": Declared(
            INTERNAL,
            why="Sensitivity of an inference that is switched off. Tuning.",
        ),
        "max_credit_minutes": Declared(
            INTERNAL,
            why="Billing bound on the same switched-off inference.",
        ),
        "min_session_seconds": Declared(
            INTERNAL,
            why="Blip filter on the same switched-off inference.",
        ),
    },
    # ----------------------------------------------------------------------
    # AWSettings — the local ActivityWatch endpoint plus the idle threshold.
    # ----------------------------------------------------------------------
    "aw": {
        "afk_timeout_minutes": Declared(
            DISCLOSED,
            value=10,
            server=(GATED,),
            why=(
                "How long without input before the employee is recorded as "
                "away. This is the definition of 'idle' in the document and "
                "in every timesheet that follows from it."
            ),
        ),
        "host": Declared(
            INTERNAL,
            why="Loopback address of the local ActivityWatch server. Transport.",
        ),
        "port": Declared(
            INTERNAL,
            why="Local ActivityWatch port. Transport.",
        ),
    },
}


# ==========================================================================
# Settings groups that exist on `Config`.
#
# The groups above are watched field-by-field. This is the outer inventory:
# it fails when a NEW settings group appears on `Config` — the case where
# somebody adds `screenshots: ScreenshotSettings` and no per-field check
# would ever have looked at it, because the group itself is new.
# ==========================================================================

#: Config attributes that are watched field-by-field above.
WATCHED_GROUPS: frozenset[str] = frozenset(BASELINE)

#: Config attributes deliberately NOT watched, each with a reason. Anything
#: on Config that is in neither set fails the inventory check.
UNWATCHED_CONFIG_FIELDS: dict[str, str] = {
    "api_url": "Where events are sent, not what is collected.",
    "device_id": "Locally generated install identifier, disclosed separately.",
    "reminders": "Break and private-time prompts shown TO the user.",
    "working_hours": (
        "Restricts collection to a schedule — it can only ever cause LESS "
        "to be recorded, and it is a per-employee contractual matter handled "
        "outside the agent."
    ),
    "engagement": "Thresholds for scoring activity already collected.",
    "fraud_detection": "Thresholds for scoring activity already collected.",
    "privacy_notice_ack_version": (
        "Records WHICH disclosure version this user acknowledged. Compliance "
        "record that the notice was shown, not a collection setting — it "
        "changes nothing about what the agent gathers."
    ),
    "privacy_notice_ack_at": (
        "When the acknowledgement happened. Compliance state, not collection."
    ),
    "setup_complete": "First-run wizard state.",
    "auto_start": "Launch at login.",
    "check_updates": "Updater behaviour.",
    "auto_install_updates": "Updater behaviour.",
    "update_channel": "Updater behaviour.",
    "debug_mode": "Local log verbosity.",
}


# ==========================================================================
# Collection facts that are not settings.
#
# Some of what we disclose is not a flag anyone can flip; it is simply what
# the code does. Those facts still go stale, and a signed document does not
# distinguish 'we changed a setting' from 'we changed the code'.
# ==========================================================================

#: The complete list of health/telemetry keys the heartbeat is allowed to
#: forward. `BetterFlowClient.heartbeat` filters against this tuple, so a key
#: absent from it never leaves the machine — which makes it the enforced
#: egress allowlist, and therefore a disclosure surface. A new entry here is
#: a new thing reported about the employee's machine.
HEARTBEAT_HEALTH_KEYS: tuple[str, ...] = (
    "idle_tracker_stale_restarts",
    "idle_tracker_blind",
    "inproc_afk",
    "afk_event_age_seconds",
    "window_event_age_seconds",
    "consecutive_sync_failures",
    "idle_while_active_detections",
    "sync_stale_seconds",
    "window_titles_captured_recently",
    "hardware_serial",
    # The disclosure acknowledgement record (version + timestamp). It
    # forwards proof the notice was shown; it is compliance evidence, not
    # a new fact gathered about the machine or the person.
    "disclosure_acknowledgement",
    # Two booleans about the AGENT'S OWN health, not about the person: whether
    # the tracker binaries failed to install, and whether this process has any
    # managed watchers of its own. Neither describes what the employee did —
    # they describe whether this machine is capable of recording anything at
    # all. They join the operational-health keys this allowlist already carries
    # (restart counts, blind flags, event ages, sync staleness) — no NOTICE
    # bullet names any of those individually; the notice's technical-details
    # bullet covers only the agent version and the time zone. So this adds no
    # new CATEGORY, but do not read it as "the notice already lists it": it
    # does not. Both flags were already computed and simply filtered out before
    # egress, which is why a device that recorded zero for two consecutive days
    # still reported itself healthy.
    #
    # Adding a field that reports LESS capture, and cannot distinguish one
    # person's activity from another's, does not widen what is collected about
    # anyone. If that reading is ever contested, the safe answer is to keep the
    # field and update the notice text, not to drop it and go back to blind.
    "tracker_download_failed",
    "managed_components_unavailable",
    # The window watcher has stayed blind across repeated restarts. Exactly the
    # same category as idle_tracker_blind, which this list already carries: a
    # fact about whether a watcher on this machine is working, never about what
    # the person did.
    "window_tracker_blind",
    # How many events this device captured but failed to DELIVER — they
    # exhausted their retries and were preserved locally rather than deleted.
    # A single integer, and only ever a count: it carries no event, no bucket,
    # no timestamp, no window title, and nothing that distinguishes one
    # person's activity from another's. It says "this machine is holding N
    # undelivered records", which is the delivery-health counterpart of
    # consecutive_sync_failures and sync_stale_seconds, both already here.
    #
    # It reports a delivery FAILURE, so it can only ever describe the agent
    # doing LESS than the notice says, never more. It was already computed on
    # every heartbeat and filtered out at this gate, which is why no device has
    # ever reported a backlog. Same reading as tracker_download_failed above:
    # if the category is ever contested, the safe answer is to keep the field
    # and update the notice text, not to drop it and go back to blind.
    "dead_letter_count",
)

#: The machine's hostname is read once and embedded in every `bucket_id`, so
#: it rides along on every event whether or not anyone considered it an
#: identifier. On personal-name-configured Macs ("Ana's MacBook Pro") it is
#: an identifier for the person, not the device. Pinned as a fact so that
#: changing it — in either direction — is a visible act.
HOSTNAME_IS_EMBEDDED_IN_BUCKET_IDS = True
