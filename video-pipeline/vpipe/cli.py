"""Shared entry-point plumbing for the stage scripts.

Every stage takes the same core arguments, loads the same config, and
re-verifies the source sentinel before doing any work. Centralising that means
a stage cannot accidentally skip the source-integrity check.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from .errors import PipelineError
from .guards import RunPaths, SourceSentinel
from .runstate import RunState

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "pipeline.toml"
DEFAULT_RUNS_ROOT = REPO_ROOT / "runs"


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--run-id", required=True, help="Identifier for this run.")
    parser.add_argument(
        "--runs-root", type=Path, default=DEFAULT_RUNS_ROOT,
        help="Directory holding all runs (default: video-pipeline/runs).",
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG,
        help="Pipeline configuration TOML.",
    )
    return parser


def load_config(path: Path) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


class StageContext:
    """Everything a stage needs, with the source already re-verified."""

    def __init__(self, args, *, needs_source: bool = True, deep_verify: bool = False):
        self.args = args
        self.config = load_config(args.config)
        self.paths = RunPaths(args.runs_root, args.run_id)
        self.state = RunState(self.paths)
        self.manifest = None
        self.sentinel = None
        if needs_source:
            self._load_source(deep_verify)

    def _load_source(self, deep: bool) -> None:
        manifest_file = self.paths.source / "manifest.json"
        if not manifest_file.exists():
            raise PipelineError(
                f"No source manifest for run `{self.paths.run_id}`. "
                f"Run bin/01_manifest.py first."
            )
        self.manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.sentinel = SourceSentinel.from_dict(self.manifest["sentinel"])
        # Safety rule 1, checked again at every stage boundary rather than once.
        self.sentinel.verify(deep=deep)

    @property
    def source_path(self) -> Path:
        return Path(self.sentinel.path)

    def log(self, stage: str) -> Path:
        return self.paths.logs / f"{stage}.log"


def main_guard(func):
    """Turn a typed refusal into a clean message and exit code.

    Refusals are an expected outcome of this pipeline, not a crash, so they
    print an explanation rather than a traceback.
    """

    def wrapper():
        try:
            return func() or 0
        except PipelineError as exc:
            label = type(exc).__name__
            print(f"\n[{label}] {exc}\n", file=sys.stderr)
            return exc.exit_code

    return wrapper


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def report(label: str, value) -> None:
    print(f"  {label:<28} {value}")
