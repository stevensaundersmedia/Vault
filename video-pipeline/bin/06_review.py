#!/usr/bin/env python3
"""Stage 06 — 720p review render.

Produces the file the approval gate attaches to. Nothing renders at source
resolution until this exact file, by checksum, has been approved.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.errors import PipelineError
from vpipe.ffmpeg_run import ffprobe_json
from vpipe.guards import sha256_file, write_text
from vpipe.render import render_edit
from vpipe.segments import EDL
from vpipe.timecode import format_hms

STAGE = "06_review"
REVIEW_NAME = "review_720p.mp4"


@main_guard
def main() -> int:
    args = base_parser(__doc__).parse_args()
    ctx = StageContext(args)
    ctx.state.require("05_resolve", "boundary resolution")

    banner(f"Stage 06 — 720p review render [{args.run_id}]")

    edl = EDL.load(ctx.paths.resolved / "edl.json")

    # The EDL records the source it was cut against. If that no longer matches,
    # the times in it refer to different footage.
    if edl.source_sha256 and ctx.sentinel.sha256 and \
            edl.source_sha256 != ctx.sentinel.sha256:
        raise PipelineError(
            "The EDL was resolved against a different source file than the one "
            "in this run's manifest. Re-run stage 05."
        )

    review_cfg = ctx.config["review"]
    output = ctx.paths.out("review", REVIEW_NAME)

    report("segments", len(edl.segments))
    report("expected duration", f"{format_hms(edl.total_duration)} "
                                f"({edl.total_duration:.3f}s)")
    report("target height", review_cfg["height"])
    print("  rendering (single pass, one continuous audio conform)...", flush=True)

    render_edit(
        edl, ctx.source_path, output,
        run_dir=ctx.paths.root,
        profile=review_cfg,
        fade_ms=ctx.config["audio"]["join_fade_ms"],
        height=review_cfg["height"],
        log_path=ctx.log(STAGE),
    )

    probe = ffprobe_json(output, log_path=ctx.log(STAGE))
    actual = float(probe["format"]["duration"])
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")

    digest = sha256_file(output)
    write_text(ctx.paths.out("review", REVIEW_NAME + ".sha256"),
               f"{digest}  {REVIEW_NAME}\n", ctx.paths.root)

    report("output", output)
    report("duration", f"{format_hms(actual)} ({actual:.3f}s)")
    report("resolution", f"{video.get('width')}x{video.get('height')}")
    report("sha256", digest)

    drift = abs(actual - edl.total_duration)
    report("duration drift", f"{drift:.3f}s")

    ctx.sentinel.verify(deep=True)  # full re-hash before the approval gate
    ctx.state.record(STAGE, output=str(output), sha256=digest,
                     duration=actual, expected=edl.total_duration, drift=drift)

    print(f"""
  Source verified unchanged (full hash).

  Next: bin/07_qa.py --run-id {args.run_id}
  Do NOT approve until the QA report is clean and you have watched this file.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
