#!/usr/bin/env python3
"""Stage 02 — ASR working audio.

Extracts a 16 kHz mono WAV for transcription only. This is a working copy for
the recogniser; the edit itself always conforms audio from the original source
stream, never from this downmix.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.ffmpeg_run import ffprobe_json, run_ffmpeg
from vpipe.timecode import format_hms

STAGE = "02_audio"


@main_guard
def main() -> int:
    args = base_parser(__doc__).parse_args()
    ctx = StageContext(args)
    ctx.state.require("01_manifest", "source manifest")

    banner(f"Stage 02 — ASR working audio [{args.run_id}]")

    audio_cfg = ctx.config["audio"]
    out = ctx.paths.out("audio", "asr_16k_mono.wav")

    run_ffmpeg(
        [
            "-i", str(ctx.source_path),
            "-vn",
            "-map", "0:a:0",
            "-ac", str(audio_cfg["asr_channels"]),
            "-ar", str(audio_cfg["asr_sample_rate"]),
            "-c:a", "pcm_s16le",
            str(out),
        ],
        outputs=[out],
        run_dir=ctx.paths.root,
        log_path=ctx.log(STAGE),
    )

    probe = ffprobe_json(out, log_path=ctx.log(STAGE))
    duration = float(probe["format"]["duration"])
    source_duration = ctx.manifest["duration"]

    # A large mismatch means the wrong track was mapped or the source is
    # damaged; either way the transcript would be misaligned with the video.
    drift = abs(duration - source_duration)
    if drift > 1.0:
        report("WARNING", f"extracted audio differs from source duration by {drift:.2f}s")

    report("output", out)
    report("duration", f"{format_hms(duration)}")
    report("format", f"{audio_cfg['asr_sample_rate']}Hz mono pcm_s16le")

    ctx.sentinel.verify()  # safety rule 1, re-checked after the read
    ctx.state.record(STAGE, output=str(out), duration=duration)
    print("\n  source untouched. Next: bin/03_transcribe.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
