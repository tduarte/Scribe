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

    def test_english_only_fillers_are_kept_in_other_languages(self):
        # "er" is German for "he"; "ah" is a word in Portuguese and Spanish.
        assert remove_fillers("Ah, er ist da", language="de") == "Ah, er ist da"
        assert remove_fillers("ah sim, um momento", language="pt") == "ah sim, momento"

    def test_neutral_grunts_go_in_every_language(self):
        assert remove_fillers("hmm um das ist uh gut", language="de") == "das ist gut"

    def test_custom_fillers_apply_in_every_language(self):
        assert remove_fillers("also er kommt", extra=["also"], language="de") == "er kommt"

    def test_unknown_language_is_treated_as_english(self):
        assert remove_fillers("er okay", language="") == "okay"
        assert remove_fillers("er okay", language="en-US") == "okay"


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

    # The cases below were reproduced against the shipped matcher; each one
    # rewrote a word the user actually said.

    def test_a_longer_word_containing_the_entry_is_not_rewritten(self):
        assert apply_custom_words("please describe the problem", ["Scribe"]) == \
            "please describe the problem"

    def test_possessives_and_plurals_keep_their_inflection(self):
        assert apply_custom_words("Scribe's window and two scribes", ["Scribe"]) == \
            "Scribe's window and two Scribes"

    def test_short_entries_never_fuzzy_match(self):
        assert apply_custom_words("he is here", ["her"]) == "he is here"

    def test_a_word_that_differs_only_by_a_plural_is_left_alone(self):
        assert apply_custom_words("run the tests then test it", ["tests"]) == \
            "run the tests then test it"

    def test_a_near_miss_keeps_its_inflection(self):
        assert apply_custom_words("kubernetis's pods", ["Kubernetes"]) == \
            "Kubernetes's pods"
        assert apply_custom_words("both kubernetises", ["Kubernetes"]) == \
            "both Kuberneteses"

    def test_a_near_miss_of_a_very_different_length_is_left_alone(self):
        assert apply_custom_words("kubernetistic", ["Kubernetes"]) == "kubernetistic"

    def test_multi_word_entries_match_a_run_of_tokens(self):
        assert apply_custom_words("fly to new yorck tomorrow", ["New York"]) == \
            "fly to New York tomorrow"
        assert apply_custom_words("fly to new york tomorrow", ["New York"]) == \
            "fly to New York tomorrow"

    def test_multi_word_entries_need_the_words_adjacent(self):
        assert apply_custom_words("new, york", ["New York"]) == "new, york"

    def test_the_longest_phrase_wins(self):
        assert apply_custom_words("in new york city", ["New York", "New York City"]) == \
            "in New York City"

    def test_accented_entries_still_match(self):
        assert apply_custom_words("we went to sao paulo", ["São Paulo"]) == \
            "we went to São Paulo"


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

    def test_language_reaches_the_filler_step(self):
        assert process("Ah, er ist da", language="de") == "Ah, er ist da"
        assert process("Ah, er ist da") == "Ist da"
