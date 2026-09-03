# video-pipeline

A local, non-destructive edit pipeline for talking-head video. One raw
recording goes in; a timestamped transcript, structured edit decisions, a 720p
review render, a QA report and subtitles come out. A source-resolution master
and a private YouTube draft come out only after you explicitly approve them.

Everything runs locally. Nothing is sent anywhere except by stage 10, which
requires two separate approvals before it will run at all.

## The safety rules, and where they live in the code

These are enforced by code, not by convention:

| Rule | Enforced by |
|---|---|
| Never modify, move or delete source media | `vpipe/guards.py` — a `SourceSentinel` (size, mtime, SHA-256) is captured before the first read and re-verified at every stage boundary. The source is opened read-only and never appears as an ffmpeg output. |
| Write only into `runs/<run-id>/` | `vpipe/ffmpeg_run.py` — every ffmpeg invocation declares its outputs and each one is resolved and proven to be inside the run directory before the process starts. Traversal and symlinks are resolved first. |
| Never render the master before the exact 720p review is approved | `vpipe/runstate.py` — approval records the review's SHA-256. `bin/09_master.py` refuses unless the file on disk still hashes to the approved value, so a re-render invalidates the approval. |
| Never upload without a separate explicit instruction | `bin/10_upload.py` needs a second approval file *and* an explicit `--execute`. |
| Never publish, schedule or make a video public | `privacyStatus` is hard-coded to `private` and `publishAt` is never set. A hand-edited approval asking for anything else is refused. There is no code path to public. |
| Stop on ambiguous edit boundaries instead of guessing | `vpipe/words.py` and `bin/05_resolve.py` raise `AmbiguityStop` (exit 4) on a non-unique anchor, a missing anchor, a mid-sentence cut, an inverted or overlapping span, or a span below the minimum length. |

## Getting started

```bash
git clone -b claude/local-ai-video-editing-muz2c5 \
    https://github.com/stevensaundersmedia/Vault.git
cd Vault/video-pipeline

bin/00_doctor.py --source /path/to/your-recording.mp4
```

The doctor checks every dependency, validates your recording, estimates the
disk space the run will need, and prints the exact install command for
anything missing. It reads nothing and writes nothing.

Then:

```bash
bin/run_to_brief.sh  /path/to/your-recording.mp4   # stages 01-04
# ... editorial pass ...
bin/run_to_review.sh <run-id>                      # stages 05-08, stops at the gate
```

On Windows, run the `bin/*.py` stages directly rather than the `.sh` wrappers.

## Doing the editorial pass with a remote assistant

The recording never has to move. Stages 01-04 run locally and produce
`30_decisions/brief.md` — the editorial rules plus your transcript as numbered
sentences, a few KB of plain text. That is the only file that needs to travel.
Send it, get JSON back, save it as `30_decisions/decisions.json`, and run
`bin/run_to_review.sh`.

**The brief contains a full transcript of everything you said**, including the
false starts and the post-sign-off chat you are about to cut. Read it before
you share it. If the recording touches anything confidential, edit those
sentences out of the brief first — the pipeline only needs the sentences the
edit will actually quote, and an anchor that no longer matches stops the run
loudly rather than cutting in the wrong place.

## Requirements

- **Python 3.11+** — the pipeline itself uses only the standard library.
- **ffmpeg and ffprobe** on PATH (tested against 6.1).
- **One ASR backend**, either:
  - `pip install faster-whisper` (recommended; `large-v3` for accuracy), or
  - [whisper.cpp](https://github.com/ggerganov/whisper.cpp) built, with
    `whisper-cli` on PATH and a ggml model file.

  Either is auto-detected. Both are normalised to the same word schema, so the
  rest of the pipeline is identical whichever you use.
- **Stage 10 only**: `pip install google-api-python-client google-auth-oauthlib`
  and an OAuth client secrets file.

## Layout

```
video-pipeline/
  bin/          stage scripts, run in order (00_doctor first)
  vpipe/        libraries: guards, ffmpeg wrapper, timecode, ASR, resolver, QA
  config/       pipeline.toml — every timing number the pipeline uses
  tests/        unit tests and the synthetic-clip fixture generator
  runs/         all generated output, gitignored
```

Each run writes to `runs/<run-id>/`:

```
00_source/manifest.json          ffprobe metadata + source sentinel (no media copied)
10_audio/asr_16k_mono.wav        working audio for the recogniser only
20_transcript/words.json         canonical word-level transcript
30_decisions/brief.md            the packet you hand to a capable model
30_decisions/decisions.json      the model's answer: quoted anchors, no timing
35_resolved/edl.json             frame-exact edit decision list
35_resolved/segment_map.csv      source-to-output map, one row per retained span
35_resolved/boundaries.md        what survives, what was cut, every join in words
40_review/review_720p.mp4        the file the approval gate attaches to
50_qa/qa_report.md               the checks, with a verdict
50_qa/sheet_overview.jpg         overview contact sheet
50_qa/joins/join_NN.jpg          three frames across each join
50_qa/joins_as_heard.md          every join re-transcribed from the render
60_subtitles/review.srt|.vtt     cues mapped through the segment map
70_master/                       empty until you approve the review
90_upload/                       empty until you separately authorise upload
APPROVALS/  RUN_STATE.json  logs/
```

## Running it

```bash
RUN=2026-09-03-my-video

bin/01_manifest.py   --run-id $RUN --source /path/to/raw.mp4
bin/02_audio.py      --run-id $RUN
bin/03_transcribe.py --run-id $RUN --model large-v3
bin/04_brief.py      --run-id $RUN
```

Hand `runs/$RUN/30_decisions/brief.md` to a capable model. Save its JSON answer
as `runs/$RUN/30_decisions/decisions.json`, then:

```bash
bin/04_brief.py      --run-id $RUN --validate
bin/05_resolve.py    --run-id $RUN
bin/06_review.py     --run-id $RUN
bin/07_qa.py         --run-id $RUN
bin/08_subs.py       --run-id $RUN
```

**Stop here.** Read the QA report, look at the contact sheets, read the joins
as heard, and watch the review in full. Then, and only then:

```bash
bin/approve_review.py --run-id $RUN --note "why"
bin/09_master.py      --run-id $RUN
```

Upload is a separate decision again, and never happens without `--execute`:

```bash
bin/approve_upload.py --run-id $RUN --title "..."
bin/10_upload.py      --run-id $RUN --execute
```

## How the edit is decided

The model is used for exactly one thing: choosing which spans of speech
survive. It expresses that as **quoted transcript anchors and a reason** — never
as a time. Timing fields in a decision file are rejected outright.

`bin/05_resolve.py` then does all the arithmetic locally:

1. Match each anchor against the transcript, ignoring case and punctuation but
   respecting word boundaries. **Exactly one match, or stop.**
2. Check the span starts and ends on sentence boundaries. **Or stop.**
3. Place each cut inside the silence around the retained words. Padding is a
   ceiling, never more than half the available gap, so a cut cannot eat into
   neighbouring speech.
4. Snap outward to frame boundaries — start down, end up — so no retained frame
   is clipped.
5. Validate the whole list: ordered, non-overlapping, inside the source, every
   span above the minimum length.

## How the render works

The review and the master come from the same EDL through the same function
(`vpipe/render.py`), differing only in scale and encoder settings. The whole
edit is **one ffmpeg invocation**: every span is trimmed inside a single
filtergraph and concatenated there, so the audio timeline is conformed
continuously in one pass rather than stitched from separately encoded pieces.
20 ms ramps at each join prevent waveform-discontinuity clicks; they are below
the threshold of being heard as a fade.

Because both renders are the same code over the same EDL, the master cannot be
a different edit from the review you approved.

## QA

`bin/07_qa.py` checks decode, duration against the EDL, A/V delta, silence,
black frames, the opening, the ending and every join; writes an overview
contact sheet and a three-frame strip across each join; and re-transcribes the
render so every join can be read as it will be heard.

The re-transcription is **required**. If no ASR backend is available it is
recorded as `BLOCKED`, not passed, the verdict is `FAIL`, and
`bin/approve_review.py` refuses the approval unless you override it with
`--accept-qa-failure` and a written note.

## Version one scope

One video source, one audio track, talking-head content, sentence-boundary
cuts only. Subtractive: retained spans play in their original order. Removes
setup, false starts, repeated takes, tangents and post-sign-off chat; keeps the
teaching spine, strong opinions, examples and the call to action. No music, no
B-roll, no zooms, no generated animation, no reordering.

## Tests

```bash
python3 tests/test_words.py
python3 tests/test_resolve.py
```

`tests/make_synthetic_source.py` generates a talking-head clip with real
synthesised speech (espeak-ng) containing every category the editorial pass is
meant to remove, for exercising the pipeline end to end without real footage.
