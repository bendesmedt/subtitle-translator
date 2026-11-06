"""Password hashing, server-side sessions, CSRF/origin checks, rate limiting, headers."""

from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

SESSION_COOKIE = "st_session"
CSRF_HEADER = "x-csrf-token"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

_hasher = PasswordHasher()  # Argon2id with library defaults
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_credentials(
    expected_user: str, password_hash: str, username: str, password: str
) -> bool:
    """Constant-time-ish credential check: always runs one Argon2 verification."""
    user_ok = hmac.compare_digest(expected_user.encode(), username.encode())
    pw_ok = verify_password(password_hash if user_ok else _DUMMY_HASH, password)
    return user_ok and pw_ok


def password_problem(password: str, confirm: str) -> str | None:
    if password != confirm:
        return "Passwords do not match"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return "Password is too long"
    return None


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


@dataclass
class Session:
    id: str
    username: str
    csrf_token: str
    created: float
    last_seen: float


@dataclass
class SessionStore:
    """In-memory server-side sessions with idle and absolute timeouts."""

    idle_seconds: float = 12 * 3600
    absolute_seconds: float = 72 * 3600
    max_sessions: int = 50
    _sessions: dict[str, Session] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, username: str) -> Session:
        now = time.monotonic()
        sess = Session(
            id=secrets.token_urlsafe(32),
            username=username,
            csrf_token=secrets.token_urlsafe(32),
            created=now,
            last_seen=now,
        )
        with self._lock:
            self._purge(now)
            while len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                del self._sessions[oldest.id]
            self._sessions[sess.id] = sess
        return sess

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        now = time.monotonic()
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            if self._expired(sess, now):
                del self._sessions[session_id]
                return None
            sess.last_seen = now
            return sess

    def delete(self, session_id: str | None) -> None:
        if session_id:
            with self._lock:
                self._sessions.pop(session_id, None)

    def delete_all(self, except_id: str | None = None) -> None:
        with self._lock:
            for sid in list(self._sessions):
                if sid != except_id:
                    del self._sessions[sid]

    def _expired(self, sess: Session, now: float) -> bool:
        return (
            now - sess.last_seen > self.idle_seconds
            or now - sess.created > self.absolute_seconds
        )

    def _purge(self, now: float) -> None:
        for sid in [s.id for s in self._sessions.values() if self._expired(s, now)]:
            del self._sessions[sid]


@dataclass
class _IpState:
    failures: int = 0
    blocked_until: float = 0.0
    last_failure: float = 0.0


@dataclass
class LoginRateLimiter:
    """Per-IP exponential backoff plus a global lockout after many failures."""

    free_attempts: int = 3
    max_backoff: float = 900.0
    global_window: float = 900.0
    global_max_failures: int = 30
    global_lockout: float = 900.0
    _ips: dict[str, _IpState] = field(default_factory=dict)
    _global_failures: list[float] = field(default_factory=list)
    _global_blocked_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def retry_after(self, ip: str) -> float:
        """Seconds the client must wait before trying again (0 = allowed)."""
        now = time.monotonic()
        with self._lock:
            wait = max(0.0, self._global_blocked_until - now)
            state = self._ips.get(ip)
            if state:
                wait = max(wait, state.blocked_until - now)
            return wait

    def failure(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._ips) > 10_000:
                cutoff = now - self.max_backoff * 2
                self._ips = {k: v for k, v in self._ips.items() if v.last_failure > cutoff}
            state = self._ips.setdefault(ip, _IpState())
            state.failures += 1
            state.last_failure = now
            if state.failures >= self.free_attempts:
                delay = min(2.0 ** (state.failures - self.free_attempts), self.max_backoff)
                state.blocked_until = now + delay
            self._global_failures = [
                t for t in self._global_failures if now - t < self.global_window
            ]
            self._global_failures.append(now)
            if len(self._global_failures) >= self.global_max_failures:
                self._global_blocked_until = now + self.global_lockout
                self._global_failures.clear()

    def success(self, ip: str) -> None:
        with self._lock:
            self._ips.pop(ip, None)


def origin_allowed(headers: dict[str, str], allowed_origins: frozenset[str]) -> bool:
    """Check that a state-changing request comes from our own origin.

    Uses Origin (or Referer as fallback) and compares its host with the Host /
    X-Forwarded-Host header or the explicitly configured ALLOWED_ORIGINS.
    """
    source = headers.get("origin") or headers.get("referer")
    if not source or source == "null":
        return False
    parts = urlsplit(source)
    if not parts.scheme or not parts.netloc:
        return False
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if origin in allowed_origins:
        return True
    hosts = {headers.get("host", "").lower()}
    fwd = headers.get("x-forwarded-host")
    if fwd:
        hosts.add(fwd.split(",")[0].strip().lower())
    hosts.discard("")
    return parts.netloc.lower() in hosts


CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
]


class SecurityMiddleware:
    """Pure ASGI middleware: security headers, no-store caching, body size limit."""

    def __init__(self, app, max_body_bytes: int = 1_000_000) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        no_store = not path.startswith("/static/")

        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    too_big = int(value) > self.max_body_bytes
                except ValueError:
                    too_big = True
                if too_big:
                    await _send_simple(send, 413, b'{"detail":"Request body too large"}')
                    return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyTooLarge
            return message

        response_started = False

        async def send_with_headers(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {h for h, _ in SECURITY_HEADERS}
                ]
                headers.extend(SECURITY_HEADERS)
                if no_store:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            if not response_started:
                await _send_simple(send, 413, b'{"detail":"Request body too large"}')


class _BodyTooLarge(Exception):
    pass


async def _send_simple(send, status: int, body: bytes) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        *SECURITY_HEADERS,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
