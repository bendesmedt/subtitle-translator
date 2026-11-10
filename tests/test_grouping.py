"""Sentence-aware grouping: units across lines and cues, redistribution and fallbacks."""

import pytest

from app.subtitles import distribute, dump_subtitles, line_count, load_subtitles, translate_subtitles


def _srt(*cues: tuple[float, float, str]) -> bytes:
    def ts(sec: float) -> str:
        ms = round(sec * 1000)
        return f"00:{ms // 60000:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, start=1)).encode()


async def _run(cues, translations=None, **kwargs):
    translations = translations or {}
    sent: list[str] = []

    async def translate(texts: list[str]) -> list[str]:
        sent.extend(texts)
        return [translations.get(t, t) for t in texts]

    subs = load_subtitles(_srt(*cues), "srt", 25)
    before = [(e.start, e.end) for e in subs.events]
    await translate_subtitles(subs, translate, **kwargs)
    assert [(e.start, e.end) for e in subs.events] == before
    for (_, _, src), event in zip(cues, subs.events, strict=True):
        assert line_count(src) == line_count(event.text)
    return [e.text for e in subs.events], sent


SPLIT = [
    (1.0, 3.0, "I told him that if he came back"),
    (3.2, 5.0, "he would regret it."),
]
SPLIT_FR = {"I told him that if he came back he would regret it.": "Je lui ai dit que s'il revenait, il le regretterait."}


async def test_sentence_split_across_cues_translated_as_one():
    out, sent = await _run(SPLIT, SPLIT_FR)
    assert sent == ["I told him that if he came back he would regret it."]
    assert out == ["Je lui ai dit que s'il revenait,", "il le regretterait."]


async def test_grouping_can_be_disabled():
    _, sent = await _run(SPLIT, SPLIT_FR, sentence_grouping=False)
    assert sent == ["I told him that if he came back", "he would regret it."]


async def test_long_pause_breaks_the_sentence():
    cues = [(1.0, 2.0, "I told him that"), (6.0, 7.0, "he would regret it.")]
    _, sent = await _run(cues)
    assert sent == ["I told him that", "he would regret it."]
    _, sent = await _run(cues, max_gap_seconds=5)
    assert sent == ["I told him that he would regret it."]


@pytest.mark.parametrize(
    ("first", "second", "joined"),
    [
        ("I met Mr.", "Smith yesterday.", True),
        ("He works for the U.S.", "government.", True),
        ("I was thinking...", "...about you.", True),
        ("I was thinking...", "about you.", True),
        ("I was thinking...", "What?", False),
        ("Stop.", "Now.", False),
        ("So am I.", "Let's go home now.", False),
        ("I want plan B.", "Then we leave.", False),
        ("Where are you", "- Home.", False),
        ("I told him", "(DOOR SHUTS)", False),
        ("I TOLD YOU", "to leave", False),
        ("<i>I told him</i>", "that he was wrong.", False),
        ("<i>I told him</i>", "<i>that he was wrong.</i>", True),
        ('<font color="#ffff00">Is anyone</font>', '<font color="#00ff00">inside?</font>', False),
        ("A <b>bold</b> idea", "for sure.", False),
        ("♪ I walk alone", "on the road ♪", False),
    ],
)
async def test_unit_boundaries(first, second, joined):
    _, sent = await _run([(1.0, 2.0, first), (2.1, 3.0, second)])
    assert (len(sent) == 1) is joined, sent


async def test_overlapping_or_unordered_cues_not_joined():
    _, sent = await _run([(1.0, 3.0, "Are you coming with us"), (1.0, 3.0, "no thanks, I am staying")])
    assert len(sent) == 2
    _, sent = await _run([(50.0, 52.0, "and he said"), (1.0, 3.0, "where were you?")])
    assert len(sent) == 2
    _, sent = await _run([(1.0, 3.05, "and he said"), (3.0, 4.0, "where were you?")])
    assert len(sent) == 1  # tiny overlap from rounding is fine


async def test_ass_speakers_with_same_style_not_joined():
    ass = (
        "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,Alice,0,0,0,,Are you coming\n"
        "Dialogue: 0,0:00:02.00,0:00:03.00,Default,Bob,0,0,0,,with us later\n"
    )
    sent: list[str] = []

    async def translate(texts):
        sent.extend(texts)
        return texts

    await translate_subtitles(load_subtitles(ass.encode(), "ass", 25), translate)
    assert sent == ["Are you coming", "with us later"]


async def test_dash_starts_unit_but_continuation_line_joins():
    out, sent = await _run(
        [(1.0, 3.0, "- Where are you\ngoing?\n- Home.")],
        {"Where are you going?": "Où vas-tu ?", "Home.": "À la maison."},
    )
    assert sent == ["Where are you going?", "Home."]
    assert out == ["- Où\\Nvas-tu ?\\N- À la maison."]


async def test_italic_sentence_across_cues_keeps_tags():
    out, sent = await _run(
        [(1.0, 2.0, "<i>I never thought</i>"), (2.0, 3.0, "<i>we would meet again.</i>")],
        {"I never thought we would meet again.": "Je n'aurais jamais cru qu'on se reverrait."},
    )
    assert sent == ["I never thought we would meet again."]
    assert out[0].startswith("<i>") and out[0].endswith("</i>")
    assert out[1].startswith("<i>") and out[1].endswith("</i>")
    assert " ".join(o[3:-4] for o in out) == "Je n'aurais jamais cru qu'on se reverrait."


async def test_all_caps_unit_translated_in_sentence_case():
    out, sent = await _run(
        [(1.0, 2.0, "I TOLD YOU"), (2.0, 3.0, "TO LEAVE!")], {"I told you to leave!": "Je t'ai dit de partir !"}
    )
    assert sent == ["I told you to leave!"]
    assert " ".join(out) == "JE T'AI DIT DE PARTIR !"


async def test_unit_is_capped_at_four_cues():
    cues = [(i, i + 0.9, f"and then word{i}") for i in range(1, 8)]
    _, sent = await _run(cues)
    assert [q.count("word") for q in sent] == [4, 3]


async def test_too_short_translation_falls_back_to_line_by_line():
    out, sent = await _run(SPLIT, {SPLIT[0][2] + " " + SPLIT[1][2]: "Non.", SPLIT[0][2]: "A", SPLIT[1][2]: "B."})
    assert sent[1:] == [SPLIT[0][2], SPLIT[1][2]]
    assert out == ["A", "B."]


async def test_empty_unit_translation_falls_back_to_line_by_line():
    joined = SPLIT[0][2] + " " + SPLIT[1][2]
    out, _ = await _run(SPLIT, {joined: ""})
    assert out == [SPLIT[0][2], SPLIT[1][2]]


async def test_srt_output_of_split_sentence():
    subs = load_subtitles(_srt(*SPLIT), "srt", 25)

    async def translate(texts):
        return [SPLIT_FR.get(t, t) for t in texts]

    await translate_subtitles(subs, translate)
    out = dump_subtitles(subs, "srt").decode("utf-8-sig")
    assert "00:00:01,000 --> 00:00:03,000\nJe lui ai dit que s'il revenait,\n" in out
    assert "00:00:03,200 --> 00:00:05,000\nil le regretterait.\n" in out


def test_distribute_follows_weights():
    assert distribute("one two three four", [1, 1]) == ["one two", "three four"]
    assert distribute("one two three four", [3, 1]) == ["one two three", "four"]
    assert distribute("one", [1, 1]) is None
    assert distribute("", [1]) is None


def test_distribute_prefers_punctuation():
    assert distribute("aaaa bbbb, cc dddd eeee", [1, 1]) == ["aaaa bbbb,", "cc dddd eeee"]


def test_distribute_keeps_french_punctuation_attached():
    assert distribute("Tu viens ? Oui", [3, 1]) == ["Tu viens ?", "Oui"]
    assert distribute("« Bonjour » dit-il", [1, 1]) == ["« Bonjour »", "dit-il"]


async def test_hostile_tag_and_punctuation_runs_stay_fast():
    import time

    bodies = ["<b>" * 20000 + "</i>" * 20000 + "Hi", "<b>" * 5000 + "x<u>" * 5000, "Hello" + "." * 20000 + "x"]
    start = time.monotonic()
    for body in bodies:
        subs = load_subtitles(_srt((1.0, 2.0, body)), "srt", 25)

        async def translate(texts):
            return [t + "." for t in texts]

        await translate_subtitles(subs, translate)
    assert time.monotonic() - start < 5


async def test_many_ass_override_blocks_stay_fast():
    import time

    subs = load_subtitles(_srt((1.0, 2.0, "{\\i1}x" * 20000)), "srt", 25)
    start = time.monotonic()

    async def translate(texts):
        return texts

    await translate_subtitles(subs, translate)
    assert time.monotonic() - start < 5
