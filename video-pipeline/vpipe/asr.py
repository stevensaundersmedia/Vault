"""Local ASR backends, shared by transcription and by QA re-transcription.

Both backends are normalised to one schema in vpipe.words. Keeping the backend
selection here means the QA stage re-transcribes the review with exactly the
same recogniser and settings that produced the edit decisions, so a difference
in the two transcripts means a difference in the audio, not in the tooling.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .errors import MissingDependency, PipelineError
from .words import Transcript, from_faster_whisper, from_whisper_cpp

CPP_BINARIES = ("whisper-cli", "whisper-cpp", "main")


def have_faster_whisper() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def find_whisper_cpp() -> str | None:
    for name in CPP_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def available_backends() -> list[str]:
    backends = []
    if have_faster_whisper():
        backends.append("faster-whisper")
    if find_whisper_cpp():
        backends.append("whisper.cpp")
    return backends


def detect_backend(requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    backends = available_backends()
    if not backends:
        raise MissingDependency(
            "No local ASR backend found. Install one of:\n"
            "  pip install faster-whisper           (recommended)\n"
            "  build whisper.cpp and put `whisper-cli` on PATH\n"
            "Transcription runs entirely locally; nothing is sent anywhere."
        )
    return backends[0]


def run_faster_whisper(audio: Path, model: str, language: str | None,
                       compute_type: str = "auto", *,
                       raw_path: Path | None = None,
                       verbose: bool = False) -> Transcript:
    from faster_whisper import WhisperModel

    engine = WhisperModel(model, device="auto", compute_type=compute_type)
    segments, info = engine.transcribe(
        str(audio),
        language=language,
        word_timestamps=True,   # non-negotiable for this pipeline
        vad_filter=False,       # VAD would silently drop speech near cuts
        condition_on_previous_text=True,
    )

    payload = {"language": info.language, "duration": info.duration, "segments": []}
    for seg in segments:
        payload["segments"].append({
            "id": seg.id, "start": seg.start, "end": seg.end, "text": seg.text,
            "words": [{"word": w.word, "start": w.start, "end": w.end,
                       "probability": getattr(w, "probability", None)}
                      for w in (seg.words or [])],
        })
        if verbose:
            print(f"    [{seg.start:7.2f}] {seg.text.strip()[:78]}", flush=True)

    if raw_path:
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    return from_faster_whisper(payload)


def run_whisper_cpp(audio: Path, model: str, language: str | None, *,
                    raw_path: Path, log_path: Path | None = None) -> Transcript:
    binary = find_whisper_cpp()
    if not binary:
        raise MissingDependency(
            "whisper.cpp binary not found on PATH (looked for: "
            + ", ".join(CPP_BINARIES) + ")."
        )
    if not model or not Path(model).exists():
        raise MissingDependency(
            f"whisper.cpp needs a ggml model file. Pass --model /path/to/"
            f"ggml-large-v3.bin (got: {model!r})."
        )

    prefix = raw_path.with_suffix("")
    cmd = [binary, "-m", str(model), "-f", str(audio),
           "--output-json-full", "--max-len", "1",
           "--output-file", str(prefix)]
    if language:
        cmd += ["-l", language]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"$ {' '.join(cmd)}\n{result.stderr}", encoding="utf-8")
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise PipelineError(f"whisper.cpp failed (exit {result.returncode}):\n{tail}")

    produced = Path(str(prefix) + ".json")
    if not produced.exists():
        raise PipelineError(
            f"whisper.cpp reported success but wrote no JSON at {produced}."
        )
    payload = json.loads(produced.read_text(encoding="utf-8"))
    if produced != raw_path:
        produced.replace(raw_path)
    return from_whisper_cpp(payload)


def transcribe(audio: Path, *, backend: str, model: str, language: str | None,
               compute_type: str, raw_path: Path,
               log_path: Path | None = None, verbose: bool = False) -> Transcript:
    """Run the chosen backend and return a normalised transcript."""
    if backend == "faster-whisper":
        return run_faster_whisper(audio, model, language, compute_type,
                                  raw_path=raw_path, verbose=verbose)
    if backend == "whisper.cpp":
        return run_whisper_cpp(audio, model, language,
                               raw_path=raw_path, log_path=log_path)
    raise PipelineError(f"Unknown ASR backend: {backend!r}")
