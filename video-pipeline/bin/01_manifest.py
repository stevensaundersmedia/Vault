#!/usr/bin/env python3
"""Stage 01 — source manifest.

Probes the source with ffprobe, records an immutability sentinel, and creates
the run directory. This is the only stage that takes a source path; every
later stage reads it back from the manifest, so the source cannot be swapped
underneath a run.

The source is opened read-only and never copied, moved or transcoded here.
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.errors import PipelineError
from vpipe.ffmpeg_run import ffprobe_json, tool_versions
from vpipe.guards import RunPaths, SourceSentinel, assert_source_usable, write_json
from vpipe.runstate import RunState
from vpipe.timecode import format_hms, parse_rate

STAGE = "01_manifest"


def pick_streams(probe: dict) -> tuple[dict, dict]:
    """Version one is one video source with one audio track; enforce that.

    Multi-track sources are refused rather than silently defaulting to the
    first track, because picking the wrong language or a mix-minus feed would
    be discovered only after a full render.
    """
    video = [s for s in probe["streams"] if s.get("codec_type") == "video"
             and s.get("disposition", {}).get("attached_pic", 0) == 0]
    audio = [s for s in probe["streams"] if s.get("codec_type") == "audio"]

    if len(video) != 1:
        raise PipelineError(
            f"Expected exactly one video stream, found {len(video)}. Version one "
            f"handles a single video source only."
        )
    if len(audio) != 1:
        raise PipelineError(
            f"Expected exactly one audio track, found {len(audio)}. Version one "
            f"handles a single audio track only; choosing one for you risks "
            f"editing to the wrong feed."
        )
    return video[0], audio[0]


@main_guard
def main() -> int:
    parser = base_parser(__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="Path to the raw recording. Read-only, never written.")
    parser.add_argument("--skip-hash", action="store_true",
                        help="Skip the initial full SHA-256 (large files). The "
                             "size and mtime sentinel still applies.")
    args = parser.parse_args()

    paths = RunPaths(args.runs_root, args.run_id).ensure()
    source = assert_source_usable(args.source, paths.root)

    banner(f"Stage 01 — source manifest [{args.run_id}]")
    report("source", source)

    probe = ffprobe_json(source, log_path=paths.logs / f"{STAGE}.log")
    video, audio = pick_streams(probe)

    fps = parse_rate(video.get("r_frame_rate") or video.get("avg_frame_rate"))
    if fps <= 0:
        raise PipelineError(f"Could not determine a usable frame rate: {video.get('r_frame_rate')}")

    duration = float(probe["format"]["duration"])

    print("  hashing source (read-only)...", flush=True)
    sentinel = SourceSentinel.capture(source, hash_content=not args.skip_hash)

    manifest = {
        "schema": "vpipe.manifest/1",
        "run_id": args.run_id,
        "sentinel": sentinel.to_dict(),
        "duration": duration,
        "fps": f"{fps.numerator}/{fps.denominator}",
        "fps_float": float(fps),
        "video": {
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": video.get("width"),
            "height": video.get("height"),
            "pix_fmt": video.get("pix_fmt"),
            "bit_rate": video.get("bit_rate"),
            "nb_frames": video.get("nb_frames"),
            "color_space": video.get("color_space"),
            "rotation": video.get("side_data_list", [{}])[0].get("rotation"),
        },
        "audio": {
            "codec": audio.get("codec_name"),
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
            "channel_layout": audio.get("channel_layout"),
            "bit_rate": audio.get("bit_rate"),
        },
        "format": {
            "format_name": probe["format"].get("format_name"),
            "size": probe["format"].get("size"),
            "bit_rate": probe["format"].get("bit_rate"),
            "nb_streams": probe["format"].get("nb_streams"),
        },
        "tools": tool_versions(),
        "probe_raw": probe,
    }
    write_json(paths.source / "manifest.json", manifest, paths.root)

    report("duration", f"{format_hms(duration)}  ({duration:.3f}s)")
    report("frame rate", f"{fps} ({float(fps):.3f} fps)")
    report("resolution", f"{video.get('width')}x{video.get('height')} {video.get('pix_fmt')}")
    report("video codec", video.get("codec_name"))
    report("audio", f"{audio.get('codec_name')} {audio.get('sample_rate')}Hz "
                    f"{audio.get('channel_layout')}")
    report("sha256", sentinel.sha256 or "(skipped)")

    state = RunState(paths)
    state.record(STAGE, source=str(source), duration=duration,
                 fps=manifest["fps"], sha256=sentinel.sha256)
    print(f"\n  manifest -> {paths.source / 'manifest.json'}")
    print("  source untouched. Next: bin/02_audio.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
