"""Clean up raw Whisper output before it is inserted into the focused app.

Whisper emits more than words: it annotates non-speech ("[BLANK_AUDIO]",
"(wind blowing)", "♪"), it transcribes disfluencies verbatim, and it has no
idea that "Kubernetes" is a word you use. Everything here is pure text -> text so
it can be unit tested without GTK, audio, or a model.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# Whisper marks non-speech with brackets, parentheses, or music notes. These are
# annotations rather than things the user said, so they never belong in output.
_NON_SPEECH = re.compile(
    r"""
      \[[^\]]*\]          # [BLANK_AUDIO], [MUSIC], [Speaker 1]
    | \([^)]*\)           # (wind blowing), (laughs)
    | \*[^*]*\*           # *sighs*
    | [♪♫♬♩]+   # musical notes
    """,
    re.VERBOSE,
)

DEFAULT_FILLERS: tuple[str, ...] = (
    "um", "uh", "erm", "hmm", "mhm", "uhh", "umm", "er", "ah",
)

_WORD = re.compile(r"[\w']+", re.UNICODE)
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?%])")
_REPEATED_SPACE = re.compile(r"[ \t]{2,}")


def strip_non_speech(text: str) -> str:
    """Remove Whisper's non-speech annotations."""
    return _NON_SPEECH.sub(" ", text)


def collapse_whitespace(text: str) -> str:
    """Normalise runs of whitespace and tidy spacing around punctuation."""
    text = text.replace(" ", " ")
    text = _REPEATED_SPACE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    return text.strip()


def remove_fillers(text: str, extra: tuple[str, ...] | list[str] = ()) -> str:
    """Drop standalone filler words.

    Only whole tokens are removed, so "Umberto" and "uhh" are treated
    differently, and a sentence that is *only* fillers collapses to nothing.
    """
    fillers = {f.lower() for f in (*DEFAULT_FILLERS, *extra) if f.strip()}
    if not fillers:
        return text

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        core = word.strip("'").lower()
        return "" if core in fillers else word

    out = _WORD.sub(replace, text)
    # Removing a filler can strand punctuation or double spaces.
    out = re.sub(r"\s*,\s*,", ",", out)
    out = re.sub(r"^[\s,]+", "", out)
    return collapse_whitespace(out)


def _fold(word: str) -> str:
    """Case- and accent-insensitive key for fuzzy matching."""
    decomposed = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def apply_custom_words(
    text: str, vocabulary: list[str] | tuple[str, ...], threshold: float = 0.18
) -> str:
    """Nudge near-miss transcriptions towards the user's own vocabulary.

    ``threshold`` is a *distance*: a candidate is accepted when its similarity to
    a vocabulary entry is at least ``1 - threshold``. An exact match (ignoring
    case and accents) is left alone so we never fight the user's own casing.
    """
    vocab = [v.strip() for v in vocabulary if v.strip()]
    if not vocab:
        return text

    folded = {_fold(v): v for v in vocab}
    cutoff = max(0.0, min(1.0, 1.0 - threshold))

    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        key = _fold(word)
        if key in folded:
            # Already correct apart from case/accents -- adopt the user's spelling.
            return folded[key]
        if len(key) < 3:
            return word
        best = difflib.get_close_matches(key, folded.keys(), n=1, cutoff=cutoff)
        if not best:
            return word
        replacement = folded[best[0]]
        # Preserve a leading capital if the speaker started a sentence.
        if word[:1].isupper() and replacement[:1].islower():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    return _WORD.sub(replace, text)


def capitalize_first(text: str) -> str:
    """Capitalise the first alphabetic character, leaving the rest alone."""
    for i, ch in enumerate(text):
        if ch.isalpha():
            return text[:i] + ch.upper() + text[i + 1:]
    return text


def process(
    text: str,
    *,
    custom_words: list[str] | tuple[str, ...] = (),
    word_threshold: float = 0.18,
    remove_filler_words: bool = True,
    custom_fillers: list[str] | tuple[str, ...] = (),
    capitalize: bool = True,
    trailing_space: bool = False,
) -> str:
    """Run the full clean-up pipeline. Returns "" if nothing was actually said."""
    out = strip_non_speech(text)
    out = collapse_whitespace(out)
    if remove_filler_words:
        out = remove_fillers(out, tuple(custom_fillers))
    if custom_words:
        out = apply_custom_words(out, custom_words, word_threshold)
    out = collapse_whitespace(out)
    if not out:
        return ""
    if capitalize:
        out = capitalize_first(out)
    if trailing_space:
        out += " "
    return out
