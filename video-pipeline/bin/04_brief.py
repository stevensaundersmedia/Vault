#!/usr/bin/env python3
"""Stage 04 — editorial brief.

Writes the packet a capable model reads in order to make the only judgement
call in this pipeline: which spans of speech survive. The brief carries the
transcript as numbered sentences and the version-one editorial rules.

This stage does not call a model. It prepares the input and, on a second run
with --validate, structurally checks the answer that came back. Keeping the
model call outside the pipeline means the editorial pass is reproducible and
reviewable, and the decision file can be hand-edited.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.decisions import SCHEMA, load_decisions, segment_sentences
from vpipe.guards import write_text
from vpipe.timecode import format_hms
from vpipe.words import Transcript

STAGE = "04_brief"

RULES = """\
# Editorial brief — version one

## What this is
A single talking-head recording, transcribed with word-level timestamps. Your
job is to choose which spans of speech survive into the edit. You are the only
part of this pipeline making a judgement call.

## What you must output
A JSON object, and nothing else:

{
  "schema": "vpipe.decisions/1",
  "keep": [
    {
      "id": "k01",
      "start_quote": "the exact opening words of the span, quoted verbatim",
      "end_quote": "the exact closing words of the span, quoted verbatim",
      "category": "spine | opinion | example | cta | context",
      "reason": "one line on why this survives"
    }
  ],
  "removed": [
    {"category": "setup | false_start | repeated_take | tangent | post_signoff",
     "quote": "a few words identifying what you dropped",
     "reason": "one line"}
  ],
  "notes": "anything you were unsure about"
}

## Hard rules
1. Quote verbatim from the transcript below. Anchors are matched against the
   transcript ignoring case and punctuation, but the WORDS must match exactly.
2. Every anchor must be UNIQUE in the transcript. If a phrase repeats, extend
   the quote until only one place can match. A non-unique anchor stops the run.
3. Cut on sentence boundaries only. start_quote must begin at the first word of
   a sentence; end_quote must finish at the last word of a sentence, including
   its terminal punctuation.
4. Spans must be in transcript order and must not overlap.
5. Never write a timestamp, a frame number or a duration. All timing is
   computed locally from the word timestamps. Timing fields in your answer are
   rejected outright.
6. If an edit boundary is genuinely ambiguous — you cannot tell where a thought
   ends, or whether two takes are the same point — do NOT pick one. Leave the
   span out of `keep` and describe the problem in `notes`. Stopping is correct
   behaviour; guessing is not.

## What to remove
- Setup and room chatter before the content starts
- False starts and restarts
- Repeated takes: keep the best single delivery, drop the others
- Irrelevant tangents
- Everything after the sign-off

## What to keep
- The teaching spine: the argument being made, in order
- Strong opinions, stated plainly
- Examples and stories that carry the argument
- The final call to action

## Out of scope for version one
No music, no B-roll, no zooms, no generated animation, no reordering. The edit
is subtractive only: retained spans play in their original order.
"""


@main_guard
def main() -> int:
    parser = base_parser(__doc__)
    parser.add_argument("--validate", action="store_true",
                        help="Structurally check an existing decisions.json "
                             "instead of writing the brief.")
    args = parser.parse_args()

    ctx = StageContext(args)
    ctx.state.require("03_transcribe", "transcription")

    words_path = ctx.paths.transcript / "words.json"
    transcript = Transcript.load(words_path)
    sentences = segment_sentences(transcript)
    decisions_path = ctx.paths.decisions / "decisions.json"

    if args.validate:
        banner(f"Stage 04 — validating decisions [{args.run_id}]")
        keeps, raw = load_decisions(decisions_path)
        report("keep spans", len(keeps))
        report("removed noted", len(raw.get("removed", [])))
        if raw.get("notes"):
            report("model notes", raw["notes"][:100])
        print("\n  Structure is valid. Anchors are resolved in stage 05.\n")
        return 0

    banner(f"Stage 04 — editorial brief [{args.run_id}]")

    lines = [RULES, "\n## Transcript\n",
             f"Source duration {format_hms(ctx.manifest['duration'])}, "
             f"{len(transcript.words)} words, {len(sentences)} sentences.\n"]
    for sentence in sentences:
        lines.append(f"[S{sentence['index']:04d}] {sentence['text']}")

    brief_path = ctx.paths.out("decisions", "brief.md")
    write_text(brief_path, "\n".join(lines) + "\n", ctx.paths.root)

    # A machine-readable copy so a caller can build the prompt programmatically.
    write_text(
        ctx.paths.out("decisions", "sentences.json"),
        json.dumps({"schema": "vpipe.sentences/1", "sentences": sentences},
                   indent=2, ensure_ascii=False) + "\n",
        ctx.paths.root,
    )

    report("sentences", len(sentences))
    report("brief", brief_path)
    ctx.sentinel.verify()
    ctx.state.record(STAGE, sentences=len(sentences), brief=str(brief_path))

    print(f"""
  Next:
    1. Give {brief_path} to a capable model.
    2. Save its JSON answer as:
         {decisions_path}
    3. Check it:  bin/04_brief.py --run-id {args.run_id} --validate
    4. Resolve:   bin/05_resolve.py --run-id {args.run_id}
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
