from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from openai import OpenAI

from .arena_client import ModelEntry

logger = logging.getLogger(__name__)


class OpenAIModelFetchError(RuntimeError):
    """Raised when the OpenAI models cannot be listed."""


@dataclass(frozen=True, slots=True)
class OpenAIModelsClientConfig:
    api_key: str


class OpenAIModelsClient:
    def __init__(self, config: OpenAIModelsClientConfig) -> None:
        self._client = OpenAI(api_key=config.api_key)

    def fetch_models(self) -> List[ModelEntry]:
        try:
            pager = self._client.models.list()
        except Exception as exc:  # pragma: no cover - network failure
            raise OpenAIModelFetchError(f"Failed to list OpenAI models: {exc}") from exc

        entries: List[ModelEntry] = []
        for model in pager:
            model_id = getattr(model, "id", None)
            if not model_id:
                logger.debug("Skipping OpenAI model because it has no id: %r", model)
                continue
            raw_payload = model.model_dump() if hasattr(model, "model_dump") else {"id": model_id}
            entries.append(ModelEntry(identifier=str(model_id), name=str(model_id), raw=raw_payload))
        return entries
