#!/usr/bin/env python3
"""Stage 07 — QA on the review render.

Verifies the review before you are asked to approve it: decode, duration,
A/V delta, silence, black frames, the opening, the ending and every join.
Produces an overview contact sheet, a strip of frames across each join, and a
re-transcription of the review so every join can be read as it will be heard.

The re-transcription is a required check. If no ASR backend is available it is
recorded as BLOCKED, not passed, and the report says the review gate is not
satisfied.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.asr import available_backends, detect_backend, transcribe
from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.errors import PipelineError
from vpipe.ffmpeg_run import ffprobe_json
from vpipe.guards import sha256_file, write_text
from vpipe.qa import (
    BLOCKED, CheckList, FAIL, PASS, WARN,
    decode_check, detect_black, detect_silence, extract_frame, hstack,
)
from vpipe.segments import EDL
from vpipe.timecode import format_hms
from vpipe.words import Transcript

STAGE = "07_qa"
REVIEW_NAME = "review_720p.mp4"


def overlaps(intervals, start, end):
    return [i for i in intervals if i["end"] > start and i["start"] < end]


@main_guard
def main() -> int:
    parser = base_parser(__doc__)
    parser.add_argument("--backend", default="auto",
                        choices=["auto", "faster-whisper", "whisper.cpp"])
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default=None)
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--skip-retranscribe", action="store_true",
                        help="Record the re-transcription check as BLOCKED "
                             "instead of running it. The report will say the "
                             "review gate is not satisfied.")
    args = parser.parse_args()

    ctx = StageContext(args)
    ctx.state.require("06_review", "review render")

    banner(f"Stage 07 — QA on the review [{args.run_id}]")

    qa_cfg = ctx.config["qa"]
    edl = EDL.load(ctx.paths.resolved / "edl.json")
    review = ctx.paths.review / REVIEW_NAME
    if not review.exists():
        raise PipelineError(f"No review render at {review}. Run bin/06_review.py.")

    log = ctx.log(STAGE)
    checks = CheckList()
    digest = sha256_file(review)

    # --- decode ----------------------------------------------------------
    errors = decode_check(review, log)
    checks.add("decode", PASS if not errors else FAIL,
               "decodes cleanly end to end" if not errors
               else f"decoder reported errors: {errors.splitlines()[0]}")

    # --- duration --------------------------------------------------------
    probe = ffprobe_json(review, log_path=log)
    actual = float(probe["format"]["duration"])
    drift = abs(actual - edl.total_duration)
    checks.add("duration", PASS if drift <= qa_cfg["duration_tolerance_s"] else FAIL,
               f"{format_hms(actual)} vs EDL {format_hms(edl.total_duration)} "
               f"(drift {drift:.3f}s, tolerance {qa_cfg['duration_tolerance_s']}s)",
               actual=actual, expected=edl.total_duration, drift=drift)

    # --- A/V delta -------------------------------------------------------
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    v_dur = float(video.get("duration") or actual)
    a_dur = float(audio.get("duration") or actual)
    av_delta = abs(v_dur - a_dur)
    v_start = float(video.get("start_time") or 0.0)
    a_start = float(audio.get("start_time") or 0.0)
    start_delta = abs(v_start - a_start)
    checks.add("a/v delta",
               PASS if max(av_delta, start_delta) <= qa_cfg["av_delta_tolerance_s"] else FAIL,
               f"video {v_dur:.3f}s / audio {a_dur:.3f}s (delta {av_delta:.3f}s), "
               f"start offset {start_delta:.3f}s",
               av_delta=av_delta, start_delta=start_delta)

    # --- silence and black ----------------------------------------------
    silences = detect_silence(review, threshold_db=qa_cfg["silence_threshold_db"],
                              min_duration=qa_cfg["silence_min_duration_s"], log_path=log)
    checks.add("silence", PASS if not silences else WARN,
               "no silence beyond threshold" if not silences
               else f"{len(silences)} run(s) over {qa_cfg['silence_min_duration_s']}s: "
                    + ", ".join(f"{format_hms(s['start'])}+{s['duration']:.1f}s"
                                for s in silences[:5]),
               intervals=silences)

    blacks = detect_black(review, min_duration=qa_cfg["black_min_duration_s"],
                          pixel_threshold=qa_cfg["black_pixel_threshold"], log_path=log)
    checks.add("black frames", PASS if not blacks else FAIL,
               "none" if not blacks
               else f"{len(blacks)} interval(s): "
                    + ", ".join(f"{format_hms(b['start'])}+{b['duration']:.2f}s"
                                for b in blacks[:5]),
               intervals=blacks)

    # --- opening and ending ---------------------------------------------
    edge = qa_cfg["edge_check_s"]
    open_black = overlaps(blacks, 0.0, edge)
    open_silent = [s for s in silences if s["start"] <= 0.05 and s["duration"] >= edge]
    checks.add("opening",
               FAIL if (open_black or open_silent) else PASS,
               f"first {edge:.0f}s: "
               + ("black at the head" if open_black else
                  "dead air at the head" if open_silent else "picture and speech present"))

    tail_start = max(0.0, actual - edge)
    end_black = overlaps(blacks, tail_start, actual)
    end_silent = [s for s in silences if s["end"] >= actual - 0.05 and s["duration"] >= edge]
    checks.add("ending",
               FAIL if end_black else (WARN if end_silent else PASS),
               f"last {edge:.0f}s: "
               + ("black at the tail" if end_black else
                  "trailing dead air" if end_silent else "ends on picture and speech"))

    # --- joins -----------------------------------------------------------
    joins = edl.joins
    join_problems = []
    for join in joins:
        window = (join["out_time"] - 0.5, join["out_time"] + 0.5)
        if overlaps(blacks, *window):
            join_problems.append(f"black at join {join['join_index']}")
        long_silence = [s for s in overlaps(silences, *window)
                        if s["duration"] >= qa_cfg["silence_min_duration_s"]]
        if long_silence:
            join_problems.append(f"silence gap at join {join['join_index']}")
    checks.add("joins", PASS if not join_problems else FAIL,
               f"{len(joins)} join(s), no black or dead air within 0.5s"
               if not join_problems else "; ".join(join_problems))

    # --- contact sheets --------------------------------------------------
    sheets_dir = ctx.paths.qa / "joins"
    overview = ctx.paths.out("qa", "sheet_overview.jpg")
    columns, rows = (int(x) for x in str(qa_cfg["overview_grid"]).split("x"))
    interval = qa_cfg["overview_interval_s"]
    # Sample often enough to fill the grid on a short review.
    if actual / interval < columns * rows / 2:
        interval = max(1.0, actual / (columns * rows))
    from vpipe.ffmpeg_run import run_ffmpeg
    run_ffmpeg(
        ["-i", str(review),
         "-vf", f"fps=1/{interval:.4f},scale=320:-2,tile={columns}x{rows}",
         "-frames:v", "1", "-q:v", "3", str(overview)],
        outputs=[overview], run_dir=ctx.paths.root, log_path=log,
    )
    checks.add("overview sheet", PASS,
               f"{columns}x{rows} grid, one frame every {interval:.1f}s -> "
               f"{overview.name}")

    join_sheets = []
    for join in joins:
        t = join["out_time"]
        frames = []
        for offset, tag in ((-0.5, "-0.5s"), (0.0, "AT JOIN"), (0.5, "+0.5s")):
            at = min(max(0.0, t + offset), max(0.0, actual - 0.05))
            frame = ctx.paths.qa / "joins" / f"j{join['join_index']:02d}{tag.replace(' ','_').replace('.','_')}.jpg"
            frames.append(extract_frame(review, at, frame, run_dir=ctx.paths.root,
                                        label=f"join {join['join_index']} {tag} "
                                              f"@{format_hms(at)}", log_path=log))
        sheet = ctx.paths.qa / "joins" / f"join_{join['join_index']:02d}.jpg"
        hstack(frames, sheet, run_dir=ctx.paths.root, log_path=log)
        join_sheets.append(sheet)
        for frame in frames:
            frame.unlink(missing_ok=True)
    checks.add("join sheets", PASS,
               f"{len(join_sheets)} strip(s) written to {sheets_dir.name}/")

    # --- re-transcribe the review ---------------------------------------
    heard_path = ctx.paths.qa / "joins_as_heard.md"
    review_transcript = None
    if args.skip_retranscribe:
        checks.add("re-transcription", BLOCKED,
                   "skipped by request; the review gate is NOT satisfied without it")
    elif not available_backends():
        checks.add("re-transcription", BLOCKED,
                   "no local ASR backend available, so the joins cannot be "
                   "verified as heard; the review gate is NOT satisfied")
    else:
        backend = detect_backend(args.backend)
        print(f"  re-transcribing the review with {backend}...", flush=True)
        review_audio = ctx.paths.out("qa", "review_16k_mono.wav")
        run_ffmpeg(
            ["-i", str(review), "-vn", "-ac", "1", "-ar",
             str(ctx.config["audio"]["asr_sample_rate"]),
             "-c:a", "pcm_s16le", str(review_audio)],
            outputs=[review_audio], run_dir=ctx.paths.root, log_path=log,
        )
        review_transcript = transcribe(
            review_audio, backend=backend, model=args.model,
            language=args.language, compute_type=args.compute_type,
            raw_path=ctx.paths.out("qa", "review_raw_asr.json"), log_path=log,
        )
        review_transcript.save(ctx.paths.out("qa", "review_words.json"))
        checks.add("re-transcription", PASS,
                   f"{len(review_transcript.words)} words recognised in the review")

    # Every join, written out as it is heard in the rendered review.
    context = qa_cfg["join_context_s"]
    lines = [f"# Joins as heard — {args.run_id}", "",
             f"Review: `{REVIEW_NAME}`  sha256 `{digest}`", ""]
    if review_transcript is None:
        lines += ["> The review was NOT re-transcribed, so what follows is the "
                  "intended wording from the source transcript, not what the "
                  "rendered file actually says. The review gate is not satisfied.",
                  ""]
    source_transcript = Transcript.load(ctx.paths.transcript / "words.json")

    for join in joins:
        t = join["out_time"]
        lines += [f"## Join {join['join_index']} — output {format_hms(t)}", "",
                  f"- removes {join['removed_duration']:.2f}s of source "
                  f"({format_hms(join['src_out_point'])} -> "
                  f"{format_hms(join['src_in_point'])})", ""]
        if review_transcript is not None:
            before = review_transcript.text_between(max(0.0, t - context), t)
            after = review_transcript.text_between(t, t + context)
            lines += ["As heard in the review:", "",
                      f"> ...{before}  **|**  {after}...", ""]
        else:
            before = source_transcript.text_between(
                max(0.0, join["src_out_point"] - context), join["src_out_point"])
            after = source_transcript.text_between(
                join["src_in_point"], join["src_in_point"] + context)
            lines += ["Intended (from the source transcript):", "",
                      f"> ...{before}  **|**  {after}...", ""]
        lines += [f"![join {join['join_index']}](joins/join_{join['join_index']:02d}.jpg)", ""]

    write_text(heard_path, "\n".join(lines) + "\n", ctx.paths.root)

    # --- report ----------------------------------------------------------
    verdict = checks.verdict
    report_lines = [
        f"# QA report — {args.run_id}", "",
        f"**Verdict: {verdict}**", "",
        "| Check | Status | Detail |", "|---|---|---|",
    ]
    for check in checks.checks:
        report_lines.append(
            f"| {check.name} | {check.status} | {check.detail.replace('|', '/')} |"
        )
    report_lines += [
        "", "## Review file", "",
        f"- path: `{review}`",
        f"- sha256: `{digest}`",
        f"- duration: {format_hms(actual)} ({actual:.3f}s)",
        f"- resolution: {video.get('width')}x{video.get('height')}",
        f"- segments: {len(edl.segments)}, joins: {len(joins)}",
        f"- source retained: {100 * edl.total_duration / edl.source_duration:.1f}%",
        "", "## Artefacts", "",
        f"- overview contact sheet: `{overview.name}`",
        f"- join contact sheets: `joins/join_NN.jpg` ({len(join_sheets)})",
        f"- joins as heard: `{heard_path.name}`",
        f"- segment map: `../35_resolved/segment_map.csv`",
        "", "## Segment map", "",
        "| # | id | source in | source out | output in | output out | reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for seg in edl.segments:
        report_lines.append(
            f"| {seg.index} | {seg.keep_id} | {format_hms(seg.src_start)} | "
            f"{format_hms(seg.src_end)} | {format_hms(seg.out_start)} | "
            f"{format_hms(seg.out_end)} | {seg.reason.replace('|', '/')} |"
        )

    if checks.blocking:
        report_lines += ["", "## Blocking issues", ""]
        report_lines += [f"- **{c.name}**: {c.detail}" for c in checks.blocking]
        report_lines += ["", "This review must not be approved as it stands."]
    else:
        report_lines += ["", "## Approval", "",
                         "Nothing blocking was found. Watch the review in full "
                         "before approving; these checks confirm the render is "
                         "sound, not that the edit is right."]

    qa_path = ctx.paths.out("qa", "qa_report.md")
    write_text(qa_path, "\n".join(report_lines) + "\n", ctx.paths.root)

    ctx.sentinel.verify()
    ctx.state.record(STAGE, verdict=verdict, sha256=digest,
                     blocking=[c.name for c in checks.blocking],
                     warnings=[c.name for c in checks.warnings])

    print(f"\n  verdict: {verdict}")
    report("qa report", qa_path)
    report("joins as heard", heard_path)
    report("review sha256", digest)
    print()
    return 0 if verdict != FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
