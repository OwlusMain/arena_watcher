from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, List, Set

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WatcherState:
    known_models: Set[str] = field(default_factory=set)
    chats: Set[int] = field(default_factory=set)

    def to_json(self) -> Dict[str, List]:
        return {
            "known_models": sorted(self.known_models),
            "chats": sorted(self.chats),
        }

    @classmethod
    def from_json(cls, data: Dict[str, Iterable]) -> "WatcherState":
        return cls(
            known_models=set(data.get("known_models", [])),
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
