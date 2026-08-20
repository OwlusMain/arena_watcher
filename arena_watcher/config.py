from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_POLL_INTERVAL_SECONDS = 30
DEFAULT_STATE_PATH = Path("data/state.json")
DEFAULT_REMOVAL_WAITLIST_SECONDS = 30 * 60


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


def _load_json_env(value: Optional[str]) -> Optional[Any]:
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
    removal_waitlist_seconds: int = DEFAULT_REMOVAL_WAITLIST_SECONDS
    json_path: List[str] = field(default_factory=list)
    model_id_path: List[str] = field(default_factory=list)
    state_path: Path = DEFAULT_STATE_PATH
    request_headers: Dict[str, Any] = field(default_factory=dict)
    request_cookies: Dict[str, Any] = field(default_factory=dict)
    arena_direct_url: Optional[str] = None
    arena_direct_request_template: Optional[Any] = None
    arena_direct_headers: Dict[str, Any] = field(default_factory=dict)
    arena_direct_cookies: Dict[str, Any] = field(default_factory=dict)
    arena_direct_bootstrap_url: Optional[str] = None
    arena_direct_recaptcha_v3_token: Optional[str] = None
    arena_direct_recaptcha_v3_token_command: Optional[str] = None
    arena_direct_text_response_path: List[str] = field(default_factory=list)
    arena_direct_image_url_response_path: List[str] = field(default_factory=list)
    arena_direct_image_base64_response_path: List[str] = field(default_factory=list)
    arena_direct_image_mime_type_response_path: List[str] = field(default_factory=list)
    arena_direct_timeout_seconds: int = 60
    google_api_key: Optional[str] = None
    google_poll_interval_seconds: Optional[int] = None
    openai_api_key: Optional[str] = None
    openai_poll_interval_seconds: Optional[int] = None
    anthropic_api_key: Optional[str] = None
    anthropic_poll_interval_seconds: Optional[int] = None
    anthropic_api_version: str = "2023-06-01"
    admin_user_ids: List[int] = field(default_factory=list)
    designarena_poll_interval_seconds: Optional[int] = None
    designarena_base_url: str = "https://www.designarena.ai/"
    designarena_request_headers: Dict[str, Any] = field(default_factory=dict)
    designarena_request_cookies: Dict[str, Any] = field(default_factory=dict)

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
        removal_waitlist_seconds = int(
            os.environ.get("REMOVAL_WAITLIST_SECONDS", DEFAULT_REMOVAL_WAITLIST_SECONDS)
        )

        json_path = _split_env_list(os.environ.get("ARENA_MODELS_JSON_PATH"))
        model_id_path = _split_env_list(os.environ.get("ARENA_MODEL_ID_PATH"))

        state_path_value = os.environ.get("STATE_PATH")
        state_path = Path(state_path_value) if state_path_value else DEFAULT_STATE_PATH

        headers = _load_json_env(os.environ.get("ARENA_REQUEST_HEADERS")) or {}
        cookies = _load_json_env(os.environ.get("ARENA_REQUEST_COOKIES")) or {}
        arena_direct_url = os.environ.get("ARENA_DIRECT_URL")
        arena_direct_request_template = _load_json_env(
            os.environ.get("ARENA_DIRECT_REQUEST_TEMPLATE")
        )
        arena_direct_headers = _load_json_env(os.environ.get("ARENA_DIRECT_HEADERS")) or headers
        arena_direct_cookies = _load_json_env(os.environ.get("ARENA_DIRECT_COOKIES")) or cookies
        arena_direct_bootstrap_url = os.environ.get("ARENA_DIRECT_BOOTSTRAP_URL")
        arena_direct_recaptcha_v3_token = os.environ.get("ARENA_DIRECT_RECAPTCHA_V3_TOKEN")
        arena_direct_recaptcha_v3_token_command = os.environ.get(
            "ARENA_DIRECT_RECAPTCHA_V3_TOKEN_COMMAND"
        )
        arena_direct_text_response_path = _split_env_list(
            os.environ.get("ARENA_DIRECT_TEXT_RESPONSE_PATH")
        )
        arena_direct_image_url_response_path = _split_env_list(
            os.environ.get("ARENA_DIRECT_IMAGE_URL_RESPONSE_PATH")
        )
        arena_direct_image_base64_response_path = _split_env_list(
            os.environ.get("ARENA_DIRECT_IMAGE_BASE64_RESPONSE_PATH")
        )
        arena_direct_image_mime_type_response_path = _split_env_list(
            os.environ.get("ARENA_DIRECT_IMAGE_MIME_TYPE_RESPONSE_PATH")
        )
        arena_direct_timeout_seconds = int(
            os.environ.get("ARENA_DIRECT_TIMEOUT_SECONDS", "60")
        )

        google_api_key = (
            os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GENAI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
        )
        google_poll_interval_seconds = os.environ.get("GOOGLE_POLL_INTERVAL_SECONDS")
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        openai_poll_interval_seconds = os.environ.get("OPENAI_POLL_INTERVAL_SECONDS")
        anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        anthropic_poll_interval_seconds = os.environ.get("ANTHROPIC_POLL_INTERVAL_SECONDS")
        anthropic_api_version = os.environ.get("ANTHROPIC_API_VERSION", "2023-06-01")
        admin_user_ids = _split_env_int_list(os.environ.get("ADMIN_USER_IDS"))
        designarena_poll_interval_seconds = os.environ.get("DESIGNARENA_POLL_INTERVAL_SECONDS")
        designarena_base_url = os.environ.get("DESIGNARENA_BASE_URL", "https://www.designarena.ai/")
        designarena_request_headers = _load_json_env(os.environ.get("DESIGNARENA_REQUEST_HEADERS")) or {}
        designarena_request_cookies = _load_json_env(os.environ.get("DESIGNARENA_REQUEST_COOKIES")) or {}

        return cls(
            telegram_token=telegram_token,
            arena_models_url=arena_models_url,
            poll_interval_seconds=poll_interval_seconds,
            removal_waitlist_seconds=removal_waitlist_seconds,
            json_path=json_path,
            model_id_path=model_id_path,
            state_path=state_path,
            request_headers=headers,
            request_cookies=cookies,
            arena_direct_url=arena_direct_url,
            arena_direct_request_template=arena_direct_request_template,
            arena_direct_headers=arena_direct_headers,
            arena_direct_cookies=arena_direct_cookies,
            arena_direct_bootstrap_url=arena_direct_bootstrap_url,
            arena_direct_recaptcha_v3_token=arena_direct_recaptcha_v3_token,
            arena_direct_recaptcha_v3_token_command=arena_direct_recaptcha_v3_token_command,
            arena_direct_text_response_path=arena_direct_text_response_path,
            arena_direct_image_url_response_path=arena_direct_image_url_response_path,
            arena_direct_image_base64_response_path=arena_direct_image_base64_response_path,
            arena_direct_image_mime_type_response_path=arena_direct_image_mime_type_response_path,
            arena_direct_timeout_seconds=arena_direct_timeout_seconds,
            google_api_key=google_api_key,
            google_poll_interval_seconds=int(google_poll_interval_seconds)
            if google_poll_interval_seconds
            else None,
            openai_api_key=openai_api_key,
            openai_poll_interval_seconds=int(openai_poll_interval_seconds)
            if openai_poll_interval_seconds
            else None,
            anthropic_api_key=anthropic_api_key,
            anthropic_poll_interval_seconds=int(anthropic_poll_interval_seconds)
            if anthropic_poll_interval_seconds
            else None,
            anthropic_api_version=anthropic_api_version,
            admin_user_ids=admin_user_ids,
            designarena_poll_interval_seconds=int(designarena_poll_interval_seconds)
            if designarena_poll_interval_seconds
            else None,
            designarena_base_url=designarena_base_url,
            designarena_request_headers=designarena_request_headers,
            designarena_request_cookies=designarena_request_cookies,
        )
