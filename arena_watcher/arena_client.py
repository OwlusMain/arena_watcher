from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import cloudscraper
from requests import Response

logger = logging.getLogger(__name__)


class ArenaFetchError(RuntimeError):
    """Raised when the arena API cannot be reached or parsed."""


@dataclass(frozen=True, slots=True)
class ModelEntry:
    identifier: str
    name: str
    raw: Dict[str, Any]


def _extract_path(data: Any, path: Iterable[str]) -> Any:
    current = data
    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:  # pragma: no cover - defensive
                raise ArenaFetchError(
                    f"Path segment {part!r} is not an integer for list traversal."
                ) from exc
            try:
                current = current[index]
            except IndexError as exc:  # pragma: no cover - defensive
                raise ArenaFetchError(
                    f"Index {index} is out of range for list in path traversal."
                ) from exc
        else:
            raise ArenaFetchError(
                f"Cannot traverse path segment {part!r} in type {type(current).__name__}."
            )
        if current is None:
            return None
    return current


class ArenaClient:
    def __init__(
        self,
        models_url: str,
        json_path: Optional[List[str]] = None,
        model_id_path: Optional[List[str]] = None,
        headers: Optional[Dict[str, Any]] = None,
        cookies: Optional[Dict[str, Any]] = None,
        timeout_seconds: int = 30,
    ) -> None:
        self._scraper = cloudscraper.create_scraper()
        self._models_url = models_url
        self._json_path = json_path or []
        self._model_id_path = model_id_path or []
        self._headers = headers or {}
        self._cookies = cookies or {}
        self._timeout_seconds = timeout_seconds

    def fetch_models(self) -> List[ModelEntry]:
        try:
            response = self._scraper.get(
                self._models_url,
                headers=self._headers,
                cookies=self._cookies,
                timeout=self._timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - network failure
            raise ArenaFetchError(f"Failed to reach {self._models_url}: {exc}") from exc

        self._ensure_ok(response)

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                payload = response.json()
            except ValueError as exc:
                raise ArenaFetchError("Arena response did not contain valid JSON.") from exc
            models = _extract_path(payload, self._json_path) if self._json_path else payload
        else:
            models = self._parse_initial_models(response.text)

        if not isinstance(models, list):
            raise ArenaFetchError(
                "Arena response did not resolve to a list of models. "
                "Consider adjusting ARENA_MODELS_JSON_PATH."
            )

        entries: List[ModelEntry] = []
        for item in models:
            if not isinstance(item, dict):
                logger.debug("Skipping model entry because it is not a dict: %r", item)
                continue
            identifier = self._extract_identifier(item)
            name = self._extract_name(item, identifier)
            entries.append(ModelEntry(identifier=identifier, name=name, raw=item))
        return entries

    def _parse_initial_models(self, html: str) -> List[Dict[str, Any]]:
        array_start = html.find('initialModels\\":') + len('initialModels\\":')

        depth = 0
        array_end: Optional[int] = None
        for index in range(array_start, len(html)):
            char = html[index]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    array_end = index
                    break

        if array_end is None:
            raise ArenaFetchError("initialModels array did not terminate properly.")

        raw_segment = html[array_start : array_end + 1]
        try:
            decoded = bytes(raw_segment, "utf-8").decode("unicode_escape")
            return json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArenaFetchError("Failed to parse initialModels array.") from exc

    def _extract_identifier(self, item: Dict[str, Any]) -> str:
        if self._model_id_path:
            value = _extract_path(item, self._model_id_path)
            if not value:
                raise ArenaFetchError(
                    "Configured ARENA_MODEL_ID_PATH could not be resolved for a model."
                )
            return str(value)

        for key in ("id", "slug", "identifier", "name", "model"):
            if key in item and item[key]:
                return str(item[key])
        raise ArenaFetchError(
            "Could not determine identifier for model entry. "
            "Consider setting ARENA_MODEL_ID_PATH."
        )

    @staticmethod
    def _extract_name(item: Dict[str, Any], fallback: str) -> str:
        for key in ("name", "publicName", "displayName"):
            if key in item and item[key]:
                return str(item[key])
        return fallback

    @staticmethod
    def _ensure_ok(response: Response) -> None:
        if 200 <= response.status_code < 300:
            return
        raise ArenaFetchError(
            f"Arena responded with status {response.status_code} for {response.url}."
        )
