import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from postprocess import (
    apply_custom_words, capitalize_first, collapse_whitespace,
    process, remove_fillers, strip_non_speech,
)


class TestStripNonSpeech:
    @pytest.mark.parametrize("raw,expected", [
        ("[BLANK_AUDIO]", ""),
        ("hello [MUSIC] world", "hello world"),
        ("(wind blowing) take shelter", "take shelter"),
        ("*sighs* fine", "fine"),
        ("♪♪♪", ""),
        ("nothing to strip", "nothing to strip"),
    ])
    def test_annotations_removed(self, raw, expected):
        assert collapse_whitespace(strip_non_speech(raw)) == expected


class TestFillers:
    def test_standalone_fillers_go(self):
        assert remove_fillers("um so uh this is it") == "so this is it"

    def test_words_containing_fillers_survive(self):
        # "Umberto" starts with "um"; only whole tokens are fillers.
        assert remove_fillers("Umberto uh arrived") == "Umberto arrived"

    def test_all_fillers_collapses_to_empty(self):
        assert remove_fillers("um uh hmm") == ""

    def test_custom_fillers(self):
        assert remove_fillers("basically it works", extra=["basically"]) == "it works"

    def test_case_insensitive(self):
        assert remove_fillers("Um okay") == "okay"


class TestCustomWords:
    def test_near_miss_corrected(self):
        assert apply_custom_words("deploy to kubernetis", ["Kubernetes"]) == \
            "deploy to Kubernetes"

    def test_exact_match_adopts_user_casing(self):
        assert apply_custom_words("use kubernetes", ["Kubernetes"]) == "use Kubernetes"

    def test_unrelated_words_untouched(self):
        assert apply_custom_words("the cat sat", ["Kubernetes"]) == "the cat sat"

    def test_short_words_not_mangled(self):
        # Two-letter tokens are too easy to false-match.
        assert apply_custom_words("go to it", ["Go"]) == "Go to it"

    def test_sentence_leading_capital_preserved(self):
        assert apply_custom_words("Kubernetis rocks", ["kubernetes"]) == \
            "Kubernetes rocks"

    def test_empty_vocabulary_is_a_noop(self):
        assert apply_custom_words("anything at all", []) == "anything at all"


class TestWhitespaceAndCaps:
    def test_space_before_punctuation_removed(self):
        assert collapse_whitespace("hello , world .") == "hello, world."

    def test_capitalize_skips_leading_punctuation(self):
        assert capitalize_first('"hello') == '"Hello'

    def test_capitalize_empty(self):
        assert capitalize_first("") == ""


class TestPipeline:
    def test_realistic_utterance(self):
        raw = "[BLANK_AUDIO] um  so , we deploy to kubernetis uh today ."
        assert process(raw, custom_words=["Kubernetes"]) == \
            "So, we deploy to Kubernetes today."

    def test_silence_yields_empty_string(self):
        assert process("[BLANK_AUDIO]") == ""
        assert process("  ♪  ") == ""

    def test_trailing_space_option(self):
        assert process("hello", trailing_space=True) == "Hello "

    def test_capitalize_can_be_disabled(self):
        assert process("hello", capitalize=False) == "hello"

    def test_fillers_can_be_kept(self):
        assert process("um hello", remove_filler_words=False) == "Um hello"
