"""The window that shows the one-time privacy notice. A thin shell, on purpose.

Every word it displays comes from ``src/privacy_notice.py`` via ``notice_lines``
— this module owns layout and nothing else. That split is load-bearing rather
than tidy: the version recorded against an acknowledgement is a hash of that
same text, so any copy hard-coded here would be text the user read and the
record does not describe. ``tests/test_privacy_notice_window.py`` guards it by
reading this module's source.

Why a ``tk.Text`` and not the canvas the setup wizard draws on: canvas text is
positioned absolutely and silently clips when it overflows. Clipping a
decorative subtitle is a cosmetic bug; clipping a disclosure bullet is a
compliance defect that looks fine in the screenshot the author took. A scrolling
text widget cannot lose a line.

Threading: called from the main thread only (``BetterFlowApp.run``), before
pystray takes it over — the same sequencing the first-run wizard and the macOS
permission gate already use. Tk is not safe off the main thread on macOS.
"""

import contextlib
import logging
import platform
import tkinter as tk

try:
    from ..privacy_notice import (
        ACK_BUTTON_TEXT,
        NOTICE_SUBTITLE,
        NOTICE_TITLE,
        notice_lines,
    )
except ImportError:  # PyInstaller bundle
    from privacy_notice import (  # type: ignore[no-redef]
        ACK_BUTTON_TEXT,
        NOTICE_SUBTITLE,
        NOTICE_TITLE,
        notice_lines,
    )

logger = logging.getLogger(__name__)

WINDOW_WIDTH = 720
WINDOW_HEIGHT = 620

if platform.system() == "Darwin":
    FONT_FAMILY = "Avenir Next"
elif platform.system() == "Windows":
    FONT_FAMILY = "Segoe UI"
else:
    FONT_FAMILY = "sans-serif"

# BetterFlow palette, matching the setup wizard.
BG_COLOR = "#0f0a1a"
CARD_COLOR = "#1a1028"
TEXT_COLOR = "#f4f0ff"
TEXT_MUTED = "#b8a8d6"
PRIMARY_COLOR = "#7D69B8"
BTN_TEXT = "#ffffff"


def show_privacy_notice(_root_factory=tk.Tk) -> bool:
    """Show the notice modally. Returns True only if the user acknowledged it.

    Closing the window without pressing the button returns False, which leaves
    the acknowledgement unrecorded and re-shows the notice next launch. That is
    the correct legal behaviour: the record has to mean someone dismissed the
    text deliberately.

    ``_root_factory`` is injected only so tests can drive the callbacks without
    a display server.
    """
    acknowledged = {"value": False}
    root = _root_factory()
    root.title("BetterFlow — Privacy Notice")
    root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
    root.configure(bg=BG_COLOR)
    root.resizable(False, False)

    def _on_acknowledge() -> None:
        acknowledged["value"] = True
        root.destroy()

    def _on_close() -> None:
        # Not an acknowledgement. Leave the record unwritten and try again next
        # launch rather than banking a dismissal nobody read.
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    tk.Label(
        root,
        text=NOTICE_TITLE,
        font=(FONT_FAMILY, 20, "bold"),
        bg=BG_COLOR,
        fg=TEXT_COLOR,
        wraplength=WINDOW_WIDTH - 80,
        justify=tk.LEFT,
    ).pack(anchor="w", padx=40, pady=(32, 8))

    tk.Label(
        root,
        text=NOTICE_SUBTITLE,
        font=(FONT_FAMILY, 12),
        bg=BG_COLOR,
        fg=TEXT_MUTED,
        wraplength=WINDOW_WIDTH - 80,
        justify=tk.LEFT,
    ).pack(anchor="w", padx=40, pady=(0, 16))

    body_frame = tk.Frame(root, bg=BG_COLOR)
    body_frame.pack(fill=tk.BOTH, expand=True, padx=40)

    scrollbar = tk.Scrollbar(body_frame)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    body = tk.Text(
        body_frame,
        wrap=tk.WORD,
        font=(FONT_FAMILY, 12),
        bg=CARD_COLOR,
        fg=TEXT_COLOR,
        relief=tk.FLAT,
        padx=18,
        pady=16,
        highlightthickness=0,
        yscrollcommand=scrollbar.set,
    )
    body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.config(command=body.yview)

    body.insert("1.0", "\n".join(notice_lines()))
    # Read-only, but still selectable and scrollable — a disabled Text cannot be
    # copied, and someone should be able to paste this to their own records.
    body.config(state=tk.DISABLED)

    tk.Button(
        root,
        text=ACK_BUTTON_TEXT,
        command=_on_acknowledge,
        font=(FONT_FAMILY, 13, "bold"),
        bg=PRIMARY_COLOR,
        fg=BTN_TEXT,
        activebackground=PRIMARY_COLOR,
        activeforeground=BTN_TEXT,
        relief=tk.FLAT,
        highlightthickness=0,
        borderwidth=0,
        padx=28,
        pady=10,
    ).pack(pady=(20, 28))

    _bring_to_front(root)
    root.mainloop()
    return acknowledged["value"]


def _bring_to_front(root) -> None:
    """Raise the window above the tray-only app that has no other UI.

    Best-effort: a window manager that refuses any of this is a cosmetic
    problem, never a reason to fail the notice.
    """
    try:
        root.update_idletasks()
        x = (root.winfo_screenwidth() - WINDOW_WIDTH) // 2
        y = (root.winfo_screenheight() - WINDOW_HEIGHT) // 3
        root.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        root.lift()
        root.attributes("-topmost", True)
        # Drop topmost straight away so the notice does not sit over everything
        # the user does afterwards; the raise has already happened.
        root.after(600, lambda: _drop_topmost(root))
    except Exception as e:  # noqa: BLE001
        logger.debug("could not raise the privacy notice window: %s", e)


def _drop_topmost(root) -> None:
    with contextlib.suppress(Exception):
        root.attributes("-topmost", False)
