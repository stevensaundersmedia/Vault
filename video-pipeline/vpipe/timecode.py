"""Frame-accurate time arithmetic.

Every time value in the pipeline is a float of seconds in the source timebase.
Conversion to and from frames happens only here, so rounding behaviour is
identical in the resolver, the renderer, the QA stage and the subtitles.
"""

from __future__ import annotations

from fractions import Fraction


def parse_rate(rate: str | float | int) -> Fraction:
    """Parse an ffprobe rate string such as '30000/1001' into an exact Fraction.

    Frame rates are kept as fractions rather than floats because 29.97 and
    23.976 accumulate visible error over a long timeline when rounded early.
    """
    if isinstance(rate, Fraction):
        return rate
    if isinstance(rate, (int, float)):
        return Fraction(rate).limit_denominator(1000000)
    text = str(rate).strip()
    if "/" in text:
        num, _, den = text.partition("/")
        num, den = int(num), int(den)
        if den == 0:
            raise ValueError(f"Frame rate has zero denominator: {rate!r}")
        return Fraction(num, den)
    return Fraction(text).limit_denominator(1000000)


def seconds_to_frame(t: float, fps: Fraction) -> int:
    """Index of the frame displayed at time t."""
    return int(Fraction(t).limit_denominator(1000000) * fps)


def frame_to_seconds(frame: int, fps: Fraction) -> float:
    return float(Fraction(frame) / fps)


def snap_down(t: float, fps: Fraction) -> float:
    """Snap to the start of the frame containing t.

    Used for segment starts: the cut lands on a frame boundary at or before the
    intended time, so no part of the kept speech is clipped.
    """
    if t <= 0:
        return 0.0
    exact = Fraction(t).limit_denominator(1000000) * fps
    frame = int(exact)
    return float(Fraction(frame) / fps)


def snap_up(t: float, fps: Fraction) -> float:
    """Snap to the next frame boundary at or after t.

    Used for segment ends, so the final frame of kept speech is included whole.
    """
    if t <= 0:
        return 0.0
    exact = Fraction(t).limit_denominator(1000000) * fps
    frame = int(exact)
    if Fraction(frame) != exact:
        frame += 1
    return float(Fraction(frame) / fps)


def format_timestamp(seconds: float, *, sep: str = ",") -> str:
    """SRT ('hh:mm:ss,mmm') or WebVTT ('hh:mm:ss.mmm') timestamp."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def format_hms(seconds: float) -> str:
    """Compact human-readable stamp for reports, e.g. '01:23:04.512'."""
    return format_timestamp(seconds, sep=".")
