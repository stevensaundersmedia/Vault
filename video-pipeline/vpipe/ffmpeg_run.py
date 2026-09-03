"""The only place the pipeline is allowed to invoke ffmpeg or ffprobe.

Routing every invocation through here means the containment check on output
paths cannot be forgotten by a stage script, and every command plus its full
stderr lands in the run log for audit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .errors import MissingDependency, PipelineError
from .guards import assert_contained


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MissingDependency(
            f"`{name}` was not found on PATH. Install ffmpeg (which provides both "
            f"ffmpeg and ffprobe) and re-run this stage."
        )
    return path


def _log(log_path: Path | None, text: str) -> None:
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(text)


def ffprobe_json(path: Path, *, log_path: Path | None = None) -> dict:
    """Full stream and format metadata for a media file. Read-only by nature."""
    require_tool("ffprobe")
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams", "-show_chapters",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    _log(log_path, f"\n$ {' '.join(cmd)}\n{result.stderr}")
    if result.returncode != 0:
        raise PipelineError(f"ffprobe failed on {path}:\n{result.stderr.strip()}")
    return json.loads(result.stdout)


def run_ffmpeg(
    args: Sequence[str],
    *,
    outputs: Iterable[Path],
    run_dir: Path,
    log_path: Path | None = None,
    expect_empty_stderr: bool = False,
) -> subprocess.CompletedProcess:
    """Run ffmpeg with every declared output verified as contained.

    `outputs` must list every path the command writes. It is checked against
    the run directory before the process starts, so a mistyped output path
    fails loudly instead of landing next to the source media.
    """
    require_tool("ffmpeg")
    checked = [assert_contained(Path(o), run_dir) for o in outputs]
    for target in checked:
        target.parent.mkdir(parents=True, exist_ok=True)

    # -nostdin stops ffmpeg consuming the terminal; -y is safe because every
    # output has just been proven to live inside the disposable run directory.
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args]
    stamp = datetime.now(timezone.utc).isoformat()
    _log(log_path, f"\n# {stamp}\n$ {' '.join(cmd)}\n")

    result = subprocess.run(cmd, capture_output=True, text=True)
    _log(log_path, result.stderr)

    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-25:])
        raise PipelineError(f"ffmpeg failed (exit {result.returncode}):\n{tail}")
    if expect_empty_stderr and result.stderr.strip():
        raise PipelineError(
            "ffmpeg reported errors during a decode check:\n"
            + "\n".join(result.stderr.strip().splitlines()[-25:])
        )
    return result


def run_ffmpeg_capture(
    args: Sequence[str], *, log_path: Path | None = None
) -> str:
    """Run an analysis-only ffmpeg command that writes no file and return stderr.

    Used for the filters that report through the log rather than an output
    file (silencedetect, blackdetect) and for the null-muxer decode check.
    Callers must pass `-f null -` so nothing is written.
    """
    require_tool("ffmpeg")
    if "null" not in args:
        raise PipelineError(
            "run_ffmpeg_capture is only for analysis commands writing to the "
            "null muxer; use run_ffmpeg for anything that produces a file."
        )
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", *args]
    _log(log_path, f"\n$ {' '.join(cmd)}\n")
    result = subprocess.run(cmd, capture_output=True, text=True)
    _log(log_path, result.stderr)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-25:])
        raise PipelineError(f"ffmpeg analysis failed (exit {result.returncode}):\n{tail}")
    return result.stderr


def tool_versions() -> dict:
    """Recorded in the manifest so a run can be reproduced against the same tools."""
    versions = {}
    for tool in ("ffmpeg", "ffprobe"):
        try:
            require_tool(tool)
        except MissingDependency:
            versions[tool] = None
            continue
        out = subprocess.run([tool, "-version"], capture_output=True, text=True)
        versions[tool] = out.stdout.splitlines()[0] if out.stdout else None
    return versions
