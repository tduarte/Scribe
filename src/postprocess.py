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

# Disfluencies. The full list is English: "er" is German for "he" and "ah"
# is a word in several languages, so only the language-neutral grunts are
# removed when Whisper heard something other than English.
DEFAULT_FILLERS: tuple[str, ...] = (
    "um", "uh", "erm", "hmm", "mhm", "uhh", "umm", "er", "ah",
)
NEUTRAL_FILLERS: tuple[str, ...] = ("um", "uh", "umm", "uhh", "hmm", "mhm")

# Fuzzy matching is only safe on entries long enough that a near miss is
# unlikely to be a different word, and only when the two are nearly the same
# length: "describe" is not a near miss of "Scribe".
FUZZY_MIN_ENTRY = 5
FUZZY_MAX_LENGTH_DIFF = 1

# Inflections a vocabulary word may carry in speech. Matched on the stem and
# reattached, so "Scribe's" stays possessive instead of becoming "Scribe".
_SUFFIXES = ("'s", "s'", "'", "es", "s")

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


def _is_english(language: str) -> bool:
    lang = language.strip().lower()
    return not lang or lang == "en" or lang.startswith("en-") or lang.startswith("en_")


def remove_fillers(
    text: str, extra: tuple[str, ...] | list[str] = (), language: str = ""
) -> str:
    """Drop standalone filler words.

    Only whole tokens are removed, so "Umberto" and "uhh" are treated
    differently, and a sentence that is *only* fillers collapses to nothing.
    ``language`` is what Whisper heard; the English-only fillers apply when it
    is English or unknown, and the user's own list applies always.
    """
    base = DEFAULT_FILLERS if _is_english(language) else NEUTRAL_FILLERS
    fillers = {f.lower() for f in (*base, *extra) if f.strip()}
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


def _split_suffix(key: str) -> list[tuple[str, int]]:
    """Ways to read ``key`` as stem + inflection: (stem, suffix length)."""
    forms = [(key, 0)]
    for suffix in _SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix) + 1:
            forms.append((key[:-len(suffix)], len(suffix)))
    return forms


def _differ_only_by_suffix(a: str, b: str) -> bool:
    return any(a == b + sfx or b == a + sfx for sfx in _SUFFIXES)


def _recase(replacement: str, word: str) -> str:
    """Preserve a leading capital if the speaker started a sentence."""
    if word[:1].isupper() and replacement[:1].islower():
        return replacement[:1].upper() + replacement[1:]
    return replacement


class _Vocabulary:
    def __init__(self, entries: list[str], threshold: float) -> None:
        self.cutoff = max(0.0, min(1.0, 1.0 - threshold))
        # Single words, keyed by their folded form.
        self.single: dict[str, str] = {}
        # Phrases as lists of folded tokens, longest first so "New York City"
        # wins over "New York".
        self.phrases: list[tuple[list[str], str]] = []
        for entry in entries:
            parts = _WORD.findall(entry)
            if len(parts) > 1:
                self.phrases.append(([_fold(p) for p in parts], entry))
            elif parts:
                self.single[_fold(parts[0])] = entry
        self.phrases.sort(key=lambda item: -len(item[0]))

    # -- single tokens ---------------------------------------------------

    def _fuzzy(self, stem: str, candidates) -> str | None:
        if len(stem) < 3:
            return None
        pool = [
            c for c in candidates
            if len(c) >= FUZZY_MIN_ENTRY
            and abs(len(c) - len(stem)) <= FUZZY_MAX_LENGTH_DIFF
            and not _differ_only_by_suffix(c, stem)
        ]
        best = difflib.get_close_matches(stem, pool, n=1, cutoff=self.cutoff)
        return best[0] if best else None

    def correct(self, word: str) -> str | None:
        """The vocabulary spelling for ``word``, or None to leave it alone."""
        key = _fold(word)
        forms = _split_suffix(key)
        for stem, cut in forms:
            if stem in self.single:
                tail = word[len(word) - cut:] if cut else ""
                return self.single[stem] + tail
        for stem, cut in forms:
            hit = self._fuzzy(stem, self.single.keys())
            if hit:
                tail = word[len(word) - cut:] if cut else ""
                return _recase(self.single[hit], word) + tail
        return None

    # -- phrases ---------------------------------------------------------

    def _token_matches(self, word: str, target: str, whole: str) -> bool:
        key = _fold(word)
        if key == target:
            return True
        if len(whole) < FUZZY_MIN_ENTRY or len(key) < 3:
            return False
        if abs(len(key) - len(target)) > FUZZY_MAX_LENGTH_DIFF:
            return False
        if _differ_only_by_suffix(key, target):
            return False
        return bool(difflib.get_close_matches(key, [target], n=1, cutoff=self.cutoff))

    def phrase_at(self, text: str, tokens: list[re.Match[str]], i: int):
        """(entry, token count) if a phrase starts at token ``i``."""
        for parts, entry in self.phrases:
            n = len(parts)
            if i + n > len(tokens):
                continue
            span = tokens[i:i + n]
            gaps = (text[a.end():b.start()] for a, b in zip(span, span[1:]))
            if not all(gap.isspace() for gap in gaps):
                continue
            whole = _fold(entry)
            if all(self._token_matches(m.group(0), part, whole)
                   for m, part in zip(span, parts)):
                return entry, n
        return None


def apply_custom_words(
    text: str, vocabulary: list[str] | tuple[str, ...], threshold: float = 0.18
) -> str:
    """Nudge near-miss transcriptions towards the user's own vocabulary.

    ``threshold`` is a *distance*: a candidate is accepted when its similarity to
    a vocabulary entry is at least ``1 - threshold``. An exact match (ignoring
    case and accents) is left alone so we never fight the user's own casing.
    Inflections are kept ("Scribe's", "Scribes"), a near miss must be about
    the same length as the entry, and entries of several words are matched
    as a run of tokens.
    """
    entries = [v.strip() for v in vocabulary if v.strip()]
    if not entries:
        return text
    vocab = _Vocabulary(entries, threshold)

    tokens = list(_WORD.finditer(text))
    out: list[str] = []
    pos = 0
    i = 0
    while i < len(tokens):
        first = tokens[i]
        hit = vocab.phrase_at(text, tokens, i) if vocab.phrases else None
        if hit:
            entry, n = hit
            out.append(text[pos:first.start()])
            out.append(_recase(entry, first.group(0)))
            pos = tokens[i + n - 1].end()
            i += n
            continue
        word = first.group(0)
        fixed = vocab.correct(word)
        out.append(text[pos:first.start()])
        out.append(fixed if fixed is not None else word)
        pos = first.end()
        i += 1
    out.append(text[pos:])
    return "".join(out)


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
    language: str = "",
) -> str:
    """Run the full clean-up pipeline. Returns "" if nothing was actually said.

    ``language`` is the language Whisper reported; it decides which fillers
    are safe to remove.
    """
    out = strip_non_speech(text)
    out = collapse_whitespace(out)
    if remove_filler_words:
        out = remove_fillers(out, tuple(custom_fillers), language)
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
