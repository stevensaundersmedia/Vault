"""The edit decision list and the source-to-output segment map.

One EDL is the single source of truth for the review render, the master
render, the QA expectations and the subtitle timing. Nothing downstream
re-derives timings from anything else, so the review and the master are
guaranteed to be the same edit at two resolutions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from fractions import Fraction
from pathlib import Path

from .errors import AmbiguityStop, PipelineError
from .timecode import format_hms


@dataclass
class Segment:
    """One retained span, in both source and output time."""

    index: int
    src_start: float
    src_end: float
    out_start: float
    out_end: float
    reason: str = ""
    keep_id: str = ""
    start_quote: str = ""
    end_quote: str = ""
    first_word: int = -1
    last_word: int = -1

    @property
    def duration(self) -> float:
        return self.src_end - self.src_start


@dataclass
class EDL:
    """An ordered, validated, non-overlapping set of retained spans."""

    source: str
    source_sha256: str
    fps: str
    source_duration: float
    segments: list[Segment] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.segments)

    @property
    def joins(self) -> list[dict]:
        """Every point where two retained spans meet, in output time.

        These are the only places the edit can betray itself, so QA treats
        them as first-class objects rather than deriving them ad hoc.
        """
        result = []
        for left, right in zip(self.segments, self.segments[1:]):
            result.append({
                "join_index": left.index,
                "out_time": left.out_end,
                "src_out_point": left.src_end,
                "src_in_point": right.src_start,
                "removed_duration": right.src_start - left.src_end,
                "before_quote": left.end_quote,
                "after_quote": right.start_quote,
            })
        return result

    def src_to_out(self, t: float) -> float | None:
        """Map a source time onto the output timeline.

        Returns None for time that was cut, which is how the subtitle stage
        knows to drop a word rather than place it wrongly.
        """
        for seg in self.segments:
            if seg.src_start <= t < seg.src_end:
                return seg.out_start + (t - seg.src_start)
        return None

    def validate(self, *, min_segment_s: float) -> None:
        """Structural checks. Any failure stops the run.

        These are the guarantees the renderer relies on: ordered, disjoint,
        inside the source, and long enough to be a deliberate edit.
        """
        if not self.segments:
            raise AmbiguityStop(
                "The edit decision list is empty: every span was removed. "
                "Refusing to render an empty timeline."
            )
        previous_end = -1.0
        for seg in self.segments:
            if seg.src_end <= seg.src_start:
                raise AmbiguityStop(
                    f"Segment {seg.index} ({seg.keep_id}) is empty or inverted: "
                    f"{format_hms(seg.src_start)} -> {format_hms(seg.src_end)}."
                )
            if seg.duration < min_segment_s:
                raise AmbiguityStop(
                    f"Segment {seg.index} ({seg.keep_id}) is only "
                    f"{seg.duration:.3f}s, below the {min_segment_s}s floor. "
                    f"A span this short is almost certainly a mis-placed anchor "
                    f"rather than an intended cut. Not guessing."
                )
            if seg.src_start < previous_end:
                raise AmbiguityStop(
                    f"Segment {seg.index} ({seg.keep_id}) starts at "
                    f"{format_hms(seg.src_start)}, before the previous segment "
                    f"ended at {format_hms(previous_end)}. Retained spans must "
                    f"be ordered and must not overlap."
                )
            if seg.src_start < 0 or seg.src_end > self.source_duration + 0.5:
                raise AmbiguityStop(
                    f"Segment {seg.index} ({seg.keep_id}) runs to "
                    f"{format_hms(seg.src_end)}, outside the source duration "
                    f"of {format_hms(self.source_duration)}."
                )
            previous_end = seg.src_end

    def to_dict(self) -> dict:
        return {
            "schema": "vpipe.edl/1",
            "source": self.source,
            "source_sha256": self.source_sha256,
            "fps": self.fps,
            "source_duration": self.source_duration,
            "output_duration": self.total_duration,
            "segment_count": len(self.segments),
            "removed_duration": self.source_duration - self.total_duration,
            "config": self.config,
            "segments": [asdict(s) for s in self.segments],
        }

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "EDL":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != "vpipe.edl/1":
            raise PipelineError(f"Unexpected EDL schema in {path}")
        return cls(
            source=data["source"],
            source_sha256=data["source_sha256"],
            fps=data["fps"],
            source_duration=data["source_duration"],
            segments=[Segment(**s) for s in data["segments"]],
            config=data.get("config", {}),
        )

    def segment_map_csv(self) -> str:
        """The human-auditable source-to-output map.

        One row per retained span: where it came from, where it landed, and the
        editorial reason it survived.
        """
        rows = [
            "segment,keep_id,src_in,src_out,src_in_hms,src_out_hms,"
            "out_in,out_out,out_in_hms,out_out_hms,duration_s,reason"
        ]
        for seg in self.segments:
            reason = seg.reason.replace('"', "'")
            rows.append(
                f"{seg.index},{seg.keep_id},"
                f"{seg.src_start:.3f},{seg.src_end:.3f},"
                f"{format_hms(seg.src_start)},{format_hms(seg.src_end)},"
                f"{seg.out_start:.3f},{seg.out_end:.3f},"
                f"{format_hms(seg.out_start)},{format_hms(seg.out_end)},"
                f"{seg.duration:.3f},\"{reason}\""
            )
        return "\n".join(rows) + "\n"


def build_output_timeline(segments: list[Segment]) -> list[Segment]:
    """Lay retained spans end to end and stamp their output times."""
    cursor = 0.0
    for position, seg in enumerate(segments):
        seg.index = position
        seg.out_start = cursor
        cursor += seg.duration
        seg.out_end = cursor
    return segments
