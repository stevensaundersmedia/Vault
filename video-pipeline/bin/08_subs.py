#!/usr/bin/env python3
"""Stage 08 — subtitles.

Cues are built from the source word timestamps mapped through the segment map,
not from a fresh transcription of the review. That keeps the subtitles locked
to the same timing model as the cut: a word that was removed cannot appear,
and a word that survived lands exactly where the render puts it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.guards import write_text
from vpipe.segments import EDL
from vpipe.timecode import format_timestamp
from vpipe.words import Transcript, Word

STAGE = "08_subs"


def greedy_lines(text: str, max_chars: int) -> list[str]:
    """Break text into lines of at most max_chars, never splitting a word.

    A single word longer than the limit gets its own over-long line: the
    alternative is dropping or hyphenating something that was actually said.
    """
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fits(text: str, cfg: dict) -> bool:
    """Whether text can be shown within the configured line budget."""
    return len(greedy_lines(text, cfg["max_chars_per_line"])) <= cfg["max_lines"]


def balanced_chunks(entries: list, parts: int) -> list[list]:
    """Split words into `parts` groups of roughly equal character length.

    Balancing matters: a naive greedy split leaves an orphan cue holding one
    word for a fraction of a second, which reads badly on screen.
    """
    if parts <= 1 or len(entries) <= 1:
        return [entries]
    lengths = [len(w.text) + 1 for w, _, _ in entries]
    target = sum(lengths) / parts
    chunks, current, running, remaining = [], [], 0.0, parts
    for entry, length in zip(entries, lengths):
        current.append(entry)
        running += length
        words_left = len(entries) - sum(len(c) for c in chunks) - len(current)
        if running >= target and remaining > 1 and words_left >= remaining - 1:
            chunks.append(current)
            current, running, remaining = [], 0.0, remaining - 1
    if current:
        chunks.append(current)
    return chunks


def build_cues(transcript: Transcript, edl: EDL, cfg: dict) -> list[dict]:
    """Map every surviving word onto the output timeline and group into cues."""
    placed: list[tuple[Word, float, float]] = []
    for word in transcript.words:
        out_start = edl.src_to_out(word.start)
        if out_start is None:
            continue  # this word was cut
        out_end = edl.src_to_out(max(word.start, word.end - 0.001))
        if out_end is None:
            # The word straddles a cut point; clamp it to the end of its segment.
            for seg in edl.segments:
                if seg.src_start <= word.start < seg.src_end:
                    out_end = seg.out_end
                    break
        placed.append((word, out_start, out_end or out_start + 0.1))

    # 1. Group into utterances: a sentence, or a run broken by a real pause.
    utterances: list[list] = []
    current: list = []
    for entry in placed:
        word, out_start, _ = entry
        if current and out_start - current[-1][2] > cfg["cue_split_gap_s"]:
            utterances.append(current)
            current = []
        current.append(entry)
        if word.ends_sentence:
            utterances.append(current)
            current = []
    if current:
        utterances.append(current)

    # 2. Split each utterance into the fewest chunks that fit the line budget
    #    and the maximum cue duration, balanced so no chunk is an orphan.
    cues: list[dict] = []
    for utterance in utterances:
        if not utterance:
            continue
        parts = 1
        while parts < len(utterance):
            chunks = balanced_chunks(utterance, parts)
            too_long = any(
                len(greedy_lines(" ".join(w.text for w, _, _ in chunk),
                                 cfg["max_chars_per_line"])) > cfg["max_lines"]
                or chunk[-1][2] - chunk[0][1] > cfg["max_cue_duration_s"]
                for chunk in chunks
            )
            if not too_long:
                break
            parts += 1
        for chunk in balanced_chunks(utterance, parts):
            text = " ".join(w.text for w, _, _ in chunk).strip()
            cues.append({
                "start": chunk[0][1],
                "end": max(chunk[-1][2], chunk[0][1] + 0.4),
                "text": "\n".join(greedy_lines(text, cfg["max_chars_per_line"])),
            })

    cues.sort(key=lambda c: c["start"])
    # Never let one cue overlap the next.
    for a, b in zip(cues, cues[1:]):
        if a["end"] > b["start"]:
            a["end"] = max(a["start"] + 0.3, b["start"] - 0.01)
    return cues


def to_srt(cues: list[dict]) -> str:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n"
            f"{format_timestamp(cue['start'])} --> {format_timestamp(cue['end'])}\n"
            f"{cue['text']}\n"
        )
    return "\n".join(blocks)


def to_vtt(cues: list[dict]) -> str:
    blocks = ["WEBVTT\n"]
    for cue in cues:
        blocks.append(
            f"{format_timestamp(cue['start'], sep='.')} --> "
            f"{format_timestamp(cue['end'], sep='.')}\n{cue['text']}\n"
        )
    return "\n".join(blocks)


@main_guard
def main() -> int:
    args = base_parser(__doc__).parse_args()
    ctx = StageContext(args)
    ctx.state.require("05_resolve", "boundary resolution")

    banner(f"Stage 08 — subtitles [{args.run_id}]")

    edl = EDL.load(ctx.paths.resolved / "edl.json")
    transcript = Transcript.load(ctx.paths.transcript / "words.json")
    cues = build_cues(transcript, edl, ctx.config["subtitles"])

    if not cues:
        from vpipe.errors import PipelineError
        raise PipelineError(
            "No subtitle cues were produced. Either the transcript and the EDL "
            "disagree about the source, or every word was cut."
        )

    srt = ctx.paths.out("subtitles", "review.srt")
    vtt = ctx.paths.out("subtitles", "review.vtt")
    write_text(srt, to_srt(cues), ctx.paths.root)
    write_text(vtt, to_vtt(cues), ctx.paths.root)

    overrun = [c for c in cues if c["end"] > edl.total_duration + 0.5]
    report("cues", len(cues))
    report("last cue ends", f"{cues[-1]['end']:.3f}s (output {edl.total_duration:.3f}s)")
    report("beyond output", len(overrun))
    report("srt", srt)
    report("vtt", vtt)

    ctx.sentinel.verify()
    ctx.state.record(STAGE, cues=len(cues), srt=str(srt), vtt=str(vtt))
    print("\n  Subtitles follow the same segment map as the cut.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
