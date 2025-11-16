from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)


def _normalize_capability_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, list):
        normalized = [str(item) for item in value if isinstance(item, str) and item]
        return normalized
    return None


@dataclass(slots=True)
class TrackedModel:
    name: str
    input_capabilities: Optional[List[str]] = None
    output_capabilities: Optional[List[str]] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "input_capabilities": self.input_capabilities,
            "output_capabilities": self.output_capabilities,
        }

    @classmethod
    def from_json(cls, data: Any) -> "TrackedModel":
        if not isinstance(data, dict):
            return cls(name=str(data))
        name = str(data.get("name") or "unknown")
        return cls(
            name=name,
            input_capabilities=_normalize_capability_list(data.get("input_capabilities")),
            output_capabilities=_normalize_capability_list(data.get("output_capabilities")),
        )


@dataclass(slots=True)
class WatcherState:
    known_models: Dict[str, TrackedModel] = field(default_factory=dict)
    google_models: Dict[str, TrackedModel] = field(default_factory=dict)
    chats: Set[int] = field(default_factory=set)

    def to_json(self) -> Dict[str, Any]:
        return {
            "known_models": {
                identifier: model.to_json()
                for identifier, model in sorted(self.known_models.items())
            },
            "google_models": {
                identifier: model.to_json()
                for identifier, model in sorted(self.google_models.items())
            },
            "chats": sorted(self.chats),
        }

    @classmethod
    def from_json(cls, data: Dict[str, Iterable]) -> "WatcherState":
        raw_models = data.get("known_models", {})
        if isinstance(raw_models, dict):
            known_models = {
                str(identifier): TrackedModel.from_json(payload)
                for identifier, payload in raw_models.items()
            }
        elif isinstance(raw_models, list):
            known_models = {str(identifier): TrackedModel(name=str(identifier)) for identifier in raw_models}
        else:
            known_models = {}

        raw_google_models = data.get("google_models", {})
        if isinstance(raw_google_models, dict):
            google_models = {
                str(identifier): TrackedModel.from_json(payload)
                for identifier, payload in raw_google_models.items()
            }
        else:
            google_models = {}

        return cls(
            known_models=known_models,
            google_models=google_models,
            chats=set(int(chat) for chat in data.get("chats", [])),
        )


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()

    def load(self) -> WatcherState:
        with self._lock:
            if not self._path.exists():
                logger.debug("No state file at %s. Returning empty state.", self._path)
                return WatcherState()

            try:
                raw = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read state from %s: %s", self._path, exc)
                return WatcherState()

            return WatcherState.from_json(raw)

    def save(self, state: WatcherState) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self._path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(state.to_json(), indent=2))
            temp_path.replace(self._path)
