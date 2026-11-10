from pathlib import Path

import pysubs2
import pytest

from app.subtitles import (
    SubtitleError,
    decode_bytes,
    dump_subtitles,
    language_from_name,
    load_nonempty,
    load_subtitles,
    output_path,
    sample_text,
    translate_subtitles,
)

SRT = """1
00:00:01,000 --> 00:00:02,500
<i>Hello</i> there,
my friend.

2
00:00:03,000 --> 00:00:04,250
<b>Bye</b> <font color="#ff0000">now</font>

3
00:00:05,000 --> 00:00:06,000
- Are you coming?
- Yes.

4
00:00:07,000 --> 00:00:08,000
{\\an8}Top line
"""

VTT = """WEBVTT

00:00:01.000 --> 00:00:02.000
<i>Hi</i> there

00:01:03.500 --> 00:01:05.000
Second line
of text
"""

ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\i1}Hello{\\i0} world\\Nsecond line
Comment: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,do not translate
Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\\p1}m 0 0 l 100 0 100 100{\\p0}
Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,{\\pos(10,20)}Positioned
"""

MICRODVD = "{1}{1}25.000\n{25}{50}{y:i}Hello|world\n{75}{100}Plain\n"


async def fake_translate(texts: list[str]) -> list[str]:
    return [f"<<{t}>>" for t in texts]


def _times(subs: pysubs2.SSAFile) -> list[tuple[int, int]]:
    return [(e.start, e.end) for e in subs.events]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Movie.en.srt", "en"),
        ("Movie.eng.srt", "en"),
        ("Movie.English.srt", "en"),
        ("Movie.en.forced.srt", "en"),
        ("Movie.en.hi.srt", "en"),
        ("Movie.hi.srt", "hi"),
        ("Movie.2019.1080p.srt", None),
        ("Movie.srt", None),
        ("en.srt", None),
        ("Show.S01E01.pt-BR.srt", "pt-BR"),
    ],
)
def test_language_from_name(name, expected):
    assert language_from_name(Path(name)) == expected


@pytest.mark.parametrize(
    ("name", "out"),
    [
        ("Movie.en.srt", "Movie.fr.srt"),
        ("Movie.srt", "Movie.fr.srt"),
        ("Movie.eng.forced.ass", "Movie.fr.forced.ass"),
        ("Movie.2019.sub", "Movie.2019.fr.sub"),
    ],
)
def test_output_path(name, out):
    assert output_path(Path("/m") / name, "fr") == Path("/m") / out


async def _translate_one(text: str, translations: dict[str, str]) -> tuple[str, list[str]]:
    sent: list[str] = []

    async def translate(texts: list[str]) -> list[str]:
        sent.extend(texts)
        return [translations.get(t, t) for t in texts]

    subs = load_subtitles(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n".encode(), "srt", 25)
    await translate_subtitles(subs, translate)
    return subs.events[0].text, sent


async def test_tags_never_sent_to_translator():
    out, sent = await _translate_one(
        "{\\an8}<i>Hello</i> big\nworld", {"Hello": "Bonjour", "big": "grand", "world": "monde"}
    )
    assert sorted(sent) == ["Hello", "big", "world"]
    assert out == "{\\an8}<i>Bonjour</i> grand\\Nmonde"


async def test_mid_line_tags_restored_exactly():
    out, sent = await _translate_one("A <b>bold</b> word", {"A": "Un", "bold": "gras", "word": "mot"})
    assert sorted(sent) == ["A", "bold", "word"]
    assert out == "Un <b>gras</b> mot"


async def test_dialogue_lines_translated_separately():
    out, sent = await _translate_one("- Are you coming?\n- Yes.", {"Are you coming?": "Tu viens ?", "Yes.": "Oui."})
    assert sent == ["Are you coming?", "Yes."]
    assert out == "- Tu viens ?\\N- Oui."


async def test_continuation_lines_joined_and_rewrapped():
    out, sent = await _translate_one(
        "<i>I never thought that we\nwould meet again here</i>",
        {"I never thought that we would meet again here": "Je n'ai jamais pensé qu'on se reverrait ici"},
    )
    assert sent == ["I never thought that we would meet again here"]
    assert out.startswith("<i>") and out.endswith("</i>") and out.count("\\N") == 1
    assert out.replace("\\N", " ") == "<i>Je n'ai jamais pensé qu'on se reverrait ici</i>"


@pytest.mark.parametrize(("fmt", "text"), [("srt", SRT), ("vtt", VTT), ("ass", ASS), ("microdvd", MICRODVD)])
async def test_roundtrip_preserves_timing(fmt, text):
    subs = load_subtitles(text.encode(), fmt, 23.976)
    before = _times(subs)
    await translate_subtitles(subs, fake_translate)
    out = dump_subtitles(subs, fmt)
    again = load_subtitles(out, fmt, 23.976)
    assert _times(again) == before
    assert "<<" in out.decode("utf-8-sig")


async def test_srt_tags_and_lines_preserved():
    subs = load_subtitles(SRT.encode(), "srt", 25)
    await translate_subtitles(subs, fake_translate)
    out = dump_subtitles(subs, "srt").decode("utf-8-sig")
    assert "<i><<Hello>></i> <<there,>>" in out
    assert '<b><<Bye>></b> <font color="#ff0000"><<now>></font>' in out
    assert "{\\an8}<<Top line>>" in out
    first_cue = out.lstrip("\ufeff").split("\n\n")[0].splitlines()
    assert len(first_cue) == 4  # index, timing, two text lines
    assert "- <<Are you coming?>>" in out and "- <<Yes.>>" in out


async def test_ass_skips_comments_and_drawings():
    subs = load_subtitles(ASS.encode(), "ass", 25)
    result = await translate_subtitles(subs, fake_translate)
    assert result.cues_translated == 2
    out = dump_subtitles(subs, "ass").decode("utf-8-sig")
    assert "do not translate" in out and "<<do not translate>>" not in out
    assert "{\\p1}m 0 0 l 100 0 100 100{\\p0}" in out
    assert "{\\pos(10,20)}<<Positioned>>" in out
    assert "Style: Default,Arial,48" in out


def test_microdvd_without_fps_uses_default():
    subs = load_subtitles(b"{25}{50}Hello\n", "microdvd", 25.0)
    assert subs.events[0].start == 1000
    out = dump_subtitles(subs, "microdvd").decode("utf-8-sig")
    assert out.startswith("{25}{50}")


def test_vobsub_and_non_microdvd_rejected():
    with pytest.raises(SubtitleError):
        load_subtitles(b"\x00\x00\x01\xba binary", "microdvd", 25)
    with pytest.raises(SubtitleError):
        load_subtitles(b"[INFORMATION]\nsubviewer", "microdvd", 25)


async def test_non_utf8_input_written_as_utf8():
    text = "1\n00:00:01,000 --> 00:00:02,000\nCa coûte très cher, déjà réglé à l'hôtel.\n"
    data = text.encode("cp1252")
    assert decode_bytes(data) == text
    subs = load_subtitles(data, "srt", 25)
    await translate_subtitles(subs, fake_translate)
    out = dump_subtitles(subs, "srt")
    assert "coûte" in out.decode("utf-8")


def test_unparseable_file():
    with pytest.raises(SubtitleError):
        load_nonempty(b"not an ass file at all", "ass", 25)


def test_sample_text():
    subs = load_subtitles(SRT.encode(), "srt", 25)
    sample = sample_text(subs)
    assert sample.startswith("Hello there, my friend.")
    assert "<" not in sample
