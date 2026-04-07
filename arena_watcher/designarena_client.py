from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List
from urllib.parse import urljoin, urlsplit

import cloudscraper

from .arena_client import ModelEntry

logger = logging.getLogger(__name__)


class DesignArenaFetchError(RuntimeError):
    """Raised when the DesignArena registry cannot be fetched or parsed."""


@dataclass(frozen=True, slots=True)
class DesignArenaClientConfig:
    base_url: str = "https://www.designarena.ai/"
    headers: Dict[str, Any] = field(default_factory=dict)
    cookies: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 30


class DesignArenaClient:
    def __init__(self, config: DesignArenaClientConfig | None = None) -> None:
        self._config = config or DesignArenaClientConfig()
        self._session = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "mobile": False}
        )

        parsed_base = urlsplit(self._config.base_url)
        origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
        self._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Referer": self._config.base_url,
                "Origin": origin,
            }
        )
        self._session.headers.update(self._config.headers)
        self._session.cookies.update(self._config.cookies)

    def fetch_models(self) -> List[ModelEntry]:
        payload = self._fetch_registry()
        raw_models = payload.get("models")
        if not isinstance(raw_models, dict):
            raise DesignArenaFetchError("DesignArena registry response did not contain a models object.")

        entries: list[ModelEntry] = []
        for fallback_identifier, raw_model in raw_models.items():
            if not isinstance(raw_model, dict):
                continue

            identifier = str(raw_model.get("id") or fallback_identifier)
            display_name = str(raw_model.get("displayName") or raw_model.get("name") or identifier)
            active = raw_model.get("active")
            if active is False:
                continue

            raw = dict(raw_model)
            raw.setdefault("id", identifier)
            raw.setdefault("displayName", display_name)
            entries.append(ModelEntry(identifier=identifier, name=display_name, raw=raw))

        if not entries:
            raise DesignArenaFetchError("No active models found in the DesignArena registry.")
        return entries

    def _fetch_registry(self) -> Dict[str, Any]:
        registry_url = urljoin(self._config.base_url, "api/registry")
        try:
            response = self._session.get(registry_url, timeout=self._config.timeout_seconds)
        except Exception as exc:  # pragma: no cover - network failure
            raise DesignArenaFetchError(f"Failed to reach {registry_url}: {exc}") from exc

        text = response.text or ""
        if self._looks_like_security_checkpoint(text):
            raise self._security_checkpoint_error(registry_url, response.status_code)
        if response.status_code < 200 or response.status_code >= 300:
            raise DesignArenaFetchError(
                f"DesignArena responded with status {response.status_code} for {response.url}."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise DesignArenaFetchError("DesignArena registry did not contain valid JSON.") from exc

        if not isinstance(payload, dict):
            raise DesignArenaFetchError("DesignArena registry returned an unexpected payload type.")
        return payload

    @staticmethod
    def _looks_like_security_checkpoint(text: str) -> bool:
        return "Vercel Security Checkpoint" in text

    @staticmethod
    def _security_checkpoint_error(url: str, status_code: int) -> DesignArenaFetchError:
        return DesignArenaFetchError(
            "DesignArena is serving a Vercel Security Checkpoint"
            f" (status {status_code}) for {url}. "
            "Provide valid DESIGNARENA_REQUEST_HEADERS and/or DESIGNARENA_REQUEST_COOKIES "
            "for this environment if the site now requires a clearance token."
        )
