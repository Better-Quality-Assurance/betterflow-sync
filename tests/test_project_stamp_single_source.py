"""Callsite guard: project stamping is single-sourced.

The active project is attached to outgoing events in several places (window,
status-span, call, mic, synthetic-AFK, salvaged-AFK). Historically each read
`self._current_project` under the lock and wrote `project_id` inline, so the
copies could drift — the same meeting upserting a projected row next to an
unprojected one (one-rule-one-implementation, the #1 audit defect class).

These tests fail if any call site re-rolls the rule inline instead of going
through `_current_project_id()` / `_stamp_project()`. They fail against the
pre-dedup code (7 locked reads, 5 inline writes) and pass once every path
routes through the two helpers.
"""

from pathlib import Path

import src.sync.sync_engine as mod

_SOURCE = Path(mod.__file__).read_text()


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
