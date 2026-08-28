import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from shortcut_label import keycaps


@pytest.mark.parametrize("trigger,expected", [
    # What GNOME actually returned on this machine.
    ("Press <Alt>space", ["Alt", "Space"]),
    ("Press <Control><Alt>space", ["Ctrl", "Alt", "Space"]),
    ("<Alt>space", ["Alt", "Space"]),
    ("<Primary><Shift>d", ["Ctrl", "Shift", "D"]),
    ("<Super>Return", ["Super", "Enter"]),
    ("<Control>Escape", ["Ctrl", "Esc"]),
    # Plain accelerator spelling.
    ("Ctrl+Alt+Space", ["Ctrl", "Alt", "Space"]),
    ("Hold <Alt>space", ["Alt", "Space"]),
    ("F13", ["F13"]),
])
def test_known_triggers(trigger, expected):
    assert keycaps(trigger) == expected


def test_empty_input_yields_nothing():
    assert keycaps("") == []
    assert keycaps("   ") == []


def test_duplicate_modifiers_collapse():
    assert keycaps("<Control>Ctrl+a") == ["Ctrl", "A"]


def test_unknown_modifier_is_still_shown():
    assert keycaps("<Weird>k") == ["Weird", "K"]
