#!/usr/bin/env python3
"""Write a words.json fixture from the synthetic clip's ground truth.

This exists because a real recogniser cannot always be run in a test
environment (no model weights, no network). It fabricates word timings by
distributing each known line evenly across its known span, which is enough to
exercise the resolver, renderer, QA and subtitle stages against real media.

It is NOT a substitute for stage 03 on real footage: the timings are regular
where real speech is not. Runs created this way are marked accordingly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.guards import RunPaths, write_text
from vpipe.runstate import RunState
from vpipe.timecode import format_hms
from vpipe.words import Transcript, Word


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runs-root", type=Path,
                        default=Path(__file__).resolve().parent.parent / "runs")
    parser.add_argument("--truth", type=Path, required=True,
                        help="The .truth.json written by make_synthetic_source.py")
    args = parser.parse_args()

    paths = RunPaths(args.runs_root, args.run_id)
    truth = json.loads(args.truth.read_text(encoding="utf-8"))

    words: list[Word] = []
    for line in truth["lines"]:
        tokens = line["text"].split()
        span = line["end"] - line["start"]
        step = span / max(len(tokens), 1)
        for i, token in enumerate(tokens):
            start = line["start"] + i * step
            words.append(Word(
                index=len(words), text=token,
                start=round(start, 3), end=round(start + step * 0.92, 3),
            ))

    transcript = Transcript(words, language="en", backend="fixture")
    transcript.save(paths.transcript / "words.json")
    write_text(paths.transcript / "transcript.txt",
               " ".join(w.text for w in words) + "\n", paths.root)
    write_text(paths.transcript / "words_timed.txt",
               "\n".join(f"[{format_hms(w.start)}] {w.text}" for w in words) + "\n",
               paths.root)

    state = RunState(paths)
    state.record("03_transcribe", backend="fixture", model="none",
                 words=len(words), language="en",
                 fixture_warning="Timings fabricated from ground truth, not ASR.")
    print(f"fixture transcript: {len(words)} words -> {paths.transcript/'words.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
