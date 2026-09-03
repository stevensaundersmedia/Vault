#!/usr/bin/env python3
"""Stage 00 — preflight check.

Run this first on a new machine. It checks everything the pipeline needs,
names exactly what is missing, and gives the install command for your
platform. It reads nothing and writes nothing.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OK, MISSING, WARN = "ok", "MISSING", "warn"

INSTALL_HINTS = {
    "ffmpeg": {
        "darwin": "brew install ffmpeg",
        "linux": "sudo apt install ffmpeg     # or: sudo dnf install ffmpeg",
        "win32": "winget install Gyan.FFmpeg  # then reopen your terminal",
    },
}


def platform_key() -> str:
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("win"):
        return "win32"
    return "linux"


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []
        self.fixes: list[str] = []

    def add(self, name: str, status: str, detail: str, fix: str = "") -> None:
        self.rows.append((name, status, detail))
        marker = {OK: "ok     ", MISSING: "MISSING", WARN: "warn   "}[status]
        print(f"  [{marker}] {name:<22} {detail}")
        if fix and status != OK:
            self.fixes.append(fix)

    @property
    def blocked(self) -> bool:
        return any(status == MISSING for _, status, _ in self.rows)


def check_python(report: Report) -> None:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 11):
        report.add("python", OK, f"{text}")
    else:
        report.add("python", MISSING,
                   f"{text} — 3.11+ is required (the config parser needs tomllib)",
                   "Install Python 3.11 or newer and re-run with it.")


def check_tool(report: Report, name: str) -> bool:
    found = shutil.which(name)
    if not found:
        hint = INSTALL_HINTS.get(name, {}).get(platform_key(), f"install {name}")
        report.add(name, MISSING, "not on PATH", hint)
        return False
    try:
        out = subprocess.run([name, "-version"], capture_output=True, text=True,
                             timeout=20)
        first = out.stdout.splitlines()[0] if out.stdout else ""
        version = first.split(" version ")[-1].split(" ")[0] if " version " in first else "?"
    except Exception:
        version = "?"
    report.add(name, OK, f"{version}  ({found})")
    return True


def check_encoders(report: Report) -> None:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        report.add("encoders", WARN, "could not query ffmpeg encoders")
        return
    missing = [codec for codec in ("libx264", "aac") if codec not in out]
    if missing:
        report.add("encoders", MISSING,
                   f"ffmpeg lacks: {', '.join(missing)}",
                   "Install a full ffmpeg build (the one shipped with some "
                   "minimal images omits libx264).")
    else:
        report.add("encoders", OK, "libx264, aac present")


def check_filters(report: Report) -> None:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        report.add("qa filters", WARN, "could not query ffmpeg filters")
        return
    needed = ("silencedetect", "blackdetect", "drawtext")
    missing = [f for f in needed if f not in out]
    if missing:
        report.add("qa filters", WARN,
                   f"missing {', '.join(missing)} — some QA checks will not run")
    else:
        report.add("qa filters", OK, "silencedetect, blackdetect, drawtext")


def check_asr(report: Report) -> None:
    from vpipe.asr import find_whisper_cpp, have_faster_whisper

    backends = []
    if have_faster_whisper():
        backends.append("faster-whisper")
    cpp = find_whisper_cpp()
    if cpp:
        backends.append(f"whisper.cpp ({cpp})")

    if backends:
        report.add("ASR backend", OK, ", ".join(backends))
    else:
        report.add("ASR backend", MISSING,
                   "neither faster-whisper nor whisper.cpp found",
                   "pip install faster-whisper\n"
                   "     # first run downloads the model (~3GB for large-v3);\n"
                   "     # try --model small.en first if you want a quick check")


def check_disk(report: Report, source: Path | None) -> None:
    runs_root = REPO_ROOT / "runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(runs_root).free
    free_gb = free / 1e9

    if source and source.exists():
        size_gb = source.stat().st_size / 1e9
        # Working audio, review, master and QA stills. Master dominates.
        needed = size_gb * 2.5 + 1.0
        status = OK if free_gb > needed else MISSING
        report.add("disk space", status,
                   f"{free_gb:.1f}GB free, ~{needed:.1f}GB needed for a "
                   f"{size_gb:.1f}GB source",
                   "Free up space or pass --runs-root pointing at a larger volume.")
    else:
        report.add("disk space", OK if free_gb > 5 else WARN,
                   f"{free_gb:.1f}GB free under runs/")


def check_source(report: Report, source: Path | None) -> None:
    if source is None:
        report.add("source file", WARN,
                   "not checked — pass --source to validate your recording")
        return
    if not source.exists():
        report.add("source file", MISSING, f"not found: {source}",
                   "Check the path.")
        return

    from vpipe.ffmpeg_run import ffprobe_json
    try:
        probe = ffprobe_json(source)
    except Exception as exc:
        report.add("source file", MISSING, f"ffprobe could not read it: {exc}")
        return

    video = [s for s in probe["streams"] if s.get("codec_type") == "video"
             and s.get("disposition", {}).get("attached_pic", 0) == 0]
    audio = [s for s in probe["streams"] if s.get("codec_type") == "audio"]
    duration = float(probe["format"].get("duration", 0))
    size_gb = source.stat().st_size / 1e9

    detail = (f"{duration / 60:.1f} min, {size_gb:.2f}GB, "
              f"{len(video)} video / {len(audio)} audio stream(s)")
    if len(video) == 1 and len(audio) == 1:
        report.add("source file", OK, detail)
    else:
        report.add("source file", MISSING,
                   detail + " — version one needs exactly one of each",
                   "Extract the track you want first, or wait for multi-track "
                   "support.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=None,
                        help="Optionally validate your recording too.")
    args = parser.parse_args()

    print("\n=== Preflight check ===")
    report = Report()

    check_python(report)
    have_ffmpeg = check_tool(report, "ffmpeg")
    check_tool(report, "ffprobe")
    if have_ffmpeg:
        check_encoders(report)
        check_filters(report)
    check_asr(report)
    check_disk(report, args.source)
    if have_ffmpeg:
        check_source(report, args.source)

    print()
    if report.blocked:
        print("  Not ready. Fix these first:\n")
        for fix in report.fixes:
            print(f"     {fix}")
        print()
        return 1

    print("  Ready. Next:\n")
    print("     bin/run_to_brief.sh /path/to/your-recording.mp4\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
