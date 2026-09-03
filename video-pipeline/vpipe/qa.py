"""QA primitives: ffmpeg analysis parsing and check bookkeeping."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg_run import run_ffmpeg, run_ffmpeg_capture

PASS, FAIL, WARN, BLOCKED = "PASS", "FAIL", "WARN", "BLOCKED"

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")
_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")


@dataclass
class Check:
    """One QA assertion and its outcome."""

    name: str
    status: str
    detail: str
    data: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        """FAIL and BLOCKED both stop approval. BLOCKED means the check could
        not be run at all, which is not the same as passing it."""
        return self.status in (FAIL, BLOCKED)


class CheckList:
    def __init__(self):
        self.checks: list[Check] = []

    def add(self, name: str, status: str, detail: str, **data) -> Check:
        check = Check(name, status, detail, data)
        self.checks.append(check)
        marker = {PASS: "ok  ", FAIL: "FAIL", WARN: "warn", BLOCKED: "BLOCKED"}[status]
        print(f"  [{marker}] {name}: {detail}")
        return check

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.blocking]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def verdict(self) -> str:
        if self.blocking:
            return FAIL
        return WARN if self.warnings else PASS


def detect_silence(media: Path, *, threshold_db: float, min_duration: float,
                   log_path: Path | None = None) -> list[dict]:
    """Silence intervals, via the silencedetect filter."""
    stderr = run_ffmpeg_capture(
        ["-i", str(media),
         "-af", f"silencedetect=noise={threshold_db}dB:d={min_duration}",
         "-f", "null", "-"],
        log_path=log_path,
    )
    intervals: list[dict] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start = _SILENCE_START.search(line)
        if start:
            pending = float(start.group(1))
        end = _SILENCE_END.search(line)
        if end:
            stop, duration = float(end.group(1)), float(end.group(2))
            intervals.append({
                "start": pending if pending is not None else stop - duration,
                "end": stop,
                "duration": duration,
            })
            pending = None
    return intervals


def detect_black(media: Path, *, min_duration: float, pixel_threshold: float,
                 log_path: Path | None = None) -> list[dict]:
    """Black intervals, via the blackdetect filter."""
    stderr = run_ffmpeg_capture(
        ["-i", str(media),
         "-vf", f"blackdetect=d={min_duration}:pic_th=0.98:pix_th={pixel_threshold}",
         "-an", "-f", "null", "-"],
        log_path=log_path,
    )
    return [
        {"start": float(m.group(1)), "end": float(m.group(2)),
         "duration": float(m.group(3))}
        for m in (_BLACK.search(line) for line in stderr.splitlines()) if m
    ]


def decode_check(media: Path, log_path: Path | None = None) -> str:
    """Decode every packet and return whatever ffmpeg complained about.

    An empty string means the file decodes cleanly end to end.
    """
    return run_ffmpeg_capture(
        ["-v", "error", "-i", str(media), "-f", "null", "-"],
        log_path=log_path,
    ).strip()


def extract_frame(media: Path, at: float, out: Path, *, run_dir: Path,
                  height: int = 360, label: str = "",
                  log_path: Path | None = None) -> Path:
    """Single frame at a given time, optionally captioned."""
    filters = [f"scale=-2:{height}"]
    if label:
        safe = label.replace("\\", "").replace(":", "\\:").replace("'", "")
        filters.append(
            f"drawtext=text='{safe}':fontcolor=white:fontsize=20:x=8:y=8:"
            f"box=1:boxcolor=black@0.65:boxborderw=6"
        )
    run_ffmpeg(
        ["-ss", f"{max(0.0, at):.4f}", "-i", str(media), "-frames:v", "1",
         "-vf", ",".join(filters), "-q:v", "3", str(out)],
        outputs=[out], run_dir=run_dir, log_path=log_path,
    )
    return out


def hstack(images: list[Path], out: Path, *, run_dir: Path,
           log_path: Path | None = None) -> Path:
    """Join frames side by side into one strip."""
    args: list[str] = []
    for image in images:
        args += ["-i", str(image)]
    args += ["-filter_complex", f"hstack=inputs={len(images)}", str(out)]
    run_ffmpeg(args, outputs=[out], run_dir=run_dir, log_path=log_path)
    return out
