"""Turn GNOME's trigger description into something worth looking at.

The GlobalShortcuts portal hands back a human string like ``Press <Alt>space``
and never the raw accelerator, so this is the only description we have. Rendered
verbatim it looks like a leaked implementation detail; split into keycaps it
reads as an instruction.
"""

from __future__ import annotations

import re

_MODIFIER = re.compile(r"<([A-Za-z_]+)>")

# GNOME's names on the left, what a keyboard actually says on the right.
_MODIFIER_NAMES = {
    "primary": "Ctrl",
    "control": "Ctrl",
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "super": "Super",
    "meta": "Meta",
    "hyper": "Hyper",
    "logo": "Super",
}

_KEY_NAMES = {
    "space": "Space",
    "return": "Enter",
    "escape": "Esc",
    "backspace": "Backspace",
    "tab": "Tab",
    "delete": "Delete",
    "insert": "Insert",
    "page_up": "Page Up",
    "page_down": "Page Down",
}


def keycaps(trigger_description: str) -> list[str]:
    """Split a trigger description into individual key labels.

    Returns an empty list when there is nothing usable, so callers can fall back
    to prose rather than rendering an empty row of boxes.
    """
    if not trigger_description:
        return []

    text = trigger_description.strip()
    # The portal prefixes a verb that is not part of the accelerator.
    text = re.sub(r"^(press|hold)\s+", "", text, flags=re.IGNORECASE)

    caps = [
        _MODIFIER_NAMES.get(name.lower(), name.capitalize())
        for name in _MODIFIER.findall(text)
    ]

    remainder = _MODIFIER.sub("", text).strip()
    # Accelerators may also arrive as plain "Ctrl+Alt+Space".
    for part in (p for p in re.split(r"[+\s]+", remainder) if p):
        key = part.lower()
        if key in _MODIFIER_NAMES:
            caps.append(_MODIFIER_NAMES[key])
        else:
            caps.append(_KEY_NAMES.get(key, part if len(part) > 1 else part.upper()))

    # Preserve order while dropping a modifier repeated by both spellings.
    seen: set[str] = set()
    return [c for c in caps if not (c in seen or seen.add(c))]
