"""Rendering an EDL, shared by the review and the master.

Both renders are produced by this one function from the same EDL, differing
only in scale and encoder settings. That is deliberate: it makes it
structurally impossible for the master to be a different edit from the review
the approval was given for.

The whole edit is one ffmpeg invocation. Each retained span is trimmed from
the source inside a single filtergraph and the spans are concatenated there,
so the audio timeline is conformed continuously in one pass rather than
stitched together from separately encoded pieces.
"""

from __future__ import annotations

from pathlib import Path

from .ffmpeg_run import run_ffmpeg
from .segments import EDL


def build_filtergraph(edl: EDL, *, height: int | None, fade_ms: int) -> str:
    """Trim every retained span and concatenate, video and audio together.

    The short fades are applied per span before the concat. They exist only to
    stop the waveform jumping at a join, which clicks; at 20ms they are below
    the threshold of being heard as a fade.
    """
    fade = max(0.0, fade_ms / 1000.0)
    chains: list[str] = []
    labels: list[str] = []

    for seg in edl.segments:
        i = seg.index
        duration = seg.duration

        video = [
            f"[0:v]trim=start={seg.src_start:.6f}:end={seg.src_end:.6f}",
            "setpts=PTS-STARTPTS",
        ]
        if height:
            # -2 keeps the aspect ratio and forces an even width, which H.264
            # requires; setsar normalises anamorphic sources.
            video.append(f"scale=-2:{height}:flags=bicubic")
            video.append("setsar=1")
        chains.append(",".join(video) + f"[v{i}]")

        audio = [
            f"[0:a]atrim=start={seg.src_start:.6f}:end={seg.src_end:.6f}",
            "asetpts=PTS-STARTPTS",
        ]
        # A fade is only meaningful if the span is comfortably longer than it.
        if fade > 0 and duration > fade * 3:
            audio.append(f"afade=t=in:st=0:d={fade:.4f}")
            audio.append(f"afade=t=out:st={duration - fade:.6f}:d={fade:.4f}")
        chains.append(",".join(audio) + f"[a{i}]")

        labels.append(f"[v{i}][a{i}]")

    chains.append(
        "".join(labels) + f"concat=n={len(edl.segments)}:v=1:a=1[vout][aout]"
    )
    return ";".join(chains)


def render_edit(
    edl: EDL,
    source: Path,
    output: Path,
    *,
    run_dir: Path,
    profile: dict,
    fade_ms: int,
    height: int | None,
    log_path: Path | None = None,
) -> Path:
    """Render the EDL to a single file in one ffmpeg pass."""
    graph = build_filtergraph(edl, height=height, fade_ms=fade_ms)

    args = [
        "-i", str(source),
        "-filter_complex", graph,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", profile["video_codec"],
        "-crf", str(profile["crf"]),
        "-preset", profile["preset"],
        "-pix_fmt", "yuv420p",
        "-c:a", profile["audio_codec"],
        "-b:a", profile["audio_bitrate"],
        "-movflags", "+faststart",
        str(output),
    ]
    run_ffmpeg(args, outputs=[output], run_dir=run_dir, log_path=log_path)
    return output
