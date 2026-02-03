from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

import requests

from .arena_client import ModelEntry

logger = logging.getLogger(__name__)


class AnthropicModelFetchError(RuntimeError):
    """Raised when the Anthropic models cannot be listed."""


@dataclass(frozen=True, slots=True)
class AnthropicModelsClientConfig:
    api_key: str
    api_version: str = "2023-06-01"
    base_url: str = "https://api.anthropic.com"
    timeout_seconds: int = 30
    page_limit: int = 1000


class AnthropicModelsClient:
    def __init__(self, config: AnthropicModelsClientConfig) -> None:
        self._config = config
        self._session = requests.Session()
        self._session.headers.update(
            {
                "x-api-key": config.api_key,
                "anthropic-version": config.api_version,
            }
        )

    def fetch_models(self) -> List[ModelEntry]:
        entries: List[ModelEntry] = []
        after_id: Optional[str] = None
        models_url = self._config.base_url.rstrip("/") + "/v1/models"

        for _ in range(100):
            params: dict[str, str | int] = {"limit": self._config.page_limit}
            if after_id:
                params["after_id"] = after_id

            try:
                response = self._session.get(
                    models_url,
                    params=params,
                    timeout=self._config.timeout_seconds,
                )
            except Exception as exc:  # pragma: no cover - network failure
                raise AnthropicModelFetchError(
                    f"Failed to reach Anthropic models API: {exc}"
                ) from exc

            if response.status_code < 200 or response.status_code >= 300:
                raise AnthropicModelFetchError(
                    "Anthropic responded with status "
                    f"{response.status_code} for {response.url}."
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise AnthropicModelFetchError(
                    "Anthropic models API did not return valid JSON."
                ) from exc

            data = payload.get("data")
            if not isinstance(data, list):
                raise AnthropicModelFetchError(
                    "Anthropic models API did not return a list of models."
                )

            for model in data:
                if not isinstance(model, dict):
                    logger.debug("Skipping Anthropic model because it is not a dict: %r", model)
                    continue
                model_id = model.get("id")
                if not model_id:
                    logger.debug("Skipping Anthropic model because it has no id: %r", model)
                    continue
                display_name = model.get("display_name") or model_id
                entries.append(
                    ModelEntry(
                        identifier=str(model_id),
                        name=str(display_name),
                        raw=model,
                    )
                )

            has_more = payload.get("has_more")
            last_id = payload.get("last_id")
            if not has_more or not last_id:
                break
            after_id = str(last_id)
        else:
            logger.warning("Stopped fetching Anthropic models after 100 pages.")

        return entries
