"""Subtitle parsing/serialisation, language codes in file names, tag protection."""

from __future__ import annotations

import codecs
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import charset_normalizer
import pysubs2
from pysubs2.exceptions import UnknownFPSError

log = logging.getLogger("subtitle_translator.subtitles")

SUPPORTED_EXTENSIONS = {".srt": "srt", ".vtt": "vtt", ".ass": "ass", ".ssa": "ssa", ".sub": "microdvd"}
_MICRODVD_LINE = re.compile(r"^\{\d+\}\{\d*\}")


class SubtitleError(Exception):
    """User-facing error about a subtitle file (message must not contain paths)."""


# --- Language codes in file names -------------------------------------------------

_LANGUAGES: dict[str, tuple[str, ...]] = {
    "en": ("eng", "english"),
    "ar": ("ara", "arabic"),
    "az": ("aze", "azerbaijani"),
    "bg": ("bul", "bulgarian"),
    "bn": ("ben", "bengali"),
    "ca": ("cat", "catalan"),
    "cs": ("cze", "ces", "czech"),
    "da": ("dan", "danish"),
    "de": ("ger", "deu", "german", "deutsch"),
    "el": ("gre", "ell", "greek"),
    "eo": ("epo", "esperanto"),
    "es": ("spa", "spanish", "espanol"),
    "et": ("est", "estonian"),
    "eu": ("baq", "eus", "basque"),
    "fa": ("per", "fas", "persian", "farsi"),
    "fi": ("fin", "finnish"),
    "fr": ("fre", "fra", "french", "francais"),
    "ga": ("gle", "irish"),
    "gl": ("glg", "galician"),
    "he": ("heb", "hebrew"),
    "hi": ("hin", "hindi"),
    "hr": ("hrv", "croatian"),
    "hu": ("hun", "hungarian"),
    "id": ("ind", "indonesian"),
    "it": ("ita", "italian"),
    "ja": ("jpn", "japanese"),
    "ko": ("kor", "korean"),
    "ky": ("kir", "kyrgyz"),
    "lt": ("lit", "lithuanian"),
    "lv": ("lav", "latvian"),
    "ms": ("may", "msa", "malay"),
    "nb": ("nob", "norwegian", "no", "nor"),
    "nl": ("dut", "nld", "dutch", "nederlands"),
    "pl": ("pol", "polish"),
    "pt": ("por", "portuguese"),
    "pt-BR": ("pob", "pt-br", "ptbr", "brazilian"),
    "ro": ("rum", "ron", "romanian"),
    "ru": ("rus", "russian"),
    "sk": ("slo", "slk", "slovak"),
    "sl": ("slv", "slovenian"),
    "sq": ("alb", "sqi", "albanian"),
    "sr": ("srp", "serbian"),
    "sv": ("swe", "swedish"),
    "th": ("tha", "thai"),
    "tl": ("tgl", "fil", "tagalog", "filipino"),
    "tr": ("tur", "turkish"),
    "uk": ("ukr", "ukrainian"),
    "ur": ("urd", "urdu"),
    "vi": ("vie", "vietnamese"),
    "zh": ("chi", "zho", "chinese"),
    "zh-Hans": ("zh-hans", "chs", "zh-cn"),
    "zh-Hant": ("zh-hant", "cht", "zh-tw"),
}
LANG_ALIASES: dict[str, str] = {}
for _code, _aliases in _LANGUAGES.items():
    LANG_ALIASES[_code.lower()] = _code
    for _alias in _aliases:
        LANG_ALIASES[_alias] = _code

# Tokens that commonly follow the language code: Movie.en.forced.srt, Movie.en.sdh.srt
_FLAGS = {"forced", "sdh", "cc", "hi", "default", "full"}


def subtitle_format(path: Path) -> str | None:
    return SUPPORTED_EXTENSIONS.get(path.suffix.lower())


def _lang_index(tokens: list[str]) -> int | None:
    """Index of the language token in a dotted stem (never the first token)."""
    n = len(tokens)
    for i in range(n - 1, max(0, n - 4), -1):
        tok = tokens[i].lower()
        if tok in _FLAGS:
            has_earlier_lang = any(tokens[j].lower() in LANG_ALIASES for j in range(1, i))
            if tok in LANG_ALIASES and not has_earlier_lang:
                return i
            continue
        if tok in LANG_ALIASES:
            return i
        return None
    return None


def language_from_name(path: Path) -> str | None:
    """Return the canonical language code found in the file name, if any."""
    tokens = path.stem.split(".")
    idx = _lang_index(tokens)
    return LANG_ALIASES[tokens[idx].lower()] if idx is not None else None


def output_path(path: Path, target: str) -> Path:
    """`Movie.en.srt` -> `Movie.fr.srt`; `Movie.srt` -> `Movie.fr.srt`."""
    tokens = path.stem.split(".")
    idx = _lang_index(tokens)
    if idx is None:
        tokens.append(target)
    else:
        tokens[idx] = target
    return path.with_name(".".join(tokens) + path.suffix)


def same_language(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.lower() == b.lower()


# --- Reading / writing ------------------------------------------------------------


def decode_bytes(data: bytes) -> str:
    """Decode subtitle bytes: UTF-8 first, then detected legacy encodings."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    for bom, enc in ((b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc)
    matches = charset_normalizer.from_bytes(data)
    best = matches.best()
    if best is None:
        raise SubtitleError("Could not detect the text encoding")
    # Short Western texts are ambiguous between single-byte code pages; prefer the most common one on a tie.
    for m in matches:
        if m.encoding == "cp1252" and m.chaos <= best.chaos and m.coherence >= best.coherence:
            return str(m)
    return str(best)


def load_subtitles(data: bytes, fmt: str, default_fps: float) -> pysubs2.SSAFile:
    if fmt == "microdvd" and b"\x00" in data[:4096]:
        raise SubtitleError("Binary .sub files (VobSub) are not supported")
    text = decode_bytes(data)
    if fmt == "microdvd":
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not _MICRODVD_LINE.match(first):
            raise SubtitleError("Only MicroDVD text .sub files are supported")
    try:
        if fmt in ("srt", "vtt"):
            return pysubs2.SSAFile.from_string(text, format_=fmt, keep_html_tags=True)
        if fmt == "microdvd":
            try:
                subs = pysubs2.SSAFile.from_string(text, format_=fmt)
                subs.info["_fps_declared"] = "1"
                return subs
            except UnknownFPSError:
                return pysubs2.SSAFile.from_string(text, format_=fmt, fps=default_fps)
        return pysubs2.SSAFile.from_string(text, format_=fmt)
    except SubtitleError:
        raise
    except Exception as exc:
        raise SubtitleError(f"Could not parse the {fmt.upper()} file") from exc


def load_nonempty(data: bytes, fmt: str, default_fps: float) -> pysubs2.SSAFile:
    subs = load_subtitles(data, fmt, default_fps)
    if not subs.events:
        raise SubtitleError("No subtitles found in the file")
    return subs


def dump_subtitles(subs: pysubs2.SSAFile, fmt: str) -> bytes:
    if fmt in ("srt", "vtt"):
        text = subs.to_string(fmt, keep_ssa_tags=True)
    elif fmt == "microdvd":
        declared = subs.info.pop("_fps_declared", None) is not None
        text = subs.to_string(fmt, fps=subs.fps, write_fps_declaration=declared)
    else:
        text = subs.to_string(fmt)
    # UTF-8 with BOM: without it many players, TVs and Windows editors assume CP1252.
    return codecs.BOM_UTF8 + text.encode("utf-8")


# --- Cue segmentation, tag protection and rewrapping ------------------------------
#
# Tags never reach LibreTranslate. A cue is split into lines and every line into the
# text segments between tags; only those segments are translated and the tags are put
# back verbatim, so no placeholder can leak or get lost. Lines are only joined (for
# better context) when they are plain continuation text of one sentence.

_TAG = re.compile(r"\{[^{}]*\}|<[^<>]+>|\\h")
_LINEBREAK = re.compile(r"\\[Nn]|\n")
_TAG_RUN_START = re.compile(r"^(?:\s*(?:\{[^{}]*\}|<[^<>]+>))+")
_TAG_RUN_END = re.compile(r"(?:(?:\{[^{}]*\}|<[^<>]+>)\s*)+$")
_LETTER = re.compile(r"[^\W\d_]")
_LEAD_DECOR = re.compile(r"^\s*(?:[-–—]+\s*)?(?:[♪♫#*]+\s*)?")
_TRAIL_DECOR = re.compile(r"\s*(?:[♪♫#*]+\s*)?$")
_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’»)\]]*$")
_LINE_START_MARKERS = ("-", "–", "—", "♪", "♫", "#", "*", "(", "[")
_BRACKETS = (("(", ")"), ("[", "]"))


def _is_caps(text: str) -> bool:
    """True for SHOUTED / SDH text: at least two letters and none of them lowercase."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 2 and not any(c.islower() for c in letters) and any(c.isupper() for c in letters)


def _sentence_case(text: str) -> str:
    lowered = text.lower()
    m = _LETTER.search(lowered)
    return lowered if m is None else lowered[: m.start()] + lowered[m.start()].upper() + lowered[m.start() + 1 :]


def _clean(text: str) -> str:
    return re.sub(r"\\[Nn]|\s+", " ", text).strip()


@dataclass
class _Segment:
    """Text between tags, without decoration such as dialogue dashes, music notes or brackets."""

    lead: str
    core: str
    trail: str
    n_lines: int = 1
    caps: bool = False

    @property
    def query(self) -> str:
        # Machine translation handles ALL-CAPS badly; translate in sentence case and re-apply caps.
        return _sentence_case(self.core) if self.caps else self.core

    def rebuild(self, translated: str) -> str:
        text = _clean(translated) or self.core
        if text.endswith(".") and not text.endswith("..") and not _SENTENCE_END.search(self.core):
            text = text[:-1].rstrip() or text
        if self.caps:
            text = text.upper()
        return f"{self.lead}{rewrap(text, self.n_lines)}{self.trail}"


Part = str | _Segment


def _text_parts(text: str, n_lines: int = 1) -> list[Part]:
    if not _LETTER.search(text):
        return [text]
    lead = _LEAD_DECOR.match(text).group(0)  # type: ignore[union-attr]
    rest = text[len(lead) :]
    trail_m = _TRAIL_DECOR.search(rest)
    trail = trail_m.group(0) if trail_m else ""
    core = rest[: len(rest) - len(trail)]
    for o, c in _BRACKETS:
        if core.startswith(o) and core.endswith(c) and core.count(o) == 1 and core.count(c) == 1:
            inner = core[1:-1]
            lead += o + inner[: len(inner) - len(inner.lstrip())]
            trail = inner[len(inner.rstrip()) :] + c + trail
            core = inner.strip()
            break
    if not _LETTER.search(core):
        return [text]
    return [_Segment(lead=lead, core=core, trail=trail, n_lines=n_lines, caps=_is_caps(core))]


def _line_parts(line: str) -> list[Part]:
    pieces = _TAG.split(line)
    tags = _TAG.findall(line)
    parts: list[Part] = []
    for i, piece in enumerate(pieces):
        if piece:
            parts.extend(_text_parts(piece))
        if i < len(tags):
            parts.append(tags[i])
    return parts


def _joinable(lines: list[str]) -> bool:
    """Only plain continuation text of one sentence may be joined and re-wrapped."""
    if len(lines) < 2:
        return False
    plain = [_TAG.sub("", ln).strip() for ln in lines]
    if not all(_LETTER.search(p) for p in plain):
        return False
    inner = _TAG_RUN_END.sub("", _TAG_RUN_START.sub("", "\n".join(ln.strip() for ln in lines)))
    if _TAG.search(inner):
        return False  # tags/colours between or inside lines: different speakers or styling
    if len({_is_caps(p) for p in plain}) > 1:
        return False
    if any(p.startswith(_LINE_START_MARKERS) for p in plain):
        return False  # dialogue dashes, songs, sound effects
    return not any(_SENTENCE_END.search(p) for p in plain[:-1])


def _joined_parts(lines: list[str]) -> list[Part]:
    joined = " ".join(ln.strip() for ln in lines)
    m = _TAG_RUN_START.match(joined)
    lead = m.group(0) if m else ""
    rest = joined[len(lead) :]
    m = _TAG_RUN_END.search(rest)
    trail = m.group(0) if m else ""
    body = rest[: len(rest) - len(trail)]
    return [p for p in (lead, *_text_parts(body, n_lines=len(lines)), trail) if p != ""]


@dataclass
class _Cue:
    rows: list[list[Part]]

    @property
    def segments(self) -> list[_Segment]:
        return [p for row in self.rows for p in row if isinstance(p, _Segment)]

    def rebuild(self, translations: list[str]) -> str:
        it = iter(translations)
        return "\\N".join(
            "".join(p if isinstance(p, str) else p.rebuild(next(it)) for p in row) for row in self.rows
        )


def prepare_cue(text: str, join: bool = True) -> _Cue:
    lines = _LINEBREAK.split(text)
    if join and _joinable(lines):
        return _Cue([_joined_parts(lines)])
    return _Cue([_line_parts(ln) for ln in lines])


def line_count(text: str) -> int:
    """Number of lines with visible text."""
    return sum(1 for ln in _LINEBREAK.split(text) if _TAG.sub("", ln).strip())


def rewrap(text: str, n_lines: int) -> str:
    """Split text into ``n_lines`` lines of balanced length on word boundaries."""
    words = text.split()
    if n_lines <= 1 or len(words) < 2:
        return " ".join(words)
    n = min(n_lines, len(words))
    prefix = [0]
    for w in words:
        prefix.append(prefix[-1] + len(w) + (1 if prefix[-1] else 0))
    total = prefix[-1]
    lines: list[str] = []
    start = 0
    for k in range(1, n):
        target = total * k / n
        lo, hi = start + 1, len(words) - (n - k)
        split = min(range(lo, hi + 1), key=lambda i: abs(prefix[i] - target))
        lines.append(" ".join(words[start:split]))
        start = split
    lines.append(" ".join(words[start:]))
    return "\\N".join(lines)


TranslateFn = Callable[[list[str]], Awaitable[list[str]]]


@dataclass
class TranslationResult:
    cues_total: int = 0
    cues_translated: int = 0
    sample: list[str] = field(default_factory=list)


async def _translate_cues(cues: list[_Cue], translate: TranslateFn) -> list[str]:
    unique = list(dict.fromkeys(s.query for cue in cues for s in cue.segments))
    translated = await translate(unique) if unique else []
    if len(translated) != len(unique):
        raise SubtitleError("Translation service returned an unexpected number of lines")
    lookup = dict(zip(unique, translated, strict=True))
    return [cue.rebuild([lookup[s.query] for s in cue.segments]) for cue in cues]


async def translate_subtitles(subs: pysubs2.SSAFile, translate: TranslateFn) -> TranslationResult:
    """Translate the text of every dialogue event in place. Timing is never touched."""
    events = [e for e in subs.events if not (e.is_comment or e.is_drawing or not e.text.strip())]
    originals = [e.text for e in events]
    texts = await _translate_cues([prepare_cue(t) for t in originals], translate)

    # Invariant: every visible source line has a translated line. Joined cues that lost a
    # line (e.g. a one-word translation) are redone line by line.
    redo = [i for i, (src, dst) in enumerate(zip(originals, texts, strict=True)) if line_count(src) != line_count(dst)]
    if redo:
        fixed = await _translate_cues([prepare_cue(originals[i], join=False) for i in redo], translate)
        for i, text in zip(redo, fixed, strict=True):
            if line_count(text) == line_count(originals[i]):
                texts[i] = text
            else:
                log.warning("Keeping the original text of a cue whose line structure could not be preserved")
                texts[i] = originals[i]

    for event, text in zip(events, texts, strict=True):
        event.text = text
    return TranslationResult(cues_total=len(events), cues_translated=len(events))


def sample_text(subs: pysubs2.SSAFile, max_chars: int = 600) -> str:
    """Plain text from the first cues, used for language detection."""
    out: list[str] = []
    size = 0
    for event in subs.events:
        if event.is_comment or event.is_drawing:
            continue
        plain = _LINEBREAK.sub(" ", _TAG.sub("", event.text)).strip()
        if plain:
            out.append(plain)
            size += len(plain) + 1
            if size >= max_chars:
                break
    return " ".join(out)[:max_chars]
