from __future__ import annotations

import logging
import sys

from arena_watcher.arena_client import ArenaClient
from arena_watcher.config import Config
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
    state_store = StateStore(config.state_path)
    bot = ArenaWatcherBot(config, arena_client, state_store, google_client, openai_client)
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
