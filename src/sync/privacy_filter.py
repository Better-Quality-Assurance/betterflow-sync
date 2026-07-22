"""Egress-boundary privacy filter for excluded applications.

The product guarantee — the one written into the Regulament Intern that every
employee signs — is that events belonging to an excluded app (password
managers, Keychain, System Settings, ...) NEVER leave the device.

Historically that guarantee was enforced in ``SyncEngine._transform_event``,
which only sees events read out of external ActivityWatch buckets. Two later
event producers (the in-process window source and the in-process input source)
build BetterFlow-shaped events directly and were appended to the upload list
without ever passing through it, so enabling either one made the guarantee
false. Patching each producer would have left the same trap for the third one.

So the filter lives here, as a pure function, and is applied at the single
place where events are serialised onto the wire
(``BetterFlowClient.send_events``). Every producer — external buckets,
in-process sources, status spans, call detector, the offline-queue drain, and
anything added later — passes through it, because they all end up in that one
HTTP call.

Matching is deliberately identical to the original ``_transform_event`` check
(exact, case-sensitive membership on ``data.app``): this closes a bypass, it
does not change what "excluded" means.
"""

from typing import Iterable, Optional


def event_app(event: dict) -> Optional[str]:
    """The app an event is attributed to, or None.

    Every producer in this codebase writes the app name to ``data.app``
    (``SyncEngine._populate_window_data``, ``WindowSource._event``,
    ``InputSource.drain_input_event``, ``SyncEngine._make_call_bf_event``).
    Tolerant of a malformed event: anything that isn't a dict-with-a-dict-data
    simply has no app, and is therefore not excluded.
    """
    if not isinstance(event, dict):
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    app = data.get("app")
    return app if isinstance(app, str) else None


def partition_excluded(
    events: Iterable[dict], exclude_apps: Iterable[str]
) -> tuple[list[dict], list[dict]]:
    """Split ``events`` into ``(kept, dropped)`` by the exclusion list.

    ``dropped`` events must never be transmitted, queued for a later attempt,
    or retried — exclusion is a permanent, local decision, not a delivery
    failure.
    """
    events = list(events)
    excluded = {a for a in exclude_apps if isinstance(a, str) and a}
    if not excluded:
        return events, []

    kept: list[dict] = []
    dropped: list[dict] = []
    for event in events:
        app = event_app(event)
        (dropped if (app and app in excluded) else kept).append(event)
    return kept, dropped
