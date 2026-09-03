#!/usr/bin/env python3
"""Stage 03 — word-level transcription.

Supports two local ASR backends, normalised to one schema so no later stage
depends on which recogniser ran:

  faster-whisper  pip install faster-whisper      (word_timestamps=True)
  whisper.cpp     whisper-cli / main on PATH      (--output-json-full --max-len 1)

Word timestamps are mandatory. A backend that cannot produce them is rejected
rather than falling back to segment timings, which are far too coarse to place
a sentence-boundary cut.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpipe.asr import detect_backend, transcribe
from vpipe.cli import StageContext, banner, base_parser, main_guard, report
from vpipe.errors import PipelineError
from vpipe.guards import write_text
from vpipe.timecode import format_hms

STAGE = "03_transcribe"


@main_guard
def main() -> int:
    parser = base_parser(__doc__)
    parser.add_argument("--backend", default="auto",
                        choices=["auto", "faster-whisper", "whisper.cpp"])
    parser.add_argument("--model", default="large-v3",
                        help="faster-whisper model name, or path to a "
                             "whisper.cpp ggml model file.")
    parser.add_argument("--language", default=None,
                        help="Force a language code; omit to auto-detect.")
    parser.add_argument("--compute-type", default="auto",
                        help="faster-whisper compute type (e.g. int8, float16).")
    args = parser.parse_args()

    ctx = StageContext(args)
    ctx.state.require("02_audio", "ASR working audio")

    banner(f"Stage 03 — transcription [{args.run_id}]")
    backend = detect_backend(args.backend)
    report("backend", backend)
    report("model", args.model)

    audio = ctx.paths.audio / "asr_16k_mono.wav"
    if not audio.exists():
        raise PipelineError(f"ASR audio missing: {audio}. Re-run bin/02_audio.py.")

    print("  transcribing locally...", flush=True)
    transcript = transcribe(
        audio,
        backend=backend, model=args.model, language=args.language,
        compute_type=args.compute_type,
        raw_path=ctx.paths.out("transcript", f"raw_{backend.replace('.', '_')}.json"),
        log_path=ctx.log(STAGE), verbose=True,
    )

    words_path = ctx.paths.out("transcript", "words.json")
    transcript.save(words_path)
    write_text(ctx.paths.out("transcript", "transcript.txt"),
               " ".join(w.text for w in transcript.words).strip() + "\n",
               ctx.paths.root)
    write_text(ctx.paths.out("transcript", "words_timed.txt"),
               "\n".join(f"[{format_hms(w.start)}] {w.text}"
                         for w in transcript.words) + "\n",
               ctx.paths.root)

    report("words", len(transcript.words))
    report("language", transcript.language or "(auto)")
    report("speech ends", format_hms(transcript.duration))
    report("words.json", words_path)

    ctx.sentinel.verify()
    ctx.state.record(STAGE, backend=backend, model=args.model,
                     words=len(transcript.words), language=transcript.language)
    print("\n  source untouched. Next: bin/04_brief.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
