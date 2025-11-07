from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, Iterable

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    JobQueue,
)

from .arena_client import ArenaClient, ArenaFetchError, ModelEntry
from .config import Config
from .state_store import StateStore, WatcherState

logger = logging.getLogger(__name__)


class ArenaWatcherBot:
    def __init__(
        self,
        config: Config,
        arena_client: ArenaClient,
        state_store: StateStore,
    ) -> None:
        self._config = config
        self._arena_client = arena_client
        self._store = state_store
        self._state = self._store.load()
        self._state_lock = asyncio.Lock()
        self._last_snapshot: dict[str, ModelEntry] = {}
        self._app: Application = (
            ApplicationBuilder()
            .token(config.telegram_token)
            .rate_limiter(AIORateLimiter(max_retries=3))
            .job_queue(JobQueue())
            .post_init(self._on_startup)
            .build()
        )
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(CommandHandler("stop", self._handle_stop))

        job_queue = self._app.job_queue
        if job_queue is None:  # pragma: no cover - guard for PTB configuration changes
            raise RuntimeError("Job queue is not available in this Application configuration.")

        job_queue.run_repeating(
            self._poll_arena,
            interval=self._config.poll_interval_seconds,
            first=0,
            name="arena-poller",
        )

    async def _on_startup(self, _: Application) -> None:
        logger.info("Arena watcher bot started with %d stored chats.", len(self._state.chats))

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        async with self._state_lock:
            if chat_id not in self._state.chats:
                self._state.chats.add(chat_id)
                self._store.save(self._state)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "👋 I'll notify this chat about Battle mode model additions/removals "
                "on lmarena.ai.\nUse /status to see the last known models."
            ),
        )

    async def _handle_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        async with self._state_lock:
            if self._last_snapshot:
                snapshot = sorted(self._last_snapshot.values(), key=lambda m: m.name.lower())
            else:
                snapshot = []

        if snapshot:
            formatted = "\n".join(f"• {entry.name}" for entry in snapshot)
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Currently tracked models ({len(snapshot)}):\n{formatted}",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="No models tracked yet. I'll update after the first successful poll.",
            )

    async def _handle_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        async with self._state_lock:
            if chat_id in self._state.chats:
                self._state.chats.remove(chat_id)
                self._store.save(self._state)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="I'll stop sending Battle mode updates to this chat.",
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="This chat was not subscribed to updates.",
                )

    async def _poll_arena(self, context: CallbackContext) -> None:
        try:
            models = self._arena_client.fetch_models()
        except ArenaFetchError as exc:
            logger.warning("Arena fetch failed: %s", exc)
            return

        logger.debug("Fetched %d models from arena.", len(models))

        async with self._state_lock:
            previous = set(self._state.known_models)
            current = {entry.identifier: entry for entry in models}
            self._last_snapshot = current

            added_ids = set(current) - previous
            removed_ids = previous - set(current)

            if not added_ids and not removed_ids:
                logger.debug("No changes detected in arena models.")
                return

            self._state.known_models = set(current)
            self._store.save(self._state)

        await self._notify_changes(
            context,
            added=[current[identifier] for identifier in added_ids],
            removed_identifiers=removed_ids,
        )

    def _format_capabilities(self, model: ModelEntry) -> str:
        capabilities = model.raw.get("capabilities") if isinstance(model.raw, dict) else None
        if not isinstance(capabilities, dict):
            return ""

        def summarize(node: Any) -> str:
            if not isinstance(node, dict):
                return "n/a"
            enabled = [key for key, value in node.items() if value]
            return ", ".join(enabled) if enabled else "none"

        input_summary = summarize(capabilities.get("inputCapabilities"))
        output_summary = summarize(capabilities.get("outputCapabilities"))
        return f" (input: {input_summary}; output: {output_summary})"

    async def _notify_changes(
        self,
        context: CallbackContext,
        added: Sequence[ModelEntry],
        removed_identifiers: Iterable[str],
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for model changes.")
            return

        added_message = ""
        if added:
            lines = "\n".join(
                f"• {entry.name}{self._format_capabilities(entry)}" for entry in added
            )
            added_message = f"🆕 New models in Battle mode:\n{lines}"

        removed_message = ""
        removed_list = sorted(removed_identifiers)
        if removed_list:
            lines = "\n".join(f"• {identifier}" for identifier in removed_list)
            removed_message = f"❌ Removed from Battle mode:\n{lines}"

        message_parts = [part for part in (added_message, removed_message) if part]
        if not message_parts:
            return

        message = "\n\n".join(message_parts)

        for chat_id in list(self._state.chats):
            try:
                await context.bot.send_message(chat_id=chat_id, text=message)
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send update to chat %s: %s", chat_id, exc)

    def run(self) -> None:
        logger.info(
            "Starting arena watcher loop. Poll interval: %s seconds.",
            self._config.poll_interval_seconds,
        )
        self._app.run_polling()
