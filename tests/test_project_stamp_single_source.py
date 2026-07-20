"""Callsite guard: the project DECISION is single-sourced.

The active project is attached to outgoing events in several places (window,
status-span, call, mic, synthetic-active-AFK inside SyncEngine; plus the
salvaged/in-process AFK events built by AfkSource). Historically each SyncEngine
site read `self._current_project` under the lock and wrote `project_id` inline,
so the copies could drift — the same meeting upserting a projected row next to
an unprojected one (one-rule-one-implementation, the #1 audit defect class).

The thing that must be single-sourced is the DECISION "what is the active
project" — `_current_project_id()`. The dict WRITE itself legitimately happens
in two modules: `_stamp_project` (SyncEngine-built events) and
`AfkSource._event()` (AFK events, which receive the id as a parameter, NOT by
reading `_current_project` themselves). These tests enforce exactly that: one
locked read of `_current_project` in SyncEngine, one write per module, and no
AfkSource read of `_current_project` (it must stay parameter-fed).
"""

from pathlib import Path
from unittest.mock import Mock

import src.sync.afk_source as afk_mod
import src.sync.sync_engine as mod
from src.config import Config
from src.sync.sync_engine import SyncEngine

_SOURCE = Path(mod.__file__).read_text()
_AFK_SOURCE = Path(afk_mod.__file__).read_text()


def test_current_project_is_read_in_exactly_one_place():
    # The locked read `project = self._current_project` must live only in
    # `_current_project_id`; every consumer calls that accessor.
    assert _SOURCE.count("project = self._current_project") == 1


def test_project_id_is_written_onto_events_in_exactly_one_place():
    # The dict write `x["project_id"] = ...` must live only in `_stamp_project`.
    assert _SOURCE.count('["project_id"] = ') == 1


def test_no_inline_conditional_project_id_arg():
    # The old constructor-arg shape `project_id=project["id"] if project else
    # None` must be gone — those sites call `_current_project_id()` now.
    assert 'project_id=project["id"] if project else None' not in _SOURCE


def test_afk_source_writes_project_id_once_and_never_reads_current_project():
    # AfkSource is the OTHER module that writes project_id (the AFK path named
    # in _stamp_project's docstring). It must write it exactly once and only
    # from its passed-in parameter — never by reading _current_project itself,
    # which would re-introduce a second decision source and defeat the dedup.
    assert _AFK_SOURCE.count('["project_id"] = ') == 1
    assert "_current_project" not in _AFK_SOURCE


def _engine() -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=Mock(),
        config=Config(),
        time_tracker=Mock(),
    )


def test_project_stamp_accepts_integer_project_ids():
    engine = _engine()
    engine.set_current_project({"id": "42", "name": "Project"})

    event = engine._stamp_project({"id": "evt"})

    assert event["project_id"] == 42


def test_project_stamp_omits_invalid_project_ids():
    engine = _engine()
    engine.set_current_project({"id": "4152903c-0894-48ed-ad50-491f97f52a46"})

    event = engine._stamp_project({"id": "evt"})

    assert "project_id" not in event
