"""The server's retention window must have exactly one definition.

``stale_after_days=7`` was written four times — three defaults in
``src/sync/queue.py`` (``failed_event_summary``, ``evict_unstorable``,
``requeue_storable_dead_letter``) and a bare ``timedelta(days=7)`` in
``SyncEngine._batch_has_storable_activity``.

Now that evict and replay are a PAIRED loop, a drift between two of those is
self-sustaining rather than merely wrong: widen one and eviction drops a row
that the replay resurrects every cooldown, with ``dropped_at`` restamped on each
pass so the cooldown never terminates it. The queue already set the precedent
for exactly this reason with ``MAX_EVENT_DURATION_SECONDS``.

These are guards, not feature tests, so per test-fixture-discipline.md Phantom 5
each was watched failing under mutation before being committed — which is how
the vacuous first version of the first one was caught (see its docstring).
"""

import inspect

from src.sync import queue as queue_mod
from src.sync import sync_engine as sync_engine_mod
from src.sync.queue import EVENT_RETENTION_DAYS, OfflineQueue

#: Every function whose ``stale_after_days`` default defines the window.
_RETENTION_DEFAULT_OWNERS = (
    OfflineQueue.failed_event_summary,
    OfflineQueue.evict_unstorable,
    OfflineQueue.requeue_storable_dead_letter,
)

_EXPECTED_DECLARATION = "stale_after_days: int = EVENT_RETENTION_DAYS,"


def _code_only(source: str) -> str:
    """Source with comment lines removed.

    These guards scan for a literal that a COMMENT explaining the guard would
    also contain — a detector poisoned by its own corpus
    (diagnosis-discipline.md Rule 9). One fired on its first run against a
    comment saying not to hardcode the number. A comment can only cause a false
    FAIL here, never a false pass, but a guard that false-fails is a guard
    someone loosens, so measure code.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )


def test_every_stale_after_days_default_is_the_shared_constant():
    """Read the SOURCE, not the resolved default.

    ``inspect.signature(...).default is EVENT_RETENTION_DAYS`` looks like the
    right assertion and discriminates nothing: the constant is 7, CPython
    interns small ints, so a hardcoded ``= 7`` satisfies both ``==`` and ``is``.
    Watched failing under mutation, that version stayed GREEN with a literal
    restored — a guard that guards nothing (Phantom 5). Only the text of the
    declaration can tell a shared constant from a copied literal.
    """
    for fn in _RETENTION_DEFAULT_OWNERS:
        # Sanity: the resolved value must still agree, or the guards disagree
        # with each other about what they are pinning.
        assert inspect.signature(fn).parameters["stale_after_days"].default == (
            EVENT_RETENTION_DAYS
        )
        declarations = [
            line.strip()
            for line in _code_only(inspect.getsource(fn)).splitlines()
            if "stale_after_days" in line and ":" in line and "=" in line
        ]
        assert declarations == [_EXPECTED_DECLARATION], (
            f"{fn.__qualname__} declares its retention window as {declarations!r} "
            "instead of the shared constant; evict and replay are a paired loop, "
            "so a drift here makes a row get dropped and resurrected every "
            "cooldown, forever"
        )


def test_batch_storability_check_derives_its_window_from_the_constant():
    """The engine's copy is a bare ``timedelta(days=...)``, so it has no default
    to inspect — read its source instead. It must name the constant and must not
    hardcode the number."""
    src = _code_only(
        inspect.getsource(sync_engine_mod.SyncEngine._batch_has_storable_activity)
    )
    assert "EVENT_RETENTION_DAYS" in src, (
        "_batch_has_storable_activity decides whether a stuck queue head holds "
        "real activity; if its window drifts from the queue's, the two disagree "
        "about the same batch"
    )
    assert "days=7" not in src


def test_no_stray_retention_literal_survives_in_the_queue_module():
    """Callsite guard over the WHOLE module, so a new method that copies the old
    default instead of importing the constant is caught even though it is not in
    the list above."""
    offenders = [
        line.strip()
        for line in _code_only(inspect.getsource(queue_mod)).splitlines()
        if "stale_after_days" in line and ": int = 7" in line
    ]
    assert not offenders, f"retention literal re-introduced: {offenders}"


def test_constant_is_exported():
    """It is imported across modules, so it belongs in ``__all__`` — otherwise
    the next author re-derives it locally."""
    assert "EVENT_RETENTION_DAYS" in queue_mod.__all__
