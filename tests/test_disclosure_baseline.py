"""The agent's effective collection configuration must match what employees
were told it collects.

This is the enforcement half of `src/disclosure_baseline.py`. Read that file
first — it explains why this is a baseline comparison and not a denylist, and
it is where the fix for any failure below goes.

Four things are compared, in the order a reader should think about them:

* `test_no_unclassified_settings_group` — a NEW settings group on `Config`.
* `test_no_unclassified_setting` — a NEW field in a watched group.
* `test_declared_values_still_hold` — a watched default changed.
* `test_declared_server_reachability_still_holds` — a watched field became
  remotely flippable, or its deferral gate changed.

Plus two non-setting collection facts: the heartbeat's egress allowlist and
the hostname embedded in every bucket id.

Nothing here constructs a tray, a client or a sync engine. It reads dataclass
fields and parses source, so it runs identically on a headless CI runner and
cannot be quietly skipped.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from src import config as config_module
from src.config import Config
from src.disclosure_baseline import (
    BASELINE,
    DISCLOSED,
    DISCLOSURE_CONTACT,
    GATED,
    HEARTBEAT_HEALTH_KEYS,
    HOSTNAME_IS_EMBEDDED_IN_BUCKET_IDS,
    KILL_ONLY,
    LIVE,
    REGULAMENT_ARTICLE,
    REGULAMENT_TITLE,
    UNWATCHED_CONFIG_FIELDS,
    WATCHED_GROUPS,
    is_pinned,
)

_SRC = Path(__file__).resolve().parents[1] / "src"

_DEFERRAL_GATE = "DEFER_UNAPPLIED_SERVER_SETTINGS"


# ---------------------------------------------------------------------------
# Failure message
#
# A guard that says "AssertionError: False is not True" teaches people to
# delete it. Every failure below names the document that just became wrong,
# the person to tell, and the specific edit that resolves it.
# ---------------------------------------------------------------------------


def _fail(headline: str, detail: str, fix: str) -> str:
    return (
        f"\n\n  {headline}\n\n"
        f"{detail}\n"
        f"  This changes what BetterQA told employees it collects, in the\n"
        f"  monitoring disclosure they each signed:\n\n"
        f"      {REGULAMENT_ARTICLE}\n"
        f"      \"{REGULAMENT_TITLE}\"\n\n"
        f"  Tell {DISCLOSURE_CONTACT} BEFORE merging. The Regulament article\n"
        f"  is a signed document; if the agent's behaviour moves and the text\n"
        f"  does not, the signature covers something that is no longer true.\n\n"
        f"  To resolve:\n{fix}\n"
        f"  The declarations live in src/disclosure_baseline.py.\n"
    )


# ---------------------------------------------------------------------------
# Server-reachability probe
#
# Parses `Config.update_from_server` and reports, for every `self.<group>.<field>`
# assignment, the deferral gate of the branch it sits in. This is a measurement
# of the real function, not a restatement of intent: it is what caught that
# `sync.in_process_window` is remotely flippable with no gate while every
# `privacy.*` field is deferred.
# ---------------------------------------------------------------------------


def _gate_of(tests: list[ast.expr]) -> str:
    """Classify the deferral gate implied by a stack of enclosing `if` tests."""
    negated = False
    plain = False
    for test in tests:
        for node in ast.walk(test):
            if (
                isinstance(node, ast.UnaryOp)
                and isinstance(node.op, ast.Not)
                and any(
                    isinstance(n, ast.Name) and n.id == _DEFERRAL_GATE
                    for n in ast.walk(node.operand)
                )
            ):
                negated = True
            if isinstance(node, ast.Name) and node.id == _DEFERRAL_GATE:
                plain = True
    if negated:
        return GATED
    if plain:
        return KILL_ONLY
    return LIVE


def _collect(node: ast.AST, tests: list[ast.expr], out: dict) -> None:
    if isinstance(node, ast.If):
        for stmt in node.body:
            _collect(stmt, tests + [node.test], out)
        for stmt in node.orelse:
            _collect(stmt, tests, out)
        return

    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Attribute)
                and isinstance(target.value.value, ast.Name)
                and target.value.value.id == "self"
                and target.value.attr in WATCHED_GROUPS
            ):
                key = (target.value.attr, target.attr)
                out.setdefault(key, set()).add(_gate_of(tests))

    for child in ast.iter_child_nodes(node):
        _collect(child, tests, out)


def _server_reachability() -> dict[tuple[str, str], tuple[str, ...]]:
    """Map (group, field) -> sorted gates it can be assigned under."""
    tree = ast.parse(inspect.getsource(config_module))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "update_from_server"
    )
    out: dict[tuple[str, str], set[str]] = {}
    for stmt in fn.body:
        _collect(stmt, [], out)
    return {key: tuple(sorted(gates)) for key, gates in out.items()}


def _normalise(value):
    """Compare lists of strings as unordered — a reorder of `exclude_apps` is
    not a change in what is collected, and a guard that fires on it gets
    deleted for crying wolf."""
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return sorted(value)
    return value


# ---------------------------------------------------------------------------
# Sanity: the probe itself must work.
#
# A broken probe does not raise, it returns a plausible finding. If this test
# ever fails, every reachability result below is void — do not "fix" the
# baseline to match it.
# ---------------------------------------------------------------------------


def test_the_reachability_probe_actually_measures_something():
    reach = _server_reachability()
    assert reach, "the probe found no server-assignable settings at all"
    # Negative control: a gate classification the probe must be able to tell
    # apart. If everything came back identical the probe measures nothing.
    gates = {g for gates in reach.values() for g in gates}
    assert GATED in gates, "probe never reports a deferred assignment"
    assert LIVE in gates, "probe never reports an ungated assignment"
    assert len(gates) > 1, "probe returns one verdict for everything"


# ---------------------------------------------------------------------------
# 1. A new settings GROUP nobody classified.
# ---------------------------------------------------------------------------


def test_no_unclassified_settings_group():
    on_config = {f.name for f in dataclasses.fields(Config)}
    classified = set(WATCHED_GROUPS) | set(UNWATCHED_CONFIG_FIELDS)

    unclassified = sorted(on_config - classified)
    assert not unclassified, _fail(
        f"UNCLASSIFIED SETTINGS GROUP ON Config: {', '.join(unclassified)}",
        (
            "  A new group of settings exists on Config and nothing has decided\n"
            "  whether it affects what this agent collects.\n\n"
        ),
        (
            "    * If it changes what is collected: add it to BASELINE and\n"
            "      classify each of its fields.\n"
            "    * If it does not: add it to UNWATCHED_CONFIG_FIELDS with the\n"
            "      reason it is out of scope.\n"
        ),
    )

    vanished = sorted(classified - on_config)
    assert not vanished, _fail(
        f"BASELINE DESCRIBES SETTINGS GROUPS THAT NO LONGER EXIST: {', '.join(vanished)}",
        (
            "  The baseline is describing an agent that is not the one in this\n"
            "  tree, so it has stopped guarding whatever replaced these.\n\n"
        ),
        "    * Remove the stale entries from src/disclosure_baseline.py.\n",
    )


# ---------------------------------------------------------------------------
# 2. A new FIELD nobody classified.
#
# This is the property that makes the guard outlive its author: a setting
# added next year by someone who has never read the baseline still fails,
# because it is absent from a declaration rather than present in a denylist.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("group", sorted(BASELINE))
def test_no_unclassified_setting(group: str):
    declared = BASELINE[group]
    actual = {f.name for f in dataclasses.fields(getattr(Config(), group))}

    unclassified = sorted(actual - set(declared))
    assert not unclassified, _fail(
        f"UNCLASSIFIED SETTING(S) IN Config.{group}: {', '.join(unclassified)}",
        (
            f"  New field(s) appeared in Config.{group} and nothing has decided\n"
            "  whether they affect what this agent collects. That decision is\n"
            "  the point of this guard: the last disclosure list went stale\n"
            "  exactly this way, by a field being added and nobody noticing.\n\n"
        ),
        (
            "    * If it changes WHAT is collected, WHETHER it leaves the\n"
            "      device, or WHO can change that at runtime: declare it\n"
            "      DISCLOSED with its value and server reachability, and get\n"
            "      the Regulament article updated.\n"
            "    * If it is billing, transport or presentation tuning:\n"
            "      declare it INTERNAL with the reason. Cheap, and it keeps\n"
            "      this guard from crying wolf.\n"
        ),
    )

    vanished = sorted(set(declared) - actual)
    assert not vanished, _fail(
        f"BASELINE DESCRIBES SETTINGS THAT NO LONGER EXIST IN Config.{group}: "
        f"{', '.join(vanished)}",
        (
            "  A declared setting has been removed from the code. If it was\n"
            "  DISCLOSED, the Regulament may now describe a behaviour the\n"
            "  agent no longer has.\n\n"
        ),
        "    * Remove the stale entries from src/disclosure_baseline.py.\n",
    )


# ---------------------------------------------------------------------------
# 3. A declared value changed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("group", "name"),
    [
        (g, n)
        for g, fields in sorted(BASELINE.items())
        for n, d in sorted(fields.items())
        if is_pinned(d)
    ],
)
def test_declared_values_still_hold(group: str, name: str):
    declared = BASELINE[group][name]
    effective = getattr(getattr(Config(), group), name)

    assert _normalise(effective) == _normalise(declared.value), _fail(
        f"COLLECTION SETTING CHANGED: {group}.{name}",
        (
            f"    declared to employees : {declared.value!r}\n"
            f"    shipping in this build: {effective!r}\n\n"
            f"  What this setting governs:\n"
            f"    {declared.why}\n\n"
        ),
        (
            "    * If the new value is intended, update BASELINE and get the\n"
            "      Regulament article amended to match, in that order.\n"
            "    * If it is not intended, this guard just caught a silent\n"
            "      change to what BetterQA collects from its employees.\n"
        ),
    )


# ---------------------------------------------------------------------------
# 4. A declared field's server reachability changed.
#
# The subtlest and most important of the four. A field can keep its default
# forever and still turn a signed document into a lie, if somebody moves its
# handler out of the deferred block and into an ungated one. Nothing about
# the shipped build changes; everything about who can change it does.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("group", "name"),
    [
        (g, n)
        for g, fields in sorted(BASELINE.items())
        for n, d in sorted(fields.items())
        if d.kind == DISCLOSED
    ],
)
def test_declared_server_reachability_still_holds(group: str, name: str):
    declared = BASELINE[group][name]
    effective = _server_reachability().get((group, name), ())

    explain = {
        (): "not settable from the server at all (requires shipping a build)",
        (GATED,): (
            f"settable from the server, but only behind {_DEFERRAL_GATE} "
            "(inert until a deliberate release lifts the gate)"
        ),
        (LIVE,): (
            "settable from the server RIGHT NOW, with no deferral gate — a "
            "backend row changes it fleet-wide on the next poll, with no "
            "release and nobody informed"
        ),
        (KILL_ONLY,): "settable from the server in the DISABLING direction only",
        (GATED, KILL_ONLY): (
            f"deferred behind {_DEFERRAL_GATE} for enabling, with a "
            "deferral-exempt path for disabling only"
        ),
    }

    def describe(value: tuple[str, ...]) -> str:
        return explain.get(tuple(sorted(value)), f"gates {sorted(value)!r}")

    assert tuple(sorted(effective)) == tuple(sorted(declared.server)), _fail(
        f"WHO CAN CHANGE {group}.{name} AT RUNTIME HAS CHANGED",
        (
            f"    declared : {describe(declared.server)}\n"
            f"    actual   : {describe(effective)}\n\n"
            f"  What this setting governs:\n"
            f"    {declared.why}\n\n"
            "  The shipped default may be unchanged. That is not the point:\n"
            "  a collection behaviour that can be switched remotely is one the\n"
            "  disclosure must describe as switchable, or must not permit.\n\n"
        ),
        (
            "    * Moving a collection setting into an UNGATED server block\n"
            f"      needs {DISCLOSURE_CONTACT} to sign off first — that is a\n"
            "      change to what can happen without telling anyone.\n"
            "    * Moving one behind the deferral gate, or removing its\n"
            "      handler, is a tightening: update BASELINE and say so.\n"
        ),
    )


# ---------------------------------------------------------------------------
# 5. Collection facts that are not settings.
# ---------------------------------------------------------------------------


def test_heartbeat_egress_allowlist_still_holds():
    from src.sync.bf_client import BetterFlowClient

    effective = tuple(BetterFlowClient.HEARTBEAT_HEALTH_KEYS)
    added = sorted(set(effective) - set(HEARTBEAT_HEALTH_KEYS))
    removed = sorted(set(HEARTBEAT_HEALTH_KEYS) - set(effective))

    assert not added and not removed, _fail(
        "THE HEARTBEAT NOW REPORTS SOMETHING DIFFERENT ABOUT THE MACHINE",
        (
            f"    newly reported : {added or 'none'}\n"
            f"    no longer sent : {removed or 'none'}\n\n"
            "  HEARTBEAT_HEALTH_KEYS is the enforced egress allowlist for the\n"
            "  heartbeat — a key absent from it never leaves the device, so a\n"
            "  key added to it is a new fact reported about the employee's\n"
            "  machine, independent of any privacy setting. One of these keys\n"
            "  is already the hardware serial number.\n\n"
        ),
        (
            "    * Update HEARTBEAT_HEALTH_KEYS in src/disclosure_baseline.py\n"
            "      and check the new key against the Regulament's list of what\n"
            "      is reported. CLAUDE.md's device-identifier section is the\n"
            "      other place that must stay in step.\n"
        ),
    )


def test_hostname_is_still_embedded_in_every_bucket_id():
    source = (_SRC / "sync" / "sync_engine.py").read_text()

    reads_hostname = "self._hostname = socket.gethostname()" in source
    in_bucket_ids = 'bucket_id": f"' in source and "{self._hostname}" in source
    effective = reads_hostname and in_bucket_ids

    assert effective == HOSTNAME_IS_EMBEDDED_IN_BUCKET_IDS, _fail(
        "THE MACHINE HOSTNAME IS NO LONGER TRAVELLING ON EVERY EVENT "
        "(or has started to)",
        (
            f"    declared : hostname embedded in bucket ids = "
            f"{HOSTNAME_IS_EMBEDDED_IN_BUCKET_IDS}\n"
            f"    actual   : {effective} "
            f"(reads hostname={reads_hostname}, in bucket_id={in_bucket_ids})\n\n"
            "  The hostname is read once at startup and interpolated into every\n"
            "  bucket id, so it accompanies every event whether or not anyone\n"
            "  treated it as an identifier. On a Mac named by its owner it\n"
            "  identifies the person, not the machine.\n\n"
        ),
        (
            "    * If this stopped being true, that is a privacy improvement\n"
            "      and the disclosure should stop claiming it. Update\n"
            "      HOSTNAME_IS_EMBEDDED_IN_BUCKET_IDS either way.\n"
        ),
    )


# ---------------------------------------------------------------------------
# 6. The baseline itself must stay honest.
# ---------------------------------------------------------------------------


def test_every_declaration_carries_a_reason():
    """A baseline entry with no stated reason is a value nobody can review."""
    for group, fields in BASELINE.items():
        for name, declared in fields.items():
            assert declared.why.strip(), f"{group}.{name} declares no reason"
            if declared.kind == DISCLOSED:
                assert len(declared.why) > 40, (
                    f"{group}.{name} is DISCLOSED but its reason is too thin to "
                    "review against the Regulament"
                )


def test_the_baseline_points_at_the_regulament():
    """Greppable from both ends: someone reading the Regulament must be able to
    find the code, and someone reading the code must be able to find the
    Regulament."""
    text = (_SRC / "disclosure_baseline.py").read_text()
    assert REGULAMENT_ARTICLE in text
    assert DISCLOSURE_CONTACT in text
    assert "art. 68^1" in REGULAMENT_ARTICLE
