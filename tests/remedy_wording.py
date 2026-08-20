"""One definition of "does this text actually give the off-then-on remedy?".

Lived in tests/test_accessibility_prompt.py first. Copied by hand into the tray
tests, in the naive `"off" in body and "on" in body` form -- which is vacuous:
"on" is a substring of "Monitoring", so a row reading "Input Monitoring blocked"
satisfies it with no remedy in it at all. Mutation-proven: stripping the remedy
from two of three branches left the suite green.

That is the same defect the accessibility-prompt file already carries controls
for, reintroduced two hours later by copying the assertion instead of importing
it. Hence one module, imported by both.
"""

import re


def conveys_the_off_then_on_remedy(text: str) -> bool:
    """True only when the text warns the toggle may lie AND gives BOTH steps.

    Whole words over a small alternation, never substrings. Both steps are
    required: "switch it off." satisfies a naive check and leaves the user worse
    off than before, because on a partly-blind machine they turn off the grant
    that still worked.
    """
    low = text.lower()
    turn_off = re.search(r"\b(off|disable|untick|uncheck)\b", low) is not None
    turn_on = re.search(r"\b(back on|then on|re-?enable|on again|turn it on)\b", low) is not None
    return turn_off and turn_on
