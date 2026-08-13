"""Rosetta-aware architecture detection.

``platform.machine()`` describes the running PROCESS, not the hardware, so an
x86_64 build translated by Rosetta 2 on Apple Silicon is indistinguishable from
a genuine Intel Mac by that call alone. These pin the discriminator
(``sysctl.proc_translated``) and — just as importantly — pin the direction the
detection fails in, because guessing "translated" wrongly on a real Intel Mac
would serve it an arm64 build it physically cannot execute.
"""

import inspect
import platform
from unittest.mock import MagicMock, patch

import pytest

from src import machine_arch as ma
from src.machine_arch import (
    ARM64,
    X86_64,
    is_rosetta_translated,
    true_machine_arch,
)
from src.ui import tray as tray_mod
from src.ui.tray import TrayIcon, arch_menu_label


@pytest.fixture(autouse=True)
def _clear_arch_cache():
    ma.reset_cache_for_tests()
    yield
    ma.reset_cache_for_tests()


def _reader(value):
    """A sysctl probe stub returning a fixed raw value (None = unreadable)."""
    return lambda: value


# --- is_rosetta_translated -------------------------------------------------


def test_translated_when_proc_translated_is_one():
    assert is_rosetta_translated(system="Darwin", sysctl_reader=_reader("1")) is True


def test_not_translated_when_proc_translated_is_zero():
    assert is_rosetta_translated(system="Darwin", sysctl_reader=_reader("0")) is False


def test_not_translated_when_key_is_absent():
    """A real Intel Mac has no sysctl.proc_translated key at all — sysctl exits
    non-zero and the reader yields None. Absent must read the same as "0";
    treating absence as translated would mark every Intel Mac as wrong-arch."""
    assert is_rosetta_translated(system="Darwin", sysctl_reader=_reader(None)) is False


def test_never_translated_off_macos():
    """Rosetta 2 is macOS-only. The probe must not even run elsewhere — a Linux
    box with some unrelated sysctl must never be called translated."""
    assert is_rosetta_translated(system="Linux", sysctl_reader=_reader("1")) is False
    assert is_rosetta_translated(system="Windows", sysctl_reader=_reader("1")) is False


def test_unreadable_sysctl_fails_toward_native(monkeypatch):
    """A sysctl that is missing, sandboxed or times out must degrade to "not
    translated" rather than raising or claiming translation."""
    def _boom(*a, **k):
        raise OSError("sysctl not found")

    monkeypatch.setattr(ma.subprocess, "run", _boom)
    assert is_rosetta_translated(system="Darwin") is False


def test_probe_runs_at_most_once_per_process(monkeypatch):
    """The tray rebuilds its menu on every stats update, under _menu_lock — the
    probe must not follow it. Forking sysctl (timeout=2) there blocks every
    other thread's menu update for as long as the probe takes, and the answer
    cannot change while the process lives.

    A FAILING probe must be cached too: on a genuine Intel Mac the key does not
    exist, so a retrying probe spawns a subprocess on every rebuild forever.
    """
    calls = []

    def _probe(*a, **k):
        calls.append(1)
        raise OSError("sysctl not found")

    monkeypatch.setattr(ma.subprocess, "run", _probe)

    for _ in range(3):
        assert is_rosetta_translated(system="Darwin") is False
    assert len(calls) == 1


# --- true_machine_arch -----------------------------------------------------


def test_translated_x86_process_reports_arm64_hardware():
    """THE DEFECT. Under Rosetta the process says x86_64; the machine is arm64."""
    assert (
        true_machine_arch(system="Darwin", machine=X86_64, translated=True) == ARM64
    )


def test_native_apple_silicon_reports_arm64():
    assert (
        true_machine_arch(system="Darwin", machine=ARM64, translated=False) == ARM64
    )


def test_genuine_intel_mac_still_reports_x86_64():
    """The direction that must never break: an arm64 binary cannot run on Intel
    at all, so a wrong answer here bricks the install rather than slowing it."""
    assert (
        true_machine_arch(system="Darwin", machine=X86_64, translated=False) == X86_64
    )


@pytest.mark.parametrize("system", ["Linux", "Windows"])
def test_non_macos_arch_passes_through_untouched(system):
    assert true_machine_arch(system=system, machine=X86_64) == X86_64
    assert true_machine_arch(system=system, machine="aarch64") == "aarch64"


# --- arch_menu_label -------------------------------------------------------


def test_label_names_the_remedy_when_translated():
    """A user reading "Intel build" has no reason to know that is the wrong one,
    so the mismatch label must say what to do about it."""
    label = arch_menu_label(system="Darwin", machine=X86_64, translated=True)
    assert label == (
        "Architecture: Intel build on Apple Silicon — switch to the Apple Silicon version"
    )


def test_label_for_native_apple_silicon():
    assert (
        arch_menu_label(system="Darwin", machine=ARM64, translated=False)
        == "Architecture: Apple Silicon (native)"
    )


def test_label_for_genuine_intel_mac_is_not_a_warning():
    """An Intel Mac running the Intel build is correct — it must not be nagged."""
    label = arch_menu_label(system="Darwin", machine=X86_64, translated=False)
    assert label == "Architecture: Intel (x86_64)"
    assert "switch" not in label.lower()


def test_label_off_macos_is_plain():
    assert (
        arch_menu_label(system="Linux", machine=X86_64, translated=False)
        == "Architecture: x86_64"
    )


# --- the callsite guard: the production menu really uses it ----------------


class _RecordedItem:
    """Stand-in for pystray.MenuItem that records what production asked for."""

    instances: list = []

    def __init__(self, text, action=None, enabled=True, **kwargs):
        self.text = text
        self.action = action
        self.enabled = enabled
        _RecordedItem.instances.append(self)


def _arch_items() -> list:
    """Drive the REAL _create_menu() and return the architecture rows it built.

    pystray binds its backend at import time, so on a headless CI runner
    ``tray.pystray`` is None and TrayIcon() is unconstructable — hence the
    repo-standard patch. Without this guard, deleting _arch_menu_item() from
    _create_menu leaves every assertion above green (Phantom 3).
    """
    _RecordedItem.instances = []
    with patch.object(tray_mod, "pystray", MagicMock()):
        icon = TrayIcon()
        with patch.object(tray_mod, "Item", _RecordedItem):
            icon._create_menu()
    return [i for i in _RecordedItem.instances if "Architecture:" in str(i.text)]


def test_production_menu_renders_the_arch_row(monkeypatch):
    """Fails if the row is dropped from _create_menu, not only if the rule breaks."""
    monkeypatch.setattr(tray_mod, "arch_menu_label", lambda: "Architecture: probed")

    (item,) = _arch_items()
    assert str(item.text) == "Architecture: probed"
    assert not item.enabled, "the row states a fact; it is not clickable"


def test_production_menu_label_agrees_with_the_label_builder():
    """The rendered label matches arch_menu_label()'s output on this host."""
    (item,) = _arch_items()
    assert str(item.text) == arch_menu_label()


def test_arch_item_delegates_instead_of_re_deriving_the_label():
    """Callsite guard — the menu must not grow its own copy of the rule.

    Comparing outputs cannot catch this: a parallel builder emitting the same
    string passes every assertion above. Only reading the consumer's source
    sees it, which is what one-rule-one-implementation.md prescribes.
    """
    source = inspect.getsource(TrayIcon._arch_menu_item)
    doc = TrayIcon._arch_menu_item.__doc__
    if doc:
        source = source.replace(doc, "")

    assert "arch_menu_label()" in source, "the menu stopped calling the shared builder"
    assert "Architecture:" not in source, "label text re-derived at the callsite"
    assert "platform.machine" not in source, "arch resolved a second time at the callsite"


# --- the real host, as a sanity control ------------------------------------


def test_detection_runs_on_this_host_without_error():
    """No fixture: prove the real probe executes and returns something sane on
    whatever machine CI or a developer is using. Guards against the module
    working only under mocks."""
    arch = true_machine_arch()
    assert isinstance(arch, str) and arch
    if platform.system() == "Darwin":
        # Every Mac is one or the other; a translated x86_64 must resolve to arm64.
        assert arch in (ARM64, X86_64)
