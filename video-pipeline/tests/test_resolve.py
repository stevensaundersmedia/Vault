#!/usr/bin/env python3
"""Tests for the resolver's refusal rules.

The value of this stage is not that it computes times correctly when the input
is good; it is that it stops when the input is bad. Each test here is a way an
editorial pass can be wrong, and asserts the pipeline halts rather than
producing a plausible-looking edit.
"""

import importlib.util
import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vpipe.decisions import load_decisions
from vpipe.errors import AmbiguityStop, PipelineError
from vpipe.segments import EDL, Segment, build_output_timeline
from vpipe.timecode import parse_rate, snap_down, snap_up
from vpipe.words import Transcript, Word

# bin/ scripts are not a package; load the module under test by path.
_spec = importlib.util.spec_from_file_location("resolve", ROOT / "bin" / "05_resolve.py")
resolve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve)

CUTS = {"lead_pad_s": 0.12, "tail_pad_s": 0.28, "min_segment_s": 0.40,
        "allow_nonterminal_anchors": False}


def transcript_from(pairs):
    """Build a transcript from (text, start, end) triples."""
    return Transcript([Word(i, t, s, e) for i, (t, s, e) in enumerate(pairs)])


def evenly(text, *, start=0.0, word_dur=0.4, gap=0.1):
    words, cursor = [], start
    for token in text.split():
        words.append((token, cursor, cursor + word_dur))
        cursor += word_dur + gap
    return words


class TestSentenceBoundaries(unittest.TestCase):
    def setUp(self):
        self.tr = transcript_from(evenly("One two three. Four five six. Seven eight nine."))

    def test_clean_sentence_span_is_accepted(self):
        resolve.check_sentence_boundaries(self.tr, 3, 5, "k01", False)

    def test_start_mid_sentence_stops(self):
        with self.assertRaises(AmbiguityStop) as ctx:
            resolve.check_sentence_boundaries(self.tr, 4, 5, "k01", False)
        self.assertIn("does not begin a sentence", str(ctx.exception))

    def test_end_mid_sentence_stops(self):
        with self.assertRaises(AmbiguityStop) as ctx:
            resolve.check_sentence_boundaries(self.tr, 3, 4, "k01", False)
        self.assertIn("does not finish a sentence", str(ctx.exception))

    def test_first_word_of_transcript_needs_no_predecessor(self):
        resolve.check_sentence_boundaries(self.tr, 0, 2, "k01", False)

    def test_override_permits_mid_sentence(self):
        """The escape hatch exists but version one never sets it."""
        resolve.check_sentence_boundaries(self.tr, 4, 4, "k01", True)


class TestTimePlacement(unittest.TestCase):
    def test_cut_lands_in_the_gap_not_in_the_speech(self):
        tr = transcript_from([("Hello.", 10.0, 10.5), ("World.", 12.0, 12.5)])
        start, end = resolve.resolve_times(tr, 0, 0, CUTS, 60.0)
        self.assertLessEqual(start, 10.0)
        self.assertGreaterEqual(end, 10.5)
        self.assertLess(end, 12.0, "the cut must not reach the next word")

    def test_padding_never_exceeds_half_the_available_gap(self):
        """With a tight gap the pad must shrink, or it eats the next word."""
        tr = transcript_from([("Hello.", 10.0, 10.5), ("World.", 10.6, 11.0)])
        _, end = resolve.resolve_times(tr, 0, 0, CUTS, 60.0)
        self.assertAlmostEqual(end, 10.5 + 0.05, places=6)

    def test_first_word_start_is_clamped_at_zero(self):
        tr = transcript_from([("Hello.", 0.02, 0.5)])
        start, _ = resolve.resolve_times(tr, 0, 0, CUTS, 60.0)
        self.assertGreaterEqual(start, 0.0)

    def test_last_word_end_is_clamped_to_source_duration(self):
        tr = transcript_from([("Hello.", 9.5, 9.95)])
        _, end = resolve.resolve_times(tr, 0, 0, CUTS, 10.0)
        self.assertLessEqual(end, 10.0)

    def test_snapping_is_outward_so_no_frame_is_clipped(self):
        fps = parse_rate("25")
        self.assertLessEqual(snap_down(1.234, fps), 1.234)
        self.assertGreaterEqual(snap_up(1.234, fps), 1.234)

    def test_snapped_times_land_on_frame_boundaries(self):
        fps = parse_rate("30000/1001")
        for raw in (0.0, 1.234, 12.5, 61.999):
            for snapped in (snap_down(raw, fps), snap_up(raw, fps)):
                frames = Fraction(snapped).limit_denominator(10**6) * fps
                self.assertEqual(frames.denominator, 1,
                                 f"{snapped} is not on a frame boundary")


class TestOrderingAndOverlap(unittest.TestCase):
    def test_overlapping_spans_are_rejected(self):
        segs = [Segment(0, 10, 20, 0, 0, keep_id="k01"),
                Segment(1, 15, 25, 0, 0, keep_id="k02")]
        with self.assertRaises(AmbiguityStop):
            EDL("s", "h", "25", 60, segs).validate(min_segment_s=0.4)

    def test_span_shorter_than_the_floor_is_rejected(self):
        segs = [Segment(0, 10.0, 10.2, 0, 0, keep_id="k01")]
        with self.assertRaises(AmbiguityStop) as ctx:
            EDL("s", "h", "25", 60, segs).validate(min_segment_s=0.4)
        self.assertIn("below the", str(ctx.exception))

    def test_span_past_the_end_of_the_source_is_rejected(self):
        segs = [Segment(0, 10, 90, 0, 0, keep_id="k01")]
        with self.assertRaises(AmbiguityStop):
            EDL("s", "h", "25", 60, segs).validate(min_segment_s=0.4)

    def test_empty_edit_is_rejected(self):
        with self.assertRaises(AmbiguityStop):
            EDL("s", "h", "25", 60, []).validate(min_segment_s=0.4)

    def test_output_timeline_is_contiguous_and_totals_correctly(self):
        segs = build_output_timeline([Segment(0, 10, 20, 0, 0),
                                      Segment(0, 30, 45, 0, 0)])
        self.assertEqual(segs[0].out_end, segs[1].out_start)
        self.assertAlmostEqual(EDL("s", "h", "25", 60, segs).total_duration, 25.0)

    def test_cut_time_maps_to_nothing_in_the_output(self):
        segs = build_output_timeline([Segment(0, 10, 20, 0, 0), Segment(0, 30, 45, 0, 0)])
        edl = EDL("s", "h", "25", 60, segs)
        self.assertIsNone(edl.src_to_out(25.0))
        self.assertAlmostEqual(edl.src_to_out(35.0), 15.0)


class TestDecisionFileContract(unittest.TestCase):
    def _write(self, tmp, payload):
        import json
        path = Path(tmp) / "decisions.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_timing_fields_are_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"schema": "vpipe.decisions/1", "keep": [
                {"id": "k01", "start_quote": "a", "end_quote": "b",
                 "reason": "r", "start_time": 12.4}]})
            with self.assertRaises(PipelineError) as ctx:
                load_decisions(path)
            self.assertIn("quoted transcript anchors only", str(ctx.exception))

    def test_missing_reason_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"schema": "vpipe.decisions/1", "keep": [
                {"id": "k01", "start_quote": "a", "end_quote": "b"}]})
            with self.assertRaises(PipelineError):
                load_decisions(path)

    def test_duplicate_ids_are_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            entry = {"id": "k01", "start_quote": "a", "end_quote": "b", "reason": "r"}
            path = self._write(tmp, {"schema": "vpipe.decisions/1",
                                     "keep": [entry, dict(entry)]})
            with self.assertRaises(PipelineError):
                load_decisions(path)

    def test_empty_keep_list_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"schema": "vpipe.decisions/1", "keep": []})
            with self.assertRaises(PipelineError):
                load_decisions(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
