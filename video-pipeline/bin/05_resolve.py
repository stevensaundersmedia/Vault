#!/usr/bin/env python3
"""Stage 05 — resolve quoted anchors to frame-exact times.

This is the boundary between judgement and arithmetic. The editorial file says
what to keep, in words. This stage decides exactly when, using nothing but the
ASR word timestamps and the source frame rate. No model runs here.

It stops rather than guessing when an anchor is not unique, is not on a
sentence boundary, or produces a span that cannot be an intended edit.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.decisions import load_decisions
from vpipe.errors import AmbiguityStop
from vpipe.guards import write_text
from vpipe.segments import EDL, Segment, build_output_timeline
from vpipe.timecode import format_hms, parse_rate, snap_down, snap_up
from vpipe.words import Transcript

STAGE = "05_resolve"


def check_sentence_boundaries(transcript: Transcript, start_idx: int, end_idx: int,
                              keep_id: str, allow_nonterminal: bool) -> None:
    """Version one cuts on sentence boundaries only.

    A start that lands mid-sentence clips the front off a thought; an end that
    lands mid-sentence cuts a speaker off. Either means the anchor is wrong,
    so the run stops instead of producing an edit that sounds broken.
    """
    if allow_nonterminal:
        return

    start_word = transcript.by_index(start_idx)
    if start_idx > 0:
        previous = transcript.by_index(start_idx - 1)
        if not previous.ends_sentence:
            raise AmbiguityStop(
                f"`{keep_id}`: the start anchor does not begin a sentence.\n"
                f"  preceding word: {previous.text!r} (no terminal punctuation)\n"
                f"  anchor starts:  {start_word.text!r} at {format_hms(start_word.start)}\n"
                f"  Cutting in here would clip the front off a thought. Extend "
                f"the start_quote back to the start of the sentence. Not guessing."
            )

    end_word = transcript.by_index(end_idx)
    if not end_word.ends_sentence and end_idx < len(transcript.words) - 1:
        raise AmbiguityStop(
            f"`{keep_id}`: the end anchor does not finish a sentence.\n"
            f"  anchor ends: {end_word.text!r} at {format_hms(end_word.end)}\n"
            f"  next word:   {transcript.by_index(end_idx + 1).text!r}\n"
            f"  Cutting here would cut the speaker off mid-sentence. Extend the "
            f"end_quote to the end of the sentence. Not guessing."
        )


def resolve_times(transcript: Transcript, start_idx: int, end_idx: int,
                  cuts: dict, source_duration: float) -> tuple[float, float]:
    """Place the cut points inside the silence surrounding the retained words.

    Padding is a ceiling, not a fixed offset: the cut never takes more than
    half the available gap, so it cannot eat into the neighbouring speech even
    when the speaker barely paused.
    """
    first = transcript.by_index(start_idx)
    last = transcript.by_index(end_idx)

    if start_idx > 0:
        gap_before = max(0.0, first.start - transcript.by_index(start_idx - 1).end)
        lead = min(cuts["lead_pad_s"], gap_before / 2.0)
    else:
        lead = min(cuts["lead_pad_s"], first.start)

    if end_idx < len(transcript.words) - 1:
        gap_after = max(0.0, transcript.by_index(end_idx + 1).start - last.end)
        tail = min(cuts["tail_pad_s"], gap_after / 2.0)
    else:
        tail = min(cuts["tail_pad_s"], max(0.0, source_duration - last.end))

    return max(0.0, first.start - lead), min(source_duration, last.end + tail)


@main_guard
def main() -> int:
    args = base_parser(__doc__).parse_args()
    ctx = StageContext(args)
    ctx.state.require("03_transcribe", "transcription")

    banner(f"Stage 05 — resolving edit boundaries [{args.run_id}]")

    cuts = ctx.config["cuts"]
    transcript = Transcript.load(ctx.paths.transcript / "words.json")
    keeps, raw = load_decisions(ctx.paths.decisions / "decisions.json")
    fps = parse_rate(ctx.manifest["fps"])
    source_duration = ctx.manifest["duration"]

    report("keep spans", len(keeps))
    report("frame rate", f"{fps} ({float(fps):.3f} fps)")

    segments: list[Segment] = []
    previous_last_word = -1
    previous_id = ""

    for keep in keeps:
        start_idx, _ = transcript.find_quote(keep.start_quote, label=f"`{keep.id}`.start_quote")
        _, end_idx = transcript.find_quote(keep.end_quote, label=f"`{keep.id}`.end_quote")

        if end_idx < start_idx:
            raise AmbiguityStop(
                f"`{keep.id}`: the end anchor sits before the start anchor in the "
                f"transcript (word {end_idx} before word {start_idx}). The span "
                f"is inside out; the anchors are probably swapped or matched the "
                f"wrong repetition. Not guessing."
            )
        if start_idx <= previous_last_word:
            raise AmbiguityStop(
                f"`{keep.id}` starts at word {start_idx}, which is inside or "
                f"before `{previous_id}` (ended at word {previous_last_word}). "
                f"Retained spans must be in transcript order and must not "
                f"overlap. Version one does not reorder. Not guessing."
            )

        check_sentence_boundaries(transcript, start_idx, end_idx, keep.id,
                                  cuts["allow_nonterminal_anchors"])

        raw_start, raw_end = resolve_times(transcript, start_idx, end_idx, cuts,
                                           source_duration)
        # Snap outward so no retained frame is clipped at either end.
        src_start = snap_down(raw_start, fps)
        src_end = snap_up(raw_end, fps)

        segments.append(Segment(
            index=len(segments), src_start=src_start, src_end=src_end,
            out_start=0.0, out_end=0.0,
            reason=keep.reason, keep_id=keep.id,
            start_quote=keep.start_quote, end_quote=keep.end_quote,
            first_word=start_idx, last_word=end_idx,
        ))
        previous_last_word = end_idx
        previous_id = keep.id

    build_output_timeline(segments)

    edl = EDL(
        source=str(ctx.source_path),
        source_sha256=ctx.sentinel.sha256,
        fps=ctx.manifest["fps"],
        source_duration=source_duration,
        segments=segments,
        config={"cuts": cuts, "audio_join_fade_ms": ctx.config["audio"]["join_fade_ms"]},
    )
    edl.validate(min_segment_s=cuts["min_segment_s"])

    edl.save(ctx.paths.out("resolved", "edl.json"))
    write_text(ctx.paths.out("resolved", "segment_map.csv"),
               edl.segment_map_csv(), ctx.paths.root)

    # A plain-language account of what survives and what each join sounds like,
    # readable before anything is rendered.
    lines = [f"# Edit boundaries — {args.run_id}", "",
             f"Source {format_hms(source_duration)} -> "
             f"output {format_hms(edl.total_duration)} "
             f"({100 * edl.total_duration / source_duration:.1f}% retained), "
             f"{len(segments)} spans, {len(edl.joins)} joins.", "",
             "## Retained spans", ""]
    for seg in segments:
        lines += [
            f"### {seg.index:02d}  `{seg.keep_id}`  "
            f"{format_hms(seg.src_start)} -> {format_hms(seg.src_end)} "
            f"({seg.duration:.2f}s)",
            f"- reason: {seg.reason}",
            f"- text: {transcript.text_between(seg.src_start, seg.src_end)}",
            "",
        ]

    lines += ["## Removed", ""]
    cursor = 0.0
    for seg in segments:
        if seg.src_start - cursor > 0.05:
            lines.append(
                f"- {format_hms(cursor)} -> {format_hms(seg.src_start)} "
                f"({seg.src_start - cursor:.2f}s): "
                f"{transcript.text_between(cursor, seg.src_start) or '(silence)'}"
            )
        cursor = seg.src_end
    if source_duration - cursor > 0.05:
        lines.append(
            f"- {format_hms(cursor)} -> {format_hms(source_duration)} "
            f"({source_duration - cursor:.2f}s): "
            f"{transcript.text_between(cursor, source_duration) or '(silence)'}"
        )

    lines += ["", "## Joins", ""]
    for join in edl.joins:
        lines += [
            f"### join {join['join_index']} at output {format_hms(join['out_time'])}",
            f"- removes {join['removed_duration']:.2f}s "
            f"({format_hms(join['src_out_point'])} -> {format_hms(join['src_in_point'])})",
            f"- ...{join['before_quote']}  ||  {join['after_quote']}...",
            "",
        ]
    write_text(ctx.paths.out("resolved", "boundaries.md"), "\n".join(lines) + "\n",
               ctx.paths.root)

    report("output duration", f"{format_hms(edl.total_duration)} ({edl.total_duration:.3f}s)")
    report("removed", f"{format_hms(source_duration - edl.total_duration)}")
    report("joins", len(edl.joins))
    report("edl", ctx.paths.resolved / "edl.json")

    ctx.sentinel.verify()
    ctx.state.record(STAGE, segments=len(segments), joins=len(edl.joins),
                     output_duration=edl.total_duration)
    print("\n  source untouched. Next: bin/06_review.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
