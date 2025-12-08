from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_STATE_PATH = Path("data/state.json")


def _split_env_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _split_env_int_list(value: Optional[str]) -> List[int]:
    raw_values = _split_env_list(value)
    integers: List[int] = []
    for raw in raw_values:
        try:
            integers.append(int(raw))
        except ValueError as exc:
            raise ValueError(
                f"Expected ADMIN_USER_IDS to contain integers but got {raw!r}."
            ) from exc
    return integers


def _load_json_env(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        raise ValueError(
            "Expected valid JSON string for configuration value but got "
            f"{value!r}."
        )


@dataclass(slots=True)
class Config:
    telegram_token: str
    arena_models_url: str
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS
    json_path: List[str] = field(default_factory=list)
    model_id_path: List[str] = field(default_factory=list)
    state_path: Path = DEFAULT_STATE_PATH
    request_headers: Dict[str, Any] = field(default_factory=dict)
    request_cookies: Dict[str, Any] = field(default_factory=dict)
    google_api_key: Optional[str] = None
    google_poll_interval_seconds: Optional[int] = None
    openai_api_key: Optional[str] = None
    openai_poll_interval_seconds: Optional[int] = None
    admin_user_ids: List[int] = field(default_factory=list)

    @classmethod
    def load_from_env(cls) -> "Config":
        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required.")

        arena_models_url = os.environ.get("ARENA_MODELS_URL")
        if not arena_models_url:
            raise RuntimeError("ARENA_MODELS_URL environment variable is required.")

        poll_interval_seconds = int(
            os.environ.get("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS)
        )

        json_path = _split_env_list(os.environ.get("ARENA_MODELS_JSON_PATH"))
        model_id_path = _split_env_list(os.environ.get("ARENA_MODEL_ID_PATH"))

        state_path_value = os.environ.get("STATE_PATH")
        state_path = Path(state_path_value) if state_path_value else DEFAULT_STATE_PATH

        headers = _load_json_env(os.environ.get("ARENA_REQUEST_HEADERS")) or {}
        cookies = _load_json_env(os.environ.get("ARENA_REQUEST_COOKIES")) or {}

        google_api_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GENAI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        )
        google_poll_interval_seconds = os.environ.get("GOOGLE_POLL_INTERVAL_SECONDS")
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        openai_poll_interval_seconds = os.environ.get("OPENAI_POLL_INTERVAL_SECONDS")
        admin_user_ids = _split_env_int_list(os.environ.get("ADMIN_USER_IDS"))

        return cls(
            telegram_token=telegram_token,
            arena_models_url=arena_models_url,
            poll_interval_seconds=poll_interval_seconds,
            json_path=json_path,
            model_id_path=model_id_path,
            state_path=state_path,
            request_headers=headers,
            request_cookies=cookies,
            google_api_key=google_api_key,
            google_poll_interval_seconds=int(google_poll_interval_seconds)
            if google_poll_interval_seconds
            else None,
            openai_api_key=openai_api_key,
            openai_poll_interval_seconds=int(openai_poll_interval_seconds)
            if openai_poll_interval_seconds
            else None,
            admin_user_ids=admin_user_ids,
        )
