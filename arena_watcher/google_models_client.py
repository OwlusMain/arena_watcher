from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from google import genai

from .arena_client import ModelEntry

logger = logging.getLogger(__name__)


class GoogleModelFetchError(RuntimeError):
    """Raised when the Google Generative AI models cannot be listed."""


@dataclass(frozen=True, slots=True)
class GoogleModelsClientConfig:
    api_key: str


class GoogleModelsClient:
    def __init__(self, config: GoogleModelsClientConfig) -> None:
        self._client = genai.Client(api_key=config.api_key)

    def fetch_models(self) -> List[ModelEntry]:
        try:
            pager = self._client.models.list()
        except Exception as exc:  # pragma: no cover - network failure
            raise GoogleModelFetchError(f"Failed to list Google models: {exc}") from exc

        entries: List[ModelEntry] = []
        for model in pager:
            name = getattr(model, "name", None)
            if not name:
                logger.debug("Skipping Google model because it has no name: %r", model)
                continue
            raw_payload = model.to_dict() if hasattr(model, "to_dict") else {"name": name}
            entries.append(ModelEntry(identifier=str(name), name=str(name), raw=raw_payload))
        return entries
