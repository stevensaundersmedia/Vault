"""Source protection and run-directory containment.

Safety rule 1 (never modify, move or delete source media) and safety rule 2
(write only into runs/<run-id>/) are enforced here rather than left to the
discipline of individual stage scripts.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

from .errors import SafetyViolation

_HASH_CHUNK = 8 * 1024 * 1024


def sha256_file(path: Path) -> str:
    """Full SHA-256 of a file. Read-only, streamed, never mmap'd."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceSentinel:
    """An immutability witness for the source file.

    Captured before the first stage touches the media and re-verified at every
    stage boundary. If any field moves, the run stops: something in this
    pipeline, or something outside it, has written to the source.
    """

    path: str
    size: int
    mtime_ns: int
    sha256: str

    @classmethod
    def capture(cls, path: Path, *, hash_content: bool = True) -> "SourceSentinel":
        stat = path.stat()
        return cls(
            path=str(path.resolve()),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=sha256_file(path) if hash_content else "",
        )

    def verify(self, *, deep: bool = False) -> None:
        """Re-check the source. `deep` re-hashes the whole file; the cheap check
        of size plus mtime runs at every stage boundary."""
        path = Path(self.path)
        if not path.exists():
            raise SafetyViolation(f"Source has disappeared since the run began: {path}")
        stat = path.stat()
        if stat.st_size != self.size:
            raise SafetyViolation(
                f"Source size changed ({self.size} -> {stat.st_size}): {path}"
            )
        if stat.st_mtime_ns != self.mtime_ns:
            raise SafetyViolation(
                f"Source modification time changed, source has been written to: {path}"
            )
        if deep and self.sha256:
            current = sha256_file(path)
            if current != self.sha256:
                raise SafetyViolation(
                    f"Source content hash changed ({self.sha256[:12]} -> "
                    f"{current[:12]}): {path}"
                )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SourceSentinel":
        return cls(**data)


def assert_source_usable(source: Path, run_dir: Path) -> Path:
    """Validate a source path before anything reads it.

    Refuses a source that lives inside the run directory, because everything
    under the run directory is treated as disposable output and a later stage
    would be entitled to overwrite it.
    """
    source = source.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()

    if not source.exists():
        raise SafetyViolation(f"Source does not exist: {source}")
    if not source.is_file():
        raise SafetyViolation(f"Source is not a regular file: {source}")
    if not os.access(source, os.R_OK):
        raise SafetyViolation(f"Source is not readable: {source}")
    if source.is_relative_to(run_dir):
        raise SafetyViolation(
            f"Source lives inside the run directory and would be treated as "
            f"disposable output: {source}"
        )
    return source


def assert_contained(path: Path, run_dir: Path, *, what: str = "output") -> Path:
    """Every file the pipeline writes must resolve inside the run directory.

    Resolution happens before the check, so `runs/x/../../etc/passwd` and
    symlinks pointing outside are both caught.
    """
    resolved = Path(path).expanduser().resolve()
    run_dir = Path(run_dir).expanduser().resolve()
    if not resolved.is_relative_to(run_dir):
        raise SafetyViolation(
            f"Refusing to write {what} outside the run directory.\n"
            f"  target:  {resolved}\n"
            f"  run dir: {run_dir}"
        )
    return resolved


class RunPaths:
    """The fixed layout of runs/<run-id>/.

    Stage scripts ask this object for paths instead of joining strings, so
    there is exactly one place where the layout is defined.
    """

    LAYOUT = {
        "source": "00_source",
        "audio": "10_audio",
        "transcript": "20_transcript",
        "decisions": "30_decisions",
        "resolved": "35_resolved",
        "review": "40_review",
        "qa": "50_qa",
        "subtitles": "60_subtitles",
        "master": "70_master",
        "upload": "90_upload",
        "approvals": "APPROVALS",
        "logs": "logs",
    }

    def __init__(self, runs_root: Path, run_id: str):
        if not run_id or "/" in run_id or run_id.startswith("."):
            raise SafetyViolation(f"Unsafe run id: {run_id!r}")
        self.runs_root = Path(runs_root).expanduser().resolve()
        self.run_id = run_id
        self.root = self.runs_root / run_id

    def __getattr__(self, name: str) -> Path:
        if name in RunPaths.LAYOUT:
            return self.root / RunPaths.LAYOUT[name]
        raise AttributeError(name)

    def ensure(self) -> "RunPaths":
        for sub in RunPaths.LAYOUT.values():
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        return self

    def out(self, area: str, filename: str) -> Path:
        """A contained output path inside one area of the run directory."""
        target = getattr(self, area) / filename
        return assert_contained(target, self.root)

    @property
    def state_file(self) -> Path:
        return self.root / "RUN_STATE.json"


def write_json(path: Path, data, run_dir: Path) -> Path:
    """Atomic, contained JSON write."""
    target = assert_contained(path, run_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def write_text(path: Path, text: str, run_dir: Path) -> Path:
    """Atomic, contained text write."""
    target = assert_contained(path, run_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return target
