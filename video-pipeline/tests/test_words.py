#!/usr/bin/env python3
"""Tests for ASR backend normalisation and anchor matching.

The model download itself is a thin call; what is worth testing is the code
that flattens two very different backend payloads into one schema, repairs the
timing quirks both are known to emit, and refuses ambiguous anchors.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.errors import AmbiguityStop, PipelineError
from vpipe.words import (
    Transcript, Word, from_faster_whisper, from_whisper_cpp, normalise_token,
)

# A realistic faster-whisper payload as written by bin/03_transcribe.py.
FW_PAYLOAD = {
    "language": "en",
    "duration": 6.2,
    "segments": [
        {"id": 0, "start": 0.0, "end": 3.1, "text": " Today we talk about drafts.",
         "words": [
             {"word": " Today", "start": 0.10, "end": 0.52, "probability": 0.99},
             {"word": " we", "start": 0.52, "end": 0.70, "probability": 0.98},
             {"word": " talk", "start": 0.70, "end": 1.02, "probability": 0.97},
             {"word": " about", "start": 1.02, "end": 1.40, "probability": 0.99},
             {"word": " drafts.", "start": 1.40, "end": 1.95, "probability": 0.96},
         ]},
        {"id": 1, "start": 3.1, "end": 6.2, "text": " Throw them away.",
         "words": [
             {"word": " Throw", "start": 3.20, "end": 3.55, "probability": 0.95},
             {"word": " them", "start": 3.55, "end": 3.72, "probability": 0.99},
             {"word": " away.", "start": 3.72, "end": 4.30, "probability": 0.98},
         ]},
    ],
}

# whisper.cpp --output-json-full --max-len 1: millisecond offsets, and the
# special markers that must be dropped.
CPP_PAYLOAD = {
    "result": {"language": "en"},
    "transcription": [
        {"timestamps": {"from": "00:00:00,100", "to": "00:00:00,520"},
         "offsets": {"from": 100, "to": 520}, "text": " Today",
         "tokens": [
             {"text": "[_BEG_]", "offsets": {"from": 0, "to": 100}},
             {"text": " Today", "offsets": {"from": 100, "to": 520}},
         ]},
        {"offsets": {"from": 520, "to": 700}, "text": " we",
         "tokens": [{"text": " we", "offsets": {"from": 520, "to": 700}}]},
        {"offsets": {"from": 700, "to": 1020}, "text": " talk",
         "tokens": [{"text": " talk", "offsets": {"from": 700, "to": 1020}}]},
        {"offsets": {"from": 1020, "to": 1400}, "text": " about",
         "tokens": [{"text": " about", "offsets": {"from": 1020, "to": 1400}}]},
        {"offsets": {"from": 1400, "to": 1950}, "text": " drafts.",
         "tokens": [{"text": " drafts.", "offsets": {"from": 1400, "to": 1950}}]},
    ],
}


class TestNormalisation(unittest.TestCase):
    def test_faster_whisper_flattens_segments_to_words(self):
        tr = from_faster_whisper(FW_PAYLOAD)
        self.assertEqual(len(tr.words), 8)
        self.assertEqual(tr.backend, "faster-whisper")
        self.assertEqual(tr.words[0].text, "Today")
        self.assertAlmostEqual(tr.words[0].start, 0.10)
        self.assertAlmostEqual(tr.words[-1].end, 4.30)
        # Indices are contiguous after cleanup, whatever the segment structure.
        self.assertEqual([w.index for w in tr.words], list(range(8)))

    def test_whisper_cpp_converts_ms_and_drops_markers(self):
        tr = from_whisper_cpp(CPP_PAYLOAD)
        self.assertEqual(tr.backend, "whisper.cpp")
        self.assertEqual([w.text for w in tr.words],
                         ["Today", "we", "talk", "about", "drafts."])
        self.assertAlmostEqual(tr.words[0].start, 0.10)
        self.assertAlmostEqual(tr.words[4].end, 1.95)

    def test_both_backends_agree_on_the_same_speech(self):
        """The whole point of normalisation: identical downstream behaviour."""
        fw = from_faster_whisper(FW_PAYLOAD)
        cpp = from_whisper_cpp(CPP_PAYLOAD)
        self.assertEqual(cpp.norm_text, "today we talk about drafts")
        self.assertTrue(fw.norm_text.startswith(cpp.norm_text))
        self.assertEqual(fw.find_quote("talk about", label="t"),
                         cpp.find_quote("talk about", label="t"))

    def test_backend_without_word_timings_is_rejected(self):
        with self.assertRaises(PipelineError):
            from_faster_whisper({"segments": [{"text": "hi", "words": []}]})
        with self.assertRaises(PipelineError):
            from_whisper_cpp({"transcription": [{"text": "hi"}]})

    def test_non_monotonic_timings_are_repaired(self):
        """Both backends occasionally emit a word that starts before the
        previous one ended; left alone that produces an inverted segment."""
        payload = {"segments": [{"words": [
            {"word": "one", "start": 1.0, "end": 2.0},
            {"word": "two", "start": 1.5, "end": 1.4},
        ]}]}
        tr = from_faster_whisper(payload)
        self.assertGreaterEqual(tr.words[1].start, tr.words[0].end)
        self.assertGreater(tr.words[1].end, tr.words[1].start)


class TestAnchors(unittest.TestCase):
    def setUp(self):
        text = ("Right, so here is the thing. I want to say the thing again. "
               "And then a unique closing line.")
        self.tr = Transcript([
            Word(i, t, i * 0.5, i * 0.5 + 0.4) for i, t in enumerate(text.split())
        ])

    def test_unique_quote_resolves_to_word_span(self):
        first, last = self.tr.find_quote("here is the thing", label="k1.start")
        self.assertEqual(self.tr.by_index(first).text, "here")
        self.assertEqual(self.tr.by_index(last).text, "thing.")

    def test_repeated_quote_stops_instead_of_guessing(self):
        with self.assertRaises(AmbiguityStop) as ctx:
            self.tr.find_quote("the thing", label="k1.start")
        self.assertIn("matches 2 places", str(ctx.exception))

    def test_absent_quote_stops(self):
        with self.assertRaises(AmbiguityStop):
            self.tr.find_quote("words never spoken", label="k1.start")

    def test_matching_ignores_punctuation_and_case(self):
        a = self.tr.find_quote("here is the thing", label="a")
        b = self.tr.find_quote("HERE -- is,, the THING!", label="b")
        self.assertEqual(a, b)

    def test_matching_respects_word_boundaries(self):
        """`thin` must not match inside `thing`, or a cut would land mid-word."""
        with self.assertRaises(AmbiguityStop):
            self.tr.find_quote("here is the thin", label="a")

    def test_sentence_terminal_detection(self):
        self.assertTrue(Word(0, "thing.", 0, 1).ends_sentence)
        self.assertTrue(Word(0, 'end."', 0, 1).ends_sentence)
        self.assertTrue(Word(0, "Right?", 0, 1).ends_sentence)
        self.assertFalse(Word(0, "the", 0, 1).ends_sentence)
        self.assertFalse(Word(0, "mid,", 0, 1).ends_sentence)

    def test_normalise_token(self):
        self.assertEqual(normalise_token("Don’t!"), "don't")
        self.assertEqual(normalise_token("—"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
