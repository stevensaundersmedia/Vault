#!/usr/bin/env python3
"""Generate a synthetic talking-head recording for testing the pipeline.

The clip carries real synthesised speech, so the whole chain including the
recogniser and the anchor matcher is exercised, not just the arithmetic. The
script deliberately contains the categories version one is expected to remove
(setup, a false start, a repeated take, a tangent, post-sign-off chat) around
a genuine teaching spine.

This writes to a directory you choose. It is a test fixture generator and is
not part of the pipeline; it never touches a real recording.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# (category, spoken line). Categories are recorded in the sidecar so a test can
# assert the editorial stage removed the right things.
SCRIPT = [
    ("setup",        "Okay, am I recording? Let me check the light. Right, I think we are good."),
    ("false_start",  "So today I want to talk about. Sorry, let me start that again."),
    ("spine",        "Today I want to talk about why your first draft should always be disposable."),
    ("take_1",       "Most people treat a first draft as a foundation. That is the mistake."),
    ("take_2",       "Most people treat a first draft as a foundation, and that is exactly the mistake."),
    ("opinion",      "A first draft is not a foundation. It is a question you are asking yourself."),
    ("example",      "Here is an example. Last month I threw away nine pages before the tenth one worked."),
    ("tangent",      "Which reminds me, the coffee machine in that office was genuinely terrible."),
    ("spine",        "The point is that throwing work away is not waste. It is how you find the shape."),
    ("cta",          "If that landed, subscribe to the channel and I will see you in the next one."),
    ("post_signoff", "Was that alright? I think I fluffed the middle bit. We can go again if you want."),
]

GAP_S = 0.75  # inter-line silence, giving the resolver real gaps to cut in


def need(tool: str) -> str:
    found = shutil.which(tool)
    if not found:
        sys.exit(f"Required tool not found: {tool}")
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--name", default="synthetic_talking_head.mp4")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", default="25")
    args = parser.parse_args()

    need("espeak-ng")
    need("ffmpeg")
    out_dir = args.out_dir.expanduser().resolve()
    work = out_dir / "_synth_work"
    work.mkdir(parents=True, exist_ok=True)

    # 1. One WAV per line, with a trailing silence gap.
    parts: list[Path] = []
    timeline = []
    cursor = 0.0
    for i, (category, line) in enumerate(SCRIPT):
        speech = work / f"line_{i:02d}.wav"
        subprocess.run(
            ["espeak-ng", "-s", "150", "-p", "45", "-v", "en-gb",
             "-w", str(speech), line],
            check=True, capture_output=True,
        )
        padded = work / f"padded_{i:02d}.wav"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(speech),
             "-af", f"apad=pad_dur={GAP_S}", "-ar", "48000", "-ac", "1",
             str(padded)],
            check=True, capture_output=True,
        )
        duration = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(padded)],
            check=True, capture_output=True, text=True).stdout.strip())
        timeline.append({
            "index": i, "category": category, "text": line,
            "start": round(cursor, 3), "end": round(cursor + duration - GAP_S, 3),
        })
        cursor += duration
        parts.append(padded)

    # 2. Concatenate into one continuous audio track.
    listing = work / "parts.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts), encoding="utf-8")
    full_audio = work / "full.wav"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(full_audio)],
        check=True, capture_output=True,
    )

    # 3. Video that visibly changes over time, so contact sheets and join
    #    frames carry real information rather than a flat colour.
    target = out_dir / args.name
    drawtext = (
        "drawtext=text='%{pts\\:hms}':fontcolor=white:fontsize=64:"
        "x=(w-text_w)/2:y=h-140:box=1:boxcolor=black@0.55:boxborderw=18"
    )
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={args.width}x{args.height}:rate={args.fps}",
         "-i", str(full_audio),
         "-vf", drawtext,
         "-shortest",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k",
         str(target)],
        check=True, capture_output=True,
    )

    sidecar = out_dir / (target.stem + ".truth.json")
    sidecar.write_text(json.dumps({
        "description": "Ground truth for the synthetic test source.",
        "gap_s": GAP_S,
        "lines": timeline,
    }, indent=2) + "\n", encoding="utf-8")

    shutil.rmtree(work)
    print(f"source:      {target}")
    print(f"ground truth {sidecar}")
    print(f"duration:    {cursor:.2f}s across {len(SCRIPT)} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
