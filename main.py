from __future__ import annotations

import logging
import sys

from arena_watcher.arena_client import ArenaClient
from arena_watcher.arena_direct_client import ArenaDirectClient, ArenaDirectClientConfig
from arena_watcher.anthropic_models_client import (
    AnthropicModelsClient,
    AnthropicModelsClientConfig,
)
from arena_watcher.config import Config
from arena_watcher.designarena_client import DesignArenaClient, DesignArenaClientConfig
from arena_watcher.google_models_client import GoogleModelsClient, GoogleModelsClientConfig
from arena_watcher.openai_models_client import OpenAIModelsClient, OpenAIModelsClientConfig
from arena_watcher.state_store import StateStore
from arena_watcher.telegram_bot import ArenaWatcherBot


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    configure_logging()
    try:
        config = Config.load_from_env()
    except Exception as exc:
        logging.error("Failed to load configuration: %s", exc)
        return 1

    arena_client = ArenaClient(
        models_url=config.arena_models_url,
        json_path=config.json_path,
        model_id_path=config.model_id_path,
        headers=config.request_headers,
        cookies=config.request_cookies,
    )
    arena_direct_client = None
    if config.arena_direct_url:
        arena_direct_client = ArenaDirectClient(
            ArenaDirectClientConfig(
                url=config.arena_direct_url,
                request_template=config.arena_direct_request_template,
                headers=config.arena_direct_headers,
                cookies=config.arena_direct_cookies,
                bootstrap_url=config.arena_direct_bootstrap_url,
                recaptcha_v3_token=config.arena_direct_recaptcha_v3_token,
                recaptcha_v3_token_command=config.arena_direct_recaptcha_v3_token_command,
                text_response_path=config.arena_direct_text_response_path,
                image_url_response_path=config.arena_direct_image_url_response_path,
                image_base64_response_path=config.arena_direct_image_base64_response_path,
                image_mime_type_response_path=config.arena_direct_image_mime_type_response_path,
                timeout_seconds=config.arena_direct_timeout_seconds,
            )
        )
    google_client = None
    if config.google_api_key:
        google_client = GoogleModelsClient(
            GoogleModelsClientConfig(
                api_key=config.google_api_key,
            )
        )
    openai_client = None
    if config.openai_api_key:
        openai_client = OpenAIModelsClient(
            OpenAIModelsClientConfig(
                api_key=config.openai_api_key,
            )
        )
    anthropic_client = None
    if config.anthropic_api_key:
        anthropic_client = AnthropicModelsClient(
            AnthropicModelsClientConfig(
                api_key=config.anthropic_api_key,
                api_version=config.anthropic_api_version,
            )
        )
    designarena_client = DesignArenaClient(
        DesignArenaClientConfig(
            base_url=config.designarena_base_url,
            headers=config.designarena_request_headers,
            cookies=config.designarena_request_cookies,
        )
    )
    state_store = StateStore(config.state_path)
    bot = ArenaWatcherBot(
        config,
        arena_client,
        state_store,
        arena_direct_client=arena_direct_client,
        google_models_client=google_client,
        openai_models_client=openai_client,
        anthropic_models_client=anthropic_client,
        designarena_client=designarena_client,
    )
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
