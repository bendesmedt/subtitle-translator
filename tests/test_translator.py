import httpx
import pytest
import respx

from app.translator import LibreTranslateClient, TranslatorError, is_insecure_remote

URL = "http://lt.test"


def _echo(request: httpx.Request) -> httpx.Response:
    import json

    body = json.loads(request.content)
    return httpx.Response(200, json={"translatedText": [f"T:{q}" for q in body["q"]]})


@respx.mock
async def test_batching_and_api_key():
    route = respx.post(f"{URL}/translate").mock(side_effect=_echo)
    client = LibreTranslateClient(URL, api_key="k", batch_size=2)
    out = await client.translate(["a", "b", "c", "d", "e"], "en", "fr")
    assert out == ["T:a", "T:b", "T:c", "T:d", "T:e"]
    assert route.call_count == 3
    assert b'"api_key":"k"' in route.calls[0].request.content.replace(b" ", b"")


@respx.mock
async def test_char_limit_batches():
    route = respx.post(f"{URL}/translate").mock(side_effect=_echo)
    client = LibreTranslateClient(URL, batch_size=50, max_chars=10)
    await client.translate(["x" * 6, "y" * 6, "z"], "en", "fr")
    assert route.call_count == 2


@respx.mock
async def test_retries_on_5xx():
    route = respx.post(f"{URL}/translate").mock(
        side_effect=[httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(200, json={"translatedText": ["ok"]})]
    )
    client = LibreTranslateClient(URL, retry_delay=0)
    assert await client.translate(["x"], "en", "fr") == ["ok"]
    assert route.call_count == 3


@respx.mock
async def test_gives_up_with_generic_error():
    respx.post(f"{URL}/translate").mock(return_value=httpx.Response(500, text="Traceback /secret/path"))
    client = LibreTranslateClient(URL, retries=1, retry_delay=0)
    with pytest.raises(TranslatorError) as exc:
        await client.translate(["x"], "en", "fr")
    assert "secret" not in str(exc.value)


@respx.mock
async def test_bad_api_key():
    respx.post(f"{URL}/translate").mock(return_value=httpx.Response(403, json={"error": "Invalid API key"}))
    with pytest.raises(TranslatorError, match="API key"):
        await LibreTranslateClient(URL).translate(["x"], "en", "fr")


@respx.mock
async def test_length_mismatch_rejected():
    respx.post(f"{URL}/translate").mock(return_value=httpx.Response(200, json={"translatedText": ["only one"]}))
    with pytest.raises(TranslatorError):
        await LibreTranslateClient(URL).translate(["a", "b"], "en", "fr")


@respx.mock
async def test_languages_and_detect():
    respx.get(f"{URL}/languages").mock(
        return_value=httpx.Response(200, json=[{"code": "en", "name": "English", "targets": ["fr", 3]}, "junk"])
    )
    respx.post(f"{URL}/detect").mock(return_value=httpx.Response(200, json=[{"language": "en", "confidence": 92}]))
    client = LibreTranslateClient(URL)
    assert await client.languages() == [{"code": "en", "name": "English", "targets": ["fr"]}]
    assert await client.detect("hello") == ("en", 92.0)


def test_insecure_remote():
    assert is_insecure_remote("http://translate.example.com")
    assert is_insecure_remote("http://8.8.8.8:5000")
    assert not is_insecure_remote("https://translate.example.com")
    assert not is_insecure_remote("http://libretranslate:5000")
    assert not is_insecure_remote("http://192.168.1.10:5000")
    assert not is_insecure_remote("http://localhost:5000")
    assert not is_insecure_remote("http://nas.local:5000")
