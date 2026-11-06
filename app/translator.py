"""Async LibreTranslate client with batching and retries."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("subtitle_translator.translator")

_RETRY_STATUS = {429, 500, 502, 503, 504}
_LOCAL_HOST_SUFFIXES = (".local", ".lan", ".internal", ".home", ".localdomain")


class TranslatorError(Exception):
    """User-facing error (never includes raw upstream responses)."""


class TranslationCancelled(Exception):
    """Raised between batches when the job was cancelled."""


def is_insecure_remote(url: str) -> bool:
    """True when the URL uses plain http to a host that does not look local/private."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").lower()
    if host in {"localhost", "libretranslate"} or "." not in host:
        return False
    if host.endswith(_LOCAL_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return True


@dataclass
class LibreTranslateClient:
    base_url: str
    api_key: str = ""
    batch_size: int = 50
    max_chars: int = 4000
    timeout: float = 120.0
    retries: int = 3
    retry_delay: float = 1.0
    transport: httpx.AsyncBaseTransport | None = None
    _languages_cache: tuple[float, list[dict]] | None = field(default=None, repr=False)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            timeout=httpx.Timeout(self.timeout, connect=10.0),
            transport=self.transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs) -> object:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_exc = exc
                log.warning("LibreTranslate %s timed out (attempt %d)", path, attempt + 1)
            except httpx.TransportError as exc:
                last_exc = exc
                log.warning("LibreTranslate %s unreachable: %s", path, exc)
            else:
                if resp.status_code in _RETRY_STATUS:
                    last_exc = TranslatorError(f"LibreTranslate returned HTTP {resp.status_code}")
                    log.warning("LibreTranslate %s HTTP %d: %.300s", path, resp.status_code, resp.text)
                elif resp.status_code >= 400:
                    log.warning("LibreTranslate %s HTTP %d: %.300s", path, resp.status_code, resp.text)
                    if resp.status_code in (401, 403):
                        raise TranslatorError("LibreTranslate rejected the API key")
                    raise TranslatorError(f"LibreTranslate rejected the request (HTTP {resp.status_code})")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise TranslatorError("LibreTranslate returned an invalid response") from exc
            if attempt < self.retries:
                await asyncio.sleep(self.retry_delay * (2**attempt))
        if isinstance(last_exc, TranslatorError):
            raise last_exc
        if isinstance(last_exc, httpx.TimeoutException):
            raise TranslatorError("LibreTranslate timed out")
        raise TranslatorError("LibreTranslate is unreachable")

    def _with_key(self, payload: dict) -> dict:
        if self.api_key:
            payload["api_key"] = self.api_key
        return payload

    async def languages(self, use_cache: bool = True) -> list[dict]:
        now = time.monotonic()
        if use_cache and self._languages_cache and now - self._languages_cache[0] < 600:
            return self._languages_cache[1]
        async with self._client() as client:
            data = await self._request(client, "GET", "/languages")
        if not isinstance(data, list):
            raise TranslatorError("LibreTranslate returned an invalid language list")
        langs = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("code"), str):
                targets = item.get("targets")
                langs.append(
                    {
                        "code": item["code"],
                        "name": str(item.get("name", item["code"]))[:64],
                        "targets": [t for t in targets if isinstance(t, str)]
                        if isinstance(targets, list)
                        else [],
                    }
                )
        self._languages_cache = (now, langs)
        return langs

    def _batches(self, texts: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        size = 0
        for text in texts:
            if current and (len(current) >= self.batch_size or size + len(text) > self.max_chars):
                batches.append(current)
                current, size = [], 0
            current.append(text)
            size += len(text)
        if current:
            batches.append(current)
        return batches

    async def translate(
        self,
        texts: list[str],
        source: str,
        target: str,
        progress=None,
        cancelled=None,
    ) -> list[str]:
        """Translate a list of strings, preserving order."""
        out: list[str] = []
        async with self._client() as client:
            for batch in self._batches(texts):
                if cancelled and cancelled():
                    raise TranslationCancelled
                payload = self._with_key({"q": batch, "source": source, "target": target, "format": "text"})
                data = await self._request(client, "POST", "/translate", json=payload)
                translated = data.get("translatedText") if isinstance(data, dict) else None
                if isinstance(translated, str) and len(batch) == 1:
                    translated = [translated]
                if not isinstance(translated, list) or len(translated) != len(batch):
                    raise TranslatorError("LibreTranslate returned an unexpected response")
                out.extend(str(t) for t in translated)
                if progress:
                    progress(len(out), len(texts))
        return out

    async def detect(self, text: str) -> tuple[str, float] | None:
        async with self._client() as client:
            data = await self._request(client, "POST", "/detect", json=self._with_key({"q": text}))
        if isinstance(data, list) and data and isinstance(data[0], dict):
            lang = data[0].get("language")
            conf = data[0].get("confidence", 0)
            if isinstance(lang, str):
                try:
                    return lang, float(conf)
                except (TypeError, ValueError):
                    return lang, 0.0
        return None
