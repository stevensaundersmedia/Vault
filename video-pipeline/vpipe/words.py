"""Word-level transcript model, ASR backend normalisation, and anchor matching.

Two ASR backends are supported and both are flattened into one schema, so no
stage downstream of transcription knows or cares which produced the words.

The anchor matcher is the bridge between the model's editorial judgement and
deterministic timing: the model supplies quoted text, this module says which
words those are, and refuses to answer when the quote is not unique.
"""

from __future__ import annotations

import bisect
import json
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path

from .errors import AmbiguityStop, PipelineError

SENTENCE_ENDINGS = ".?!…"

# Characters that survive normalisation: letters, digits and the apostrophes
# that live inside contractions. Everything else becomes a token separator.
_KEEP = re.compile(r"[^a-z0-9']+")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})


@dataclass
class Word:
    """One ASR word with its source-timeline span."""

    index: int
    text: str          # as spoken, with original punctuation and casing
    start: float
    end: float

    @property
    def ends_sentence(self) -> bool:
        stripped = self.text.rstrip().rstrip('"”)’\'')
        return bool(stripped) and stripped[-1] in SENTENCE_ENDINGS


def normalise_token(text: str) -> str:
    """Reduce one word to its comparable form."""
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES).lower()
    text = _KEEP.sub("", text)
    return text.strip("'")


class Transcript:
    """Words plus the normalised search surface used for anchor matching."""

    def __init__(self, words: list[Word], language: str = "", backend: str = ""):
        self.words = words
        self.language = language
        self.backend = backend
        self._build_index()

    def _build_index(self) -> None:
        """Build a space-joined normalised string and a char-offset -> word map.

        Tokens that normalise to nothing (stray punctuation emitted as its own
        word by some backends) are skipped in the search surface but keep their
        place in self.words, so timing is never shifted by the cleanup.
        """
        parts: list[str] = []
        starts: list[int] = []
        owners: list[int] = []
        cursor = 0
        for word in self.words:
            token = normalise_token(word.text)
            if not token:
                continue
            if parts:
                cursor += 1  # the joining space
            parts.append(token)
            starts.append(cursor)
            owners.append(word.index)
            cursor += len(token)
        self.norm_text = " ".join(parts)
        self._tok_starts = starts
        self._tok_word_index = owners

    def _word_at_offset(self, offset: int) -> int:
        pos = bisect.bisect_right(self._tok_starts, offset) - 1
        if pos < 0:
            raise PipelineError(f"Offset {offset} precedes the transcript.")
        return self._tok_word_index[pos]

    def find_quote(self, quote: str, *, label: str) -> tuple[int, int]:
        """Locate a quoted anchor and return its (first, last) word indices.

        Refuses on zero matches and on more than one. This is the single point
        where an ambiguous edit boundary stops the run instead of being guessed.
        """
        needle = " ".join(
            token for token in (normalise_token(w) for w in quote.split()) if token
        )
        if not needle:
            raise AmbiguityStop(
                f"{label}: the anchor quote contains no matchable words: {quote!r}"
            )

        padded_hay = f" {self.norm_text} "
        padded_needle = f" {needle} "
        hits: list[int] = []
        start = padded_hay.find(padded_needle)
        while start != -1:
            hits.append(start)  # offset into padded string; -1 + 1 cancels out
            start = padded_hay.find(padded_needle, start + 1)

        if not hits:
            raise AmbiguityStop(
                f"{label}: anchor quote not found in the transcript.\n"
                f"  quote: {quote!r}\n"
                f"  Either the wording drifted from what was actually said, or "
                f"this anchor belongs to a different recording. Not guessing."
            )
        if len(hits) > 1:
            contexts = []
            for hit in hits[:5]:
                window = self.norm_text[max(0, hit - 60): hit + len(needle) + 60]
                contexts.append(f"    ...{window}...")
            raise AmbiguityStop(
                f"{label}: anchor quote matches {len(hits)} places in the "
                f"transcript, so the intended one is unknowable.\n"
                f"  quote: {quote!r}\n"
                f"  matches:\n" + "\n".join(contexts) + "\n"
                f"  Lengthen the quote until it is unique. Not guessing."
            )

        offset = hits[0]
        first = self._word_at_offset(offset)
        last = self._word_at_offset(offset + len(needle) - 1)
        return first, last

    def by_index(self, index: int) -> Word:
        return self.words[index]

    def text_between(self, start_s: float, end_s: float) -> str:
        """Plain text of every word overlapping a source-time window."""
        return " ".join(
            w.text for w in self.words if w.end > start_s and w.start < end_s
        ).strip()

    @property
    def duration(self) -> float:
        return self.words[-1].end if self.words else 0.0

    def to_dict(self) -> dict:
        return {
            "schema": "vpipe.words/1",
            "backend": self.backend,
            "language": self.language,
            "word_count": len(self.words),
            "words": [asdict(w) for w in self.words],
        }

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema") != "vpipe.words/1":
            raise PipelineError(f"Unexpected transcript schema in {path}")
        words = [Word(**w) for w in data["words"]]
        return cls(words, data.get("language", ""), data.get("backend", ""))


def _clean_words(raw: list[tuple[str, float, float]], backend: str) -> list[Word]:
    """Shared post-processing for every backend.

    Drops empty tokens, repairs the non-monotonic and zero-length spans that
    both backends occasionally emit, and re-indexes.
    """
    words: list[Word] = []
    previous_end = 0.0
    for text, start, end in raw:
        text = text.strip()
        if not text:
            continue
        start = max(float(start), previous_end)
        end = max(float(end), start + 0.001)
        words.append(Word(index=len(words), text=text, start=start, end=end))
        previous_end = end
    if not words:
        raise PipelineError(
            f"The {backend} transcript contains no words. Check that the audio "
            f"track actually carries speech."
        )
    return words


def from_faster_whisper(payload: dict) -> Transcript:
    """Normalise the JSON written by bin/03_transcribe.py's faster-whisper path."""
    raw = [
        (w["word"], w["start"], w["end"])
        for seg in payload.get("segments", [])
        for w in seg.get("words") or []
    ]
    if not raw:
        raise PipelineError(
            "faster-whisper returned no word timings. The model must be run "
            "with word_timestamps=True."
        )
    return Transcript(
        _clean_words(raw, "faster-whisper"),
        payload.get("language", ""),
        "faster-whisper",
    )


def from_whisper_cpp(payload: dict) -> Transcript:
    """Normalise whisper.cpp `--output-json --max-len 1` output.

    whisper.cpp reports offsets in milliseconds and, in token mode, emits
    punctuation and the special [_BEG_]-style markers as their own tokens.
    """
    raw: list[tuple[str, float, float]] = []
    for seg in payload.get("transcription", []):
        tokens = seg.get("tokens")
        # With --max-len 1 each segment is already one word; token detail is
        # used when present because it carries tighter timings.
        entries = tokens if tokens else [seg]
        for entry in entries:
            text = entry.get("text", "")
            if text.strip().startswith("[_"):
                continue
            offsets = entry.get("offsets") or {}
            if "from" not in offsets:
                continue
            raw.append((text, offsets["from"] / 1000.0, offsets["to"] / 1000.0))
    if not raw:
        raise PipelineError(
            "whisper.cpp returned no word offsets. Run it with "
            "`--output-json-full --max-len 1` so per-word offsets are written."
        )
    return Transcript(
        _clean_words(raw, "whisper.cpp"),
        payload.get("result", {}).get("language", ""),
        "whisper.cpp",
    )
