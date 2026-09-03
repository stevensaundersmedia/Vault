"""The editorial decision file: schema, validation, and sentence segmentation.

The contract with the model is deliberately narrow. It may express editorial
judgement as quoted text and a reason. It may not express time. Every numeric
timing in this pipeline is computed by bin/05_resolve.py from the ASR word
timestamps, so a model that hallucinates a plausible-looking timecode cannot
move a cut.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import AmbiguityStop, PipelineError
from .words import SENTENCE_ENDINGS, Transcript

SCHEMA = "vpipe.decisions/1"

# Fields that would let the model set timing directly. Their presence is an
# error rather than something to ignore, because it means whoever produced the
# file misunderstood the contract and their other output cannot be trusted.
FORBIDDEN_KEYS = {
    "start", "end", "start_time", "end_time", "src_start", "src_end",
    "timecode", "tc_in", "tc_out", "seconds", "offset", "frame", "in", "out",
}

VALID_CATEGORIES = {
    "spine", "opinion", "example", "cta", "context",
}


@dataclass
class KeepSpan:
    """One span the editorial pass chose to retain, expressed only as quotes."""

    id: str
    start_quote: str
    end_quote: str
    reason: str
    category: str = ""


def _check_no_timing(obj, path: str = "keep") -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise PipelineError(
                    f"The decision file sets `{path}.{key}`. Editorial decisions "
                    f"must be expressed as quoted transcript anchors only; all "
                    f"timing is resolved locally from the word timestamps. "
                    f"Remove the timing fields and re-run."
                )
            _check_no_timing(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _check_no_timing(item, f"{path}[{i}]")


def load_decisions(path: Path) -> tuple[list[KeepSpan], dict]:
    """Parse and structurally validate a decision file."""
    if not path.exists():
        raise PipelineError(
            f"No decision file at {path}.\n"
            f"Run bin/04_brief.py, hand the brief to a capable model, and save "
            f"its JSON answer to that path."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"The decision file is not valid JSON: {exc}") from exc

    if data.get("schema") != SCHEMA:
        raise PipelineError(
            f"Decision file schema is {data.get('schema')!r}, expected {SCHEMA!r}."
        )

    keeps_raw = data.get("keep")
    if not isinstance(keeps_raw, list) or not keeps_raw:
        raise PipelineError(
            "The decision file retains nothing. Refusing to proceed to an "
            "empty edit."
        )
    _check_no_timing(keeps_raw)

    seen: set[str] = set()
    keeps: list[KeepSpan] = []
    for i, entry in enumerate(keeps_raw):
        if not isinstance(entry, dict):
            raise PipelineError(f"keep[{i}] is not an object.")
        missing = [k for k in ("id", "start_quote", "end_quote", "reason")
                   if not str(entry.get(k, "")).strip()]
        if missing:
            raise PipelineError(
                f"keep[{i}] is missing required field(s): {', '.join(missing)}. "
                f"Every retained span needs an id, both anchors, and a stated "
                f"editorial reason."
            )
        keep_id = str(entry["id"]).strip()
        if keep_id in seen:
            raise PipelineError(f"Duplicate keep id `{keep_id}`.")
        seen.add(keep_id)

        category = str(entry.get("category", "")).strip().lower()
        if category and category not in VALID_CATEGORIES:
            raise PipelineError(
                f"keep[{i}] (`{keep_id}`) has category `{category}`, which is not "
                f"one of: {', '.join(sorted(VALID_CATEGORIES))}."
            )
        keeps.append(KeepSpan(
            id=keep_id,
            start_quote=str(entry["start_quote"]),
            end_quote=str(entry["end_quote"]),
            reason=str(entry["reason"]),
            category=category,
        ))
    return keeps, data


def segment_sentences(transcript: Transcript) -> list[dict]:
    """Split the transcript into sentences with their word ranges.

    Version one cuts on sentence boundaries only, so the brief presents the
    transcript as sentences and the resolver checks anchors against these
    boundaries.
    """
    sentences: list[dict] = []
    start = 0
    for i, word in enumerate(transcript.words):
        if word.ends_sentence or i == len(transcript.words) - 1:
            span = transcript.words[start:i + 1]
            if span:
                sentences.append({
                    "index": len(sentences),
                    "first_word": span[0].index,
                    "last_word": span[-1].index,
                    "start": span[0].start,
                    "end": span[-1].end,
                    "text": " ".join(w.text for w in span).strip(),
                })
            start = i + 1
    return sentences
