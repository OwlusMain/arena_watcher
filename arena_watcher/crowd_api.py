"""Public HTTP API for the Arena Any Model extension.

The extension is distributed to many people, so nothing shipped inside it is a
secret. The API is therefore built to stay cheap to serve and hard to abuse:

* an install registers once by solving a proof-of-work challenge and gets an
  HMAC-signed token (no database; installs can be banned by id);
* every endpoint is rate limited per install, per client IP and globally;
* request bodies are small and strictly validated;
* no CORS headers, so ordinary web pages cannot call the API from a browser.

Sightings only feed the bot's quorum/approval pipeline (see ``crowd_ledger``);
a single report never reaches the channel unless its install is trusted.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 32 * 1024
MAX_MODELS_PER_SIGHTING = 8
MAX_TEXT_LENGTH = 200
MAX_CAPABILITY_KEYS = 16
CHALLENGE_TTL_SECONDS = 600
MODELS_CACHE_SECONDS = 30

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CAPABILITY_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_SOLUTION_RE = re.compile(r"^[0-9a-zA-Z]{1,32}$")
CLIENT_IP = web.RequestKey("client_ip", str)


class RateLimiter:
    """Token buckets keyed by client; the least recently used keys are evicted first."""

    def __init__(self, capacity: float, per_seconds: float, max_keys: int = 50_000) -> None:
        self._capacity = capacity
        self._refill_per_second = capacity / per_seconds
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, stamp = self._buckets.pop(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - stamp) * self._refill_per_second)
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
        self._buckets[key] = (tokens, now)
        if len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        return allowed


class TokenSigner:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("CROWD_API_SECRET must be at least 32 characters long.")
        self._secret = secret.encode()

    def _mac(self, *parts: str) -> str:
        message = "|".join(parts).encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]

    def issue_token(self) -> tuple[str, str]:
        install_id = secrets.token_hex(8)
        return install_id, f"{install_id}.{self._mac('install', install_id)}"

    def verify_token(self, token: str) -> Optional[str]:
        install_id, _, mac = token.partition(".")
        if not install_id or not mac or len(token) > 64:
            return None
        if not hmac.compare_digest(mac, self._mac("install", install_id)):
            return None
        return install_id

    def issue_challenge(self, now: float) -> str:
        stamp = str(int(now))
        nonce = secrets.token_hex(8)
        return f"{stamp}.{nonce}.{self._mac('challenge', stamp, nonce)}"

    def verify_challenge(self, challenge: str, now: float) -> bool:
        parts = challenge.split(".")
        if len(parts) != 3 or len(challenge) > 80:
            return False
        stamp, nonce, mac = parts
        if not hmac.compare_digest(mac, self._mac("challenge", stamp, nonce)):
            return False
        try:
            age = now - int(stamp)
        except ValueError:
            return False
        return 0 <= age <= CHALLENGE_TTL_SECONDS


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + 8 - byte.bit_length()
    return bits


def proof_of_work_ok(challenge: str, solution: str, bits: int) -> bool:
    if not _SOLUTION_RE.match(solution):
        return False
    digest = hashlib.sha256(f"{challenge}:{solution}".encode()).digest()
    return leading_zero_bits(digest) >= bits


def client_network(ip: str) -> str:
    """Collapse an address to its /24 (IPv4) or /48 (IPv6) so one host counts once."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > MAX_TEXT_LENGTH:
        return None
    return cleaned


def _clean_capabilities(value: Any) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, bool] = {}
    for key, flag in value.items():
        if len(cleaned) >= MAX_CAPABILITY_KEYS:
            break
        if isinstance(key, str) and _CAPABILITY_KEY_RE.match(key) and flag:
            cleaned[key] = True
    return cleaned


def sanitize_model(raw: Any) -> Optional[dict[str, Any]]:
    """Reduce a reported Arena model to the fields the bot uses, or reject it."""
    if not isinstance(raw, dict):
        return None
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not _UUID_RE.match(identifier):
        return None
    public_name = _clean_text(raw.get("publicName"))
    if not public_name:
        return None
    model: dict[str, Any] = {"id": identifier, "publicName": public_name}
    for key in ("displayName", "name", "organization", "provider"):
        text = _clean_text(raw.get(key))
        if text:
            model[key] = text
    capabilities = raw.get("capabilities")
    if isinstance(capabilities, dict):
        model["capabilities"] = {
            "inputCapabilities": _clean_capabilities(capabilities.get("inputCapabilities")),
            "outputCapabilities": _clean_capabilities(capabilities.get("outputCapabilities")),
        }
    if isinstance(raw.get("userSelectable"), bool):
        model["userSelectable"] = raw["userSelectable"]
    return model


SightingsHandler = Callable[[str, str, list[dict[str, Any]]], Awaitable[dict[str, int]]]


class CrowdApi:
    def __init__(
        self,
        *,
        secret: str,
        pow_bits: int,
        models_provider: Callable[[], list[dict[str, Any]]],
        sightings_handler: SightingsHandler,
        is_banned: Callable[[str], bool],
        trust_forwarded_for: bool = True,
    ) -> None:
        self._signer = TokenSigner(secret)
        self._pow_bits = pow_bits
        self._models_provider = models_provider
        self._sightings_handler = sightings_handler
        self._is_banned = is_banned
        self._trust_forwarded_for = trust_forwarded_for
        self._used_challenges: dict[str, float] = {}
        self._models_cache: tuple[float, bytes, str] | None = None

        self._ip_limiter = RateLimiter(capacity=120, per_seconds=3600)
        self._register_limiter = RateLimiter(capacity=3, per_seconds=3600)
        self._models_limiter = RateLimiter(capacity=30, per_seconds=3600)
        self._sightings_limiter = RateLimiter(capacity=60, per_seconds=3600)
        self._global_limiter = RateLimiter(capacity=3000, per_seconds=3600)

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=MAX_BODY_BYTES, middlewares=[self._guard])
        app.router.add_get("/v1/challenge", self._challenge)
        app.router.add_post("/v1/register", self._register)
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/v1/sightings", self._sightings)
        return app

    def _client_ip(self, request: web.Request) -> str:
        peer = request.remote or ""
        forwarded = request.headers.get("X-Forwarded-For", "")
        if self._trust_forwarded_for and forwarded and peer in {"127.0.0.1", "::1"}:
            # Caddy replaces untrusted X-Forwarded-For values, so the last hop is the client.
            return forwarded.split(",")[-1].strip()
        return peer

    @web.middleware
    async def _guard(self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        ip = self._client_ip(request)
        request[CLIENT_IP] = ip
        if not self._global_limiter.allow("global") or not self._ip_limiter.allow(ip):
            return self._error(429, "rate_limited")
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if exc.status == 413:
                return self._error(413, "body_too_large")
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("Crowd API request failed: %s %s", request.method, request.path)
            return self._error(500, "internal_error")

    @staticmethod
    def _error(status: int, code: str) -> web.Response:
        return web.json_response({"error": code}, status=status)

    async def _json_body(self, request: web.Request) -> Optional[dict[str, Any]]:
        if request.content_type != "application/json":
            return None
        try:
            payload = json.loads(await request.text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _authenticate(self, request: web.Request) -> Optional[str]:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        install_id = self._signer.verify_token(header[7:].strip())
        if install_id is None or self._is_banned(install_id):
            return None
        return install_id

    async def _challenge(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"challenge": self._signer.issue_challenge(time.time()), "bits": self._pow_bits}
        )

    async def _register(self, request: web.Request) -> web.Response:
        if not self._register_limiter.allow(request[CLIENT_IP]):
            return self._error(429, "rate_limited")
        payload = await self._json_body(request)
        if payload is None:
            return self._error(400, "bad_request")
        challenge = str(payload.get("challenge") or "")
        solution = str(payload.get("solution") or "")
        now = time.time()
        self._used_challenges = {
            key: expiry for key, expiry in self._used_challenges.items() if expiry > now
        }
        if (
            not self._signer.verify_challenge(challenge, now)
            or challenge in self._used_challenges
            or not proof_of_work_ok(challenge, solution, self._pow_bits)
        ):
            return self._error(403, "bad_proof_of_work")
        self._used_challenges[challenge] = now + CHALLENGE_TTL_SECONDS
        install_id, token = self._signer.issue_token()
        logger.info("Registered crowd install %s from %s", install_id, request[CLIENT_IP])
        return web.json_response({"token": token, "install": install_id})

    async def _models(self, request: web.Request) -> web.Response:
        install_id = self._authenticate(request)
        if install_id is None:
            return self._error(401, "unauthorized")
        if not self._models_limiter.allow(install_id):
            return self._error(429, "rate_limited")

        now = time.monotonic()
        if self._models_cache is None or now - self._models_cache[0] > MODELS_CACHE_SECONDS:
            body = json.dumps({"models": self._models_provider()}, separators=(",", ":")).encode()
            self._models_cache = (now, body, hashlib.sha256(body).hexdigest()[:32])
        _, body, etag = self._models_cache
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.Response(body=body, content_type="application/json", headers={"ETag": etag})

    async def _sightings(self, request: web.Request) -> web.Response:
        install_id = self._authenticate(request)
        if install_id is None:
            return self._error(401, "unauthorized")
        if not self._sightings_limiter.allow(install_id):
            return self._error(429, "rate_limited")
        payload = await self._json_body(request)
        raw_models = payload.get("models") if payload else None
        if not isinstance(raw_models, list) or not 0 < len(raw_models) <= MAX_MODELS_PER_SIGHTING:
            return self._error(400, "bad_request")
        models = [model for model in (sanitize_model(raw) for raw in raw_models) if model]
        if not models:
            return self._error(400, "no_valid_models")
        result = await self._sightings_handler(
            install_id, client_network(request[CLIENT_IP]), models
        )
        return web.json_response(result)
