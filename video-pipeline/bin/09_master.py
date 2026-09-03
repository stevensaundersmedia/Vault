#!/usr/bin/env python3
"""Stage 09 — master render at source resolution. GATED.

Refuses to run unless the exact 720p review file, by SHA-256, has been
approved. The master is produced from the same EDL as the review by the same
code path, so it cannot be a different edit.
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
from vpipe.runstate import check_review_approval
from vpipe.segments import EDL
from vpipe.timecode import format_hms

STAGE = "09_master"
REVIEW_NAME = "review_720p.mp4"
MASTER_NAME = "master.mp4"


@main_guard
def main() -> int:
    args = base_parser(__doc__).parse_args()
    ctx = StageContext(args, deep_verify=True)

    banner(f"Stage 09 — master render [{args.run_id}]")

    # Safety rule 3. Checked before anything else happens.
    approval = check_review_approval(ctx.paths, ctx.paths.review / REVIEW_NAME)
    report("approved review", Path(approval["approved_file"]).name)
    report("approved sha256", approval["sha256"])
    report("approved at", approval["approved_at"])
    if approval.get("note"):
        report("note", approval["note"])

    edl = EDL.load(ctx.paths.resolved / "edl.json")
    if edl.source_sha256 and ctx.sentinel.sha256 and \
            edl.source_sha256 != ctx.sentinel.sha256:
        raise PipelineError(
            "The EDL was resolved against a different source file than the one "
            "in this run's manifest. Refusing to master."
        )

    output = ctx.paths.out("master", MASTER_NAME)
    master_cfg = ctx.config["master"]

    report("resolution", f"{ctx.manifest['video']['width']}x"
                         f"{ctx.manifest['video']['height']} (source, unscaled)")
    report("expected duration", format_hms(edl.total_duration))
    print("  rendering master (single pass, one continuous audio conform)...",
          flush=True)

    render_edit(
        edl, ctx.source_path, output,
        run_dir=ctx.paths.root,
        profile=master_cfg,
        fade_ms=ctx.config["audio"]["join_fade_ms"],
        height=None,                      # source resolution preserved
        log_path=ctx.log(STAGE),
    )

    probe = ffprobe_json(output, log_path=ctx.log(STAGE))
    actual = float(probe["format"]["duration"])
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    digest = sha256_file(output)
    write_text(ctx.paths.out("master", MASTER_NAME + ".sha256"),
               f"{digest}  {MASTER_NAME}\n", ctx.paths.root)

    drift = abs(actual - edl.total_duration)
    report("output", output)
    report("duration", f"{format_hms(actual)} (drift vs EDL {drift:.3f}s)")
    report("resolution", f"{video.get('width')}x{video.get('height')}")
    report("sha256", digest)

    ctx.sentinel.verify(deep=True)
    ctx.state.record(STAGE, output=str(output), sha256=digest,
                     duration=actual, drift=drift,
                     from_review_sha256=approval["sha256"])

    print("""
  Source verified unchanged (full hash).

  Nothing is uploaded. Upload requires a separate explicit instruction and a
  separate approval; see bin/10_upload.py --help.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
