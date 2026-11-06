"""Subtitle parsing/serialisation, language codes in file names, tag protection."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import charset_normalizer
import pysubs2
from pysubs2.exceptions import UnknownFPSError

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
    return text.encode("utf-8")


# --- Tag protection and rewrapping ------------------------------------------------

_TAG = re.compile(r"\{[^{}]*\}|<[^<>]+>|\\h")
_LINEBREAK = re.compile(r"\\[Nn]|\n")
_PLACEHOLDER = re.compile(r"\[\s*\[\s*(\d+)\s*\]\s*\]")


def _placeholder(i: int) -> str:
    return f"[[{i}]]"


@dataclass
class _Unit:
    """One translatable piece of text with its inline tags taken out."""

    lead: str
    trail: str
    tags: list[str]
    body: str
    n_lines: int

    def rebuild(self, translated: str) -> str:
        text = rewrap(translated, self.n_lines)
        seen: set[int] = set()

        def restore(m: re.Match[str]) -> str:
            idx = int(m.group(1))
            if idx >= len(self.tags) or idx in seen:
                return ""
            seen.add(idx)
            return self.tags[idx]

        text = _PLACEHOLDER.sub(restore, text)
        missing = "".join(t for i, t in enumerate(self.tags) if i not in seen)
        text = re.sub(r"[ \t]{2,}", " ", text).strip(" ")
        return f"{self.lead}{text}{missing}{self.trail}"


def _make_unit(text: str, n_lines: int) -> _Unit:
    pieces = _TAG.split(text)
    tags = _TAG.findall(text)
    lead_parts: list[str] = []
    while tags and not pieces[0].strip():
        lead_parts.append(pieces.pop(0) + tags.pop(0))
    trail_parts: list[str] = []
    while tags and not pieces[-1].strip():
        trail_parts.insert(0, tags.pop() + pieces.pop())
    body = pieces[0]
    for i, _tag in enumerate(tags):
        body += _placeholder(i) + pieces[i + 1]
    lead = "".join(lead_parts)
    trail = "".join(trail_parts)
    stripped = body.strip()
    lead += body[: len(body) - len(body.lstrip())]
    trail = body[len(body.rstrip()):] + trail if stripped else trail
    return _Unit(lead=lead, trail=trail, tags=tags, body=stripped, n_lines=n_lines)


@dataclass
class _Cue:
    units: list[_Unit]
    separator: str = "\\N"

    def rebuild(self, translations: list[str]) -> str:
        return self.separator.join(u.rebuild(t) for u, t in zip(self.units, translations, strict=True))


def prepare_cue(text: str) -> _Cue:
    """Split a cue into translatable units.

    Dialogue cues (every line starts with "-") are translated line by line;
    otherwise the lines are joined for better context and re-wrapped afterwards.
    """
    lines = _LINEBREAK.split(text)
    plain = [_TAG.sub("", ln).strip() for ln in lines]
    if len(lines) > 1 and all(p.startswith(("-", "–", "—")) for p in plain if p):
        return _Cue([_make_unit(ln, 1) for ln in lines])
    joined = " ".join(ln.strip() for ln in lines)
    return _Cue([_make_unit(joined, max(1, sum(1 for p in plain if p)))])


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


async def translate_subtitles(subs: pysubs2.SSAFile, translate: TranslateFn) -> TranslationResult:
    """Translate the text of every dialogue event in place. Timing is never touched."""
    work: list[tuple[pysubs2.SSAEvent, _Cue]] = []
    for event in subs.events:
        if event.is_comment or event.is_drawing or not event.text.strip():
            continue
        work.append((event, prepare_cue(event.text)))

    bodies = [u.body for _, cue in work for u in cue.units if u.body]
    translated = await translate(bodies) if bodies else []
    if len(translated) != len(bodies):
        raise SubtitleError("Translation service returned an unexpected number of lines")

    it = iter(translated)
    result = TranslationResult(cues_total=len(work))
    for event, cue in work:
        texts = [next(it) if u.body else "" for u in cue.units]
        event.text = cue.rebuild(texts)
        result.cues_translated += 1
    return result


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
