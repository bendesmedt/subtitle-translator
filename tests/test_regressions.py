"""Regression tests from a real run (Maigret (2016) S01E02, en -> fr)."""

import json
import re

import httpx
import respx

from app.subtitles import dump_subtitles, line_count, load_subtitles, translate_subtitles
from app.translator import LibreTranslateClient

URL = "http://lt.test"
Y, G, C = '<font color="#ffff00">', '<font color="#00ff00">', '<font color="#00ffff">'
CUES = [
    "(METALLIC CLICK)",
    f"{Y}Is anyone inside?</font>\n{G}No, sir.</font>",
    "(CLICK)",
    f"{Y}Come on.</font>",
    "(DOG BARKS)",
    "(CLUCKING)",
    f"{Y}Kitchen.</font>",
    "(FLOORBOARDS CREEK)",
    f"(DOOR SHUTS)\n{Y}(COCKS GUN)</font>",
    f"{Y}Come out.</font>",
    f"{Y}Come out!</font>",
    "(SUSPENSE MUSIC)",
    f"(COAT HANGERS RATTLE)\n{C}(GASPS)</font>",
    "(THEME SONG PLAYS)",
    "(DISTANT CAR HORNS)",
]
SRT = "\n".join(
    f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n{text}\n" for i, text in enumerate(CUES, start=1)
).encode()

FR = {
    "Metallic click": "Clic métallique",
    "Is anyone inside?": "Il y a quelqu'un à l'intérieur ?",
    "No, sir.": "Non, monsieur.",
    "Click": "Clic",
    "Come on.": "Allez.",
    "Dog barks": "Le chien aboie",
    "Clucking": "Gloussements",
    "Kitchen.": "Cuisine.",
    "Floorboards creek": "Le plancher grince",
    "Door shuts": "La porte se ferme",
    "Cocks gun": "Arme les chiens",
    "Come out.": "Sortez.",
    "Come out!": "Sortez !",
    "Suspense music": "Musique à suspense",
    "Coat hangers rattle": "Les cintres s'entrechoquent",
    "Gasps": "Halète",
    "Theme song plays": "Générique",
    "Distant car horns": "Klaxons au loin",
}


def fake_lt(request: httpx.Request, calls: list) -> httpx.Response:
    """Behaves like LibreTranslate: ALL-CAPS and unknown text come back unchanged, the JSON
    body is raw UTF-8 (not ASCII-escaped) and tags/placeholders would be mangled."""
    body = json.loads(request.content)
    q = body["q"]
    calls.append(q)
    items = q if isinstance(q, list) else [q]
    for item in items:
        assert "<" not in item and "[[" not in item and "{" not in item, item
    out = [FR.get(item, item) for item in items]
    payload = {"translatedText": out if isinstance(q, list) else out[0]}
    return httpx.Response(
        200, content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


async def _run() -> tuple[bytes, list]:
    calls: list = []
    subs = load_subtitles(SRT, "srt", 25)
    client = LibreTranslateClient(URL, batch_size=50)
    with respx.mock:
        respx.post(f"{URL}/translate").mock(side_effect=lambda r: fake_lt(r, calls))
        await translate_subtitles(subs, lambda texts: client.translate(texts, "en", "fr"))
    return dump_subtitles(subs, "srt"), calls


def _cues(out: bytes) -> list[str]:
    text = out.decode("utf-8-sig")
    return ["\n".join(block.splitlines()[2:]) for block in text.strip().split("\n\n")]


async def test_utf8_with_bom_and_no_mojibake():
    out, _ = await _run()
    assert out.startswith(b"\xef\xbb\xbf")
    text = out[3:].decode("utf-8")  # strict: valid UTF-8, never double-encoded
    assert "MÉTALLIQUE" in text and "à l'intérieur" in text
    assert not re.search("Ã.|Â", text)


async def test_tags_restored_and_no_placeholders():
    out, _ = await _run()
    cues = _cues(out)
    assert cues[1] == f"{Y}Il y a quelqu'un à l'intérieur ?</font>\n{G}Non, monsieur.</font>"
    assert cues[3] == f"{Y}Allez.</font>"
    assert "[[" not in out.decode("utf-8-sig")


async def test_multiline_cues_keep_every_line():
    out, _ = await _run()
    cues = _cues(out)
    for src, dst in zip(CUES, cues, strict=True):
        assert line_count(src) == line_count(dst), (src, dst)
    assert cues[8] == f"(LA PORTE SE FERME)\n{Y}(ARME LES CHIENS)</font>"
    assert cues[12] == f"(LES CINTRES S'ENTRECHOQUENT)\n{C}(HALÈTE)</font>"


async def test_all_caps_sound_cues_translated_and_kept_upper():
    out, calls = await _run()
    cues = _cues(out)
    assert cues[0] == "(CLIC MÉTALLIQUE)"
    assert cues[4] == "(LE CHIEN ABOIE)"
    assert cues[14] == "(KLAXONS AU LOIN)"
    assert cues[13] == "(GÉNÉRIQUE)"
    sent = [q for call in calls for q in (call if isinstance(call, list) else [call])]
    assert "Dog barks" in sent and "DOG BARKS" not in sent
    assert all("(" not in q for q in sent)  # brackets stay out of the text sent to LT


async def test_unchanged_batch_items_retried_individually():
    attempts: list = []

    def lt(request: httpx.Request) -> httpx.Response:
        q = json.loads(request.content)["q"]
        attempts.append(q)
        if isinstance(q, list):  # batch: second item comes back untranslated
            return httpx.Response(200, json={"translatedText": ["Bonjour", q[1], ""]})
        return httpx.Response(200, json={"translatedText": {"Dog barks": "Le chien aboie", "Gasps": "Halète"}[q]})

    client = LibreTranslateClient(URL)
    with respx.mock:
        respx.post(f"{URL}/translate").mock(side_effect=lt)
        out = await client.translate(["Hello", "Dog barks", "Gasps"], "en", "fr")
    assert out == ["Bonjour", "Le chien aboie", "Halète"]
    assert attempts[1:] == ["Dog barks", "Gasps"]


async def test_joined_cue_falls_back_to_line_by_line():
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nWell, I suppose\nthat is right\n"
    subs = load_subtitles(srt, "srt", 25)

    async def translate(texts: list[str]) -> list[str]:
        if texts == ["Well, I suppose that is right"]:
            return ["D'accord"]  # one word cannot fill two lines
        return [t.upper() for t in texts]

    await translate_subtitles(subs, translate)
    assert subs.events[0].text == "WELL, I SUPPOSE\\NTHAT IS RIGHT"
