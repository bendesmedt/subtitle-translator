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


# --- Segmentation, sentence grouping and redistribution ---------------------------
#
# Tags never reach the translator. Every visible line is split into the text segments
# between tags ("slots"); tags, dialogue dashes, music notes and SDH brackets stay in
# place and are restored verbatim. Consecutive plain slots that form one sentence -
# across lines and across cues - are translated as one string ("unit") and the result
# is spread back over the slots on word boundaries. Anything unusual (sound effects,
# speaker changes, styling inside a line) is translated slot by slot.

_TAG = re.compile(r"\{[^{}]*\}|<[^<>]+>|\\h")
_LINEBREAK = re.compile(r"\\[Nn]|\n")
_HTML_TAG = re.compile(r"<\s*(/)?\s*([A-Za-z][\w-]*)")
_LETTER = re.compile(r"[^\W\d_]")
_LEAD_DECOR = re.compile(r"^\s*(?:[-–—]+\s*)?(?:[♪♫#*]+\s*)?")
_TRAIL_DECOR = re.compile(r"\s*(?:[♪♫#*]+\s*)?$")
_SENTENCE_END = re.compile(r"[.!?…。！？]+[\"'”’»)\]]*$")
_ELLIPSIS_END = re.compile(r"(?:\.\.\.|…)[\"'”’»]*$")
_DASHES = frozenset("-–—")
_NOT_PLAIN = re.compile(r"[♪♫#*()\[\]]")
_SPLIT_AFTER = re.compile(r"[,;:.!?…]['\"”’»)\]]*$")
_OPENING_PUNCT = frozenset("«“‘¿¡([\"'")
_BRACKETS = (("(", ")"), ("[", "]"))
_ABBREVIATIONS = frozenset(
    {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "prof", "vs", "mt", "sgt", "capt", "lt", "col", "gen", "rev", "fr"}
)
MAX_UNIT_CUES = 4
MAX_UNIT_CHARS = 400
MAX_CUE_TAGS = 32  # more tags than this: no grouping, keeps tag tracking linear
MAX_OVERLAP_MS = 100  # rounding in timestamps, not a second speaker


_DRAWING = re.compile(r"\{[^{}]*\\p[1-9]")


def _is_drawing(text: str) -> bool:
    """ASS vector drawing (``{\\p1}``). pysubs2's ``is_drawing`` is quadratic in the number of tags."""
    return _DRAWING.search(text) is not None


def _tail(text: str) -> str:
    """End of the text; enough for end-of-sentence checks and keeps the regexes linear."""
    return text.rstrip()[-32:]


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


@dataclass(eq=False)
class _Segment:
    """Text between tags, without decoration such as dialogue dashes, music notes or brackets."""

    lead: str
    core: str
    trail: str
    caps: bool = False

    @property
    def query(self) -> str:
        # Machine translation handles ALL-CAPS badly; translate in sentence case and re-apply caps.
        return _sentence_case(self.core) if self.caps else self.core

    def rebuild(self, translated: str, final: bool = True) -> str:
        text = _clean(translated) or self.core
        # LibreTranslate likes to end a fragment with "."; drop it when the source had no sentence end.
        if final and text.endswith(".") and not text.endswith("..") and not _SENTENCE_END.search(_tail(self.core)):
            text = text[:-1].rstrip() or text
        if self.caps:
            text = text.upper()
        return f"{self.lead}{text}{self.trail}"


Part = str | _Segment


def _text_parts(text: str) -> list[Part]:
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
    return [_Segment(lead=lead, core=core, trail=trail, caps=_is_caps(core))]


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


def line_count(text: str) -> int:
    """Number of lines with visible text."""
    return sum(1 for ln in _LINEBREAK.split(text) if _TAG.sub("", ln).strip())


@dataclass(eq=False)
class _Slot:
    """One translatable segment of one line of one cue."""

    seg: _Segment
    cue: int
    start: int  # ms
    end: int  # ms
    groupable: bool
    dash: bool
    style: tuple
    out: str = ""
    final: bool = True


@dataclass
class _Cue:
    rows: list[list[Part]]
    slots: list[_Slot] = field(default_factory=list)

    def rebuild(self) -> str:
        it = iter(self.slots)

        def render(p: Part) -> str:
            if isinstance(p, str):
                return p
            slot = next(it)
            return p.rebuild(slot.out, final=slot.final)

        return "\\N".join("".join(render(p) for p in row) for row in self.rows)


def _apply_html_tag(tag: str, open_tags: list[str]) -> None:
    m = _HTML_TAG.match(tag)
    if not m:
        return
    name = m.group(2).lower()
    if m.group(1):
        for i in range(len(open_tags) - 1, -1, -1):
            if open_tags[i].split(maxsplit=1)[0].lstrip("<").rstrip(">") == name:
                del open_tags[i]
                break
    elif not tag.rstrip().endswith("/>"):
        open_tags.append(re.sub(r"\s+", " ", tag.lower()))


def _plain(seg: _Segment) -> bool:
    lead = seg.lead.strip()
    return (
        all(c in _DASHES or c.isspace() for c in lead)
        and not seg.trail.strip()
        and not _NOT_PLAIN.search(seg.core)
    )


def _prepare_cue(index: int, event: pysubs2.SSAEvent) -> _Cue:
    cue = _Cue([_line_parts(ln) for ln in _LINEBREAK.split(event.text)])
    track = sum(1 for row in cue.rows for p in row if isinstance(p, str) and p[:1] in "<{") <= MAX_CUE_TAGS
    open_tags: list[str] = []
    lead_ass = ""
    for r, row in enumerate(cue.rows):
        segs = [p for p in row if isinstance(p, _Segment)]
        row_ok = len(segs) == 1
        dash = False
        seen = False
        for p in row:
            if isinstance(p, _Segment):
                seen = True
                dash = dash or any(c in _DASHES for c in p.lead)
                cue.slots.append(
                    _Slot(
                        seg=p,
                        cue=index,
                        start=event.start,
                        end=event.end,
                        groupable=track and row_ok and _plain(p),
                        dash=dash,
                        style=(event.style, event.name, lead_ass, tuple(open_tags)),
                    )
                )
            elif p.startswith("<"):
                if track:
                    _apply_html_tag(p, open_tags)
            elif p.startswith("{"):
                if r == 0 and not seen and not cue.slots:
                    lead_ass += p  # positioning/styling of the whole cue
                else:
                    row_ok = False  # styling change inside the text
            elif p.strip() in _DASHES and not seen:
                dash = True
            elif p.strip():
                row_ok = False  # \h, stray symbols, ...
        if not row_ok:
            for slot in cue.slots[len(cue.slots) - len(segs) :]:
                slot.groupable = False
    return cue


def _ends_sentence(text: str, following: str) -> bool:
    text = _tail(text)
    if _ELLIPSIS_END.search(text):
        nxt = following.lstrip()
        return not (nxt.startswith(("...", "…")) or nxt[:1].islower())
    if not _SENTENCE_END.search(text):
        return False
    if text.endswith("."):
        word = text.split()[-1][:-1].lower().lstrip("\"'“‘«(")
        # "Mr." and dotted abbreviations ("U.S.", "e.g.") do not end a sentence; "So am I." does
        if word in _ABBREVIATIONS or re.fullmatch(r"(?:[^\W\d_]\.)+[^\W\d_]", word):
            return False
    return True


def _continues(unit: list[_Slot], slot: _Slot, max_gap_ms: float) -> bool:
    prev = unit[-1]
    if not (prev.groupable and slot.groupable) or slot.dash:
        return False
    if slot.style != prev.style or slot.seg.caps != prev.seg.caps:
        return False
    if slot.cue != prev.cue and not -MAX_OVERLAP_MS <= slot.start - prev.end <= max_gap_ms:
        return False  # long pause, cues out of order, or overlapping cues (two speakers at once)
    if len({s.cue for s in unit} | {slot.cue}) > MAX_UNIT_CUES:
        return False
    if sum(len(s.seg.core) + 1 for s in unit) + len(slot.seg.core) > MAX_UNIT_CHARS:
        return False
    return not _ends_sentence(prev.seg.core, slot.seg.core)


def group_slots(slots: list[_Slot], max_gap_seconds: float) -> list[list[_Slot]]:
    units: list[list[_Slot]] = []
    for slot in slots:
        if units and _continues(units[-1], slot, max_gap_seconds * 1000):
            units[-1].append(slot)
        else:
            units.append([slot])
    return units


def _words(text: str) -> list[str]:
    """Words, with stand-alone punctuation (French " ?", "« ") glued to its neighbour."""
    words: list[str] = []
    pending = ""
    for token in text.split():
        if any(c.isalnum() for c in token):
            words.append(f"{pending}{token}" if pending else token)
            pending = ""
        elif all(c in _OPENING_PUNCT for c in token):
            pending = f"{pending}{token} "
        elif words:
            words[-1] = f"{words[-1]} {token}"
        else:
            pending = f"{pending}{token} "
    if pending:
        if words:
            words[-1] = f"{words[-1]} {pending.strip()}"
        else:
            words.append(pending.strip())
    return words


def distribute(text: str, weights: list[float]) -> list[str] | None:
    """Split ``text`` into ``len(weights)`` non-empty pieces on word boundaries.

    Piece lengths follow the weights (the source length of each slot); split points
    right after punctuation are preferred. Returns None when there are fewer words
    than pieces."""
    n = len(weights)
    words = _words(text)
    if n == 0 or len(words) < n:
        return None
    if n == 1:
        return [" ".join(words)]
    prefix = [0]
    for w in words:
        prefix.append(prefix[-1] + len(w) + (1 if prefix[-1] else 0))
    total = prefix[-1]
    weight_sum = sum(max(w, 1.0) for w in weights)
    bonus = min(12.0, 0.3 * total / n)
    pieces: list[str] = []
    start = 0
    cumulative = 0.0
    for k in range(1, n):
        cumulative += max(weights[k - 1], 1.0)
        target = total * cumulative / weight_sum
        lo, hi = start + 1, len(words) - (n - k)

        def cost(i: int, target: float = target) -> float:
            return abs(prefix[i] - target) - (bonus if _SPLIT_AFTER.search(words[i - 1]) else 0.0)

        split = min(range(lo, hi + 1), key=cost)
        pieces.append(" ".join(words[start:split]))
        start = split
    pieces.append(" ".join(words[start:]))
    return pieces


def _unit_query(unit: list[_Slot]) -> str:
    if len(unit) == 1:
        return unit[0].seg.query
    joined = _clean(" ".join(s.seg.core for s in unit))
    return _sentence_case(joined) if unit[0].seg.caps else joined


TranslateFn = Callable[[list[str]], Awaitable[list[str]]]


@dataclass
class TranslationResult:
    cues_total: int = 0
    cues_translated: int = 0
    sample: list[str] = field(default_factory=list)


async def _translate_units(units: list[list[_Slot]], translate: TranslateFn) -> list[list[_Slot]]:
    """Translate units and fill in every slot; returns the units that could not be split back."""
    queries = [_unit_query(u) for u in units]
    unique = list(dict.fromkeys(queries))
    translated = await translate(unique) if unique else []
    if len(translated) != len(unique):
        raise SubtitleError("Translation service returned an unexpected number of lines")
    lookup = dict(zip(unique, translated, strict=True))
    failed: list[list[_Slot]] = []
    for unit, query in zip(units, queries, strict=True):
        pieces = distribute(lookup[query], [len(s.seg.core) for s in unit]) if len(unit) > 1 else [lookup[query]]
        if pieces is None:
            failed.append(unit)
            continue
        for i, (slot, piece) in enumerate(zip(unit, pieces, strict=True)):
            slot.out, slot.final = piece, i == len(unit) - 1
    return failed


def _singles(units: list[list[_Slot]]) -> list[list[_Slot]]:
    return [[s] for u in units for s in u]


async def translate_subtitles(
    subs: pysubs2.SSAFile,
    translate: TranslateFn,
    *,
    sentence_grouping: bool = True,
    max_gap_seconds: float = 2.0,
) -> TranslationResult:
    """Translate the text of every dialogue event in place. Timing is never touched."""
    events = [e for e in subs.events if not (e.is_comment or _is_drawing(e.text) or not e.text.strip())]
    originals = [e.text for e in events]
    cues = [_prepare_cue(i, e) for i, e in enumerate(events)]
    slots = [s for cue in cues for s in cue.slots]
    units = group_slots(slots, max_gap_seconds) if sentence_grouping else [[s] for s in slots]

    failed = await _translate_units(units, translate)
    if failed:  # too few words to give every line one: translate those lines one by one
        await _translate_units(_singles(failed), translate)
    texts = [cue.rebuild() for cue in cues]

    # Invariant: every visible source line keeps a translated line.
    bad = {i for i, (src, dst) in enumerate(zip(originals, texts, strict=True)) if line_count(src) != line_count(dst)}
    if bad:
        redo = [u for u in units if len(u) > 1 and any(s.cue in bad for s in u)]
        if redo:
            await _translate_units(_singles(redo), translate)
            for i in {s.cue for u in redo for s in u}:
                texts[i] = cues[i].rebuild()
        for i in bad:
            if line_count(texts[i]) != line_count(originals[i]):
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
        if event.is_comment or _is_drawing(event.text):
            continue
        plain = _LINEBREAK.sub(" ", _TAG.sub("", event.text)).strip()
        if plain:
            out.append(plain)
            size += len(plain) + 1
            if size >= max_chars:
                break
    return " ".join(out)[:max_chars]
