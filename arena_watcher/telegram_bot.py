from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from dataclasses import dataclass
from html import escape
from types import SimpleNamespace
from typing import Any, Optional, Sequence

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.request import HTTPXRequest
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
    JobQueue,
)

from .arena_client import ArenaClient, ArenaFetchError, ModelEntry
from .arena_direct_client import ArenaDirectClient, ArenaDirectProbeError
from .anthropic_models_client import AnthropicModelFetchError, AnthropicModelsClient
from .config import Config
from .crowd_api import CrowdApi, RateLimiter
from .crowd_ledger import CrowdLedger
from .google_models_client import GoogleModelFetchError, GoogleModelsClient
from .model_probe import ModelProbeResult, infer_probe_kind, probe_prompt_for
from .openai_models_client import OpenAIModelFetchError, OpenAIModelsClient
from .designarena_client import DesignArenaClient, DesignArenaFetchError
from .state_store import StateStore, TrackedModel, WatcherState
from .telegram_messages import split_html_message, split_text_message


# A getUpdates long poll finishes (with updates, empty or with an error) at least
# every ~15 s. If none has finished for this long, polling is stuck: the process
# exits so systemd restarts it (Restart=on-failure).
POLLING_STALL_SECONDS = 5 * 60


class PollingActivityRequest(HTTPXRequest):
    """getUpdates request that records when the last long poll finished."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.last_finished = time.monotonic()

    async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
        try:
            return await super().do_request(*args, **kwargs)
        finally:
            self.last_finished = time.monotonic()


@dataclass(slots=True)
class CapabilityDiff:
    identifier: str
    model: TrackedModel
    input_added: list[str]
    input_removed: list[str]
    output_added: list[str]
    output_removed: list[str]

    def has_changes(self) -> bool:
        return any(
            (
                self.input_added,
                self.input_removed,
                self.output_added,
                self.output_removed,
            )
        )


@dataclass(frozen=True, slots=True)
class ArenaModelChange:
    identifier: str
    model: TrackedModel


@dataclass(frozen=True, slots=True)
class ArenaProbeNotification:
    identifier: str
    model: TrackedModel
    result: ModelProbeResult

logger = logging.getLogger(__name__)


class ArenaWatcherBot:
    _CHANNEL_ACTIVE_STATUSES = {
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.OWNER,
    }
    _CHANNEL_INACTIVE_STATUSES = {
        ChatMemberStatus.LEFT,
        ChatMemberStatus.BANNED,
    }

    def __init__(
        self,
        config: Config,
        arena_client: ArenaClient,
        state_store: StateStore,
        arena_direct_client: ArenaDirectClient | None = None,
        google_models_client: GoogleModelsClient | None = None,
        openai_models_client: OpenAIModelsClient | None = None,
        anthropic_models_client: AnthropicModelsClient | None = None,
        designarena_client: DesignArenaClient | None = None,
    ) -> None:
        self._config = config
        self._arena_client = arena_client
        self._arena_direct_client = arena_direct_client
        self._google_client = google_models_client
        self._openai_client = openai_models_client
        self._anthropic_client = anthropic_models_client
        self._designarena_client = designarena_client
        self._store = state_store
        self._state = self._store.load()
        self._state_lock = asyncio.Lock()
        self._last_snapshot: dict[str, TrackedModel] = dict(self._state.known_models)
        self._admin_user_ids: set[int] = set(config.admin_user_ids)
        self._crowd_ledger = CrowdLedger(
            self._state.crowd, config.crowd_quorum, config.crowd_trusted_installs
        )
        self._crowd_runner: web.AppRunner | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # Caps review DMs so a burst of fake installs cannot flood the admins;
        # anything beyond it is still listed by /crowd.
        self._crowd_review_limiter = RateLimiter(capacity=20, per_seconds=3600)
        self._polling_request = PollingActivityRequest(connection_pool_size=1)
        self._app: Application = (
            ApplicationBuilder()
            .token(config.telegram_token)
            .rate_limiter(AIORateLimiter(max_retries=3))
            .job_queue(JobQueue())
            .get_updates_request(self._polling_request)
            .post_init(self._on_startup)
            .post_shutdown(self._on_shutdown)
            .build()
        )
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("stop", self._handle_stop))
        self._app.add_handler(CommandHandler("tag", self._handle_tag))
        self._app.add_handler(CommandHandler("crowd", self._handle_crowd_status))
        self._app.add_handler(CommandHandler("crowdtrust", self._handle_crowd_trust))
        self._app.add_handler(CommandHandler("crowdban", self._handle_crowd_ban))
        self._app.add_handler(CallbackQueryHandler(self._handle_crowd_callback, pattern=r"^crowd:"))
        self._app.add_handler(
            ChatMemberHandler(self._handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
        )

        job_queue = self._app.job_queue
        if job_queue is None:  # pragma: no cover - guard for PTB configuration changes
            raise RuntimeError("Job queue is not available in this Application configuration.")

        job_queue.run_repeating(
            self._check_polling,
            interval=60,
            first=POLLING_STALL_SECONDS,
            name="polling-watchdog",
        )
        job_queue.run_repeating(
            self._poll_arena,
            interval=self._config.poll_interval_seconds,
            first=0,
            name="arena-poller",
        )
        if self._google_client:
            job_queue.run_repeating(
                self._poll_google_models,
                interval=self._config.google_poll_interval_seconds
                or self._config.poll_interval_seconds,
                first=5,
                name="google-model-poller",
            )
        if self._openai_client:
            job_queue.run_repeating(
                self._poll_openai_models,
                interval=self._config.openai_poll_interval_seconds
                or self._config.poll_interval_seconds,
                first=10,
                name="openai-model-poller",
            )
        if self._anthropic_client:
            job_queue.run_repeating(
                self._poll_anthropic_models,
                interval=self._config.anthropic_poll_interval_seconds
                or self._config.poll_interval_seconds,
                first=12,
                name="anthropic-model-poller",
            )
        if self._designarena_client:
            job_queue.run_repeating(
                self._poll_designarena_models,
                interval=self._config.designarena_poll_interval_seconds
                or self._config.poll_interval_seconds,
                first=15,
                name="designarena-model-poller",
            )

    async def _check_polling(self, _: CallbackContext) -> None:
        stalled_for = time.monotonic() - self._polling_request.last_finished
        if stalled_for < POLLING_STALL_SECONDS:
            return
        logger.critical(
            "No getUpdates call has finished for %.0f s; Telegram polling is stuck. "
            "Exiting so systemd restarts the bot.",
            stalled_for,
        )
        logging.shutdown()
        os._exit(1)

    async def _on_startup(self, _: Application) -> None:
        logger.info("Arena watcher bot started with %d stored chats.", len(self._state.chats))
        if self._config.crowd_api_port and self._config.crowd_api_secret:
            api = CrowdApi(
                secret=self._config.crowd_api_secret,
                pow_bits=self._config.crowd_pow_bits,
                models_provider=self._crowd_models_payload,
                sightings_handler=self._handle_crowd_sightings,
                is_banned=self._crowd_ledger.is_banned,
            )
            self._crowd_runner = web.AppRunner(api.build_app(), access_log=None)
            await self._crowd_runner.setup()
            try:
                await web.TCPSite(
                    self._crowd_runner, self._config.crowd_api_host, self._config.crowd_api_port
                ).start()
            except OSError:
                # The API is an add-on; keep the watcher running without it.
                logger.exception("Crowd API failed to start; continuing without it.")
                return
            logger.info(
                "Crowd API listening on %s:%s",
                self._config.crowd_api_host,
                self._config.crowd_api_port,
            )

    async def _on_shutdown(self, _: Application) -> None:
        if self._crowd_runner:
            await self._crowd_runner.cleanup()

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        async with self._state_lock:
            if chat_id not in self._state.chats:
                self._state.chats.add(chat_id)
                self._store.save(self._state)
        await self._send_message(
            context,
            chat_id=chat_id,
            text=(
                "👋 I'll notify this chat about Battle mode model additions/removals "
                "on arena.ai."
            ),
        )

    async def _handle_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat:
            return
        chat_id = update.effective_chat.id
        async with self._state_lock:
            if chat_id in self._state.chats:
                self._state.chats.remove(chat_id)
                self._store.save(self._state)
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text="I'll stop sending Battle mode updates to this chat.",
                )
            else:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text="This chat was not subscribed to updates.",
                )

    async def _handle_tag(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user:
            return
        chat_id = chat.id
        if not self._is_admin(user.id):
            await self._send_message(
                context,
                chat_id=chat_id,
                text="You are not allowed to set model tags.",
            )
            return

        if not context.args:
            await self._send_message(
                context,
                chat_id=chat_id,
                text="Usage: /tag <identifier|name> <tag text>. Send an empty tag to clear it.",
            )
            return

        target_key = context.args[0]
        provided_tag = " ".join(context.args[1:]).strip()
        new_tag = provided_tag or None

        status: str = "not_found"
        updated_model: TrackedModel | None = None
        updated_identifier: str | None = None
        ambiguous_matches: list[tuple[str, str]] = []
        target_source: str | None = None
        previous_tag: str | None = None

        async with self._state_lock:
            target_lower = target_key.lower()
            sources: list[tuple[str, dict[str, TrackedModel]]] = [
                ("Arena", self._state.known_models),
                ("Google", self._state.google_models),
                ("OpenAI", self._state.openai_models),
                ("Anthropic", self._state.anthropic_models),
                ("DesignArena", self._state.designarena_models),
            ]
            exact_matches: list[tuple[str, TrackedModel, dict[str, TrackedModel], str]] = []
            name_matches: list[tuple[str, TrackedModel, dict[str, TrackedModel], str]] = []
            for source_name, container in sources:
                for identifier, model in container.items():
                    if identifier == target_key:
                        exact_matches.append((identifier, model, container, source_name))
                    elif model.name.lower() == target_lower:
                        name_matches.append((identifier, model, container, source_name))

            chosen: tuple[str, TrackedModel, dict[str, TrackedModel], str] | None = None
            if len(exact_matches) == 1:
                chosen = exact_matches[0]
            elif len(exact_matches) > 1:
                ambiguous_matches = [(identifier, source) for identifier, _, _, source in exact_matches]
                status = "ambiguous"
            elif len(name_matches) == 1:
                chosen = name_matches[0]
            elif len(name_matches) > 1:
                ambiguous_matches = [(identifier, source) for identifier, _, _, source in name_matches]
                status = "ambiguous"

            if chosen:
                identifier, model, container, source_name = chosen
                previous_tag = model.tag
                model.tag = new_tag
                container[identifier] = model
                self._store.save(self._state)
                updated_model = model
                updated_identifier = identifier
                target_source = source_name
                status = "updated"

        if status == "not_found":
            await self._send_message(
                context,
                chat_id=chat_id,
                text=f"Could not find a model matching {self._escape(target_key)}.",
            )
            return

        if status == "ambiguous":
            lines = "\n".join(
                f"• {self._escape(identifier)} ({source})" for identifier, source in ambiguous_matches
            )
            await self._send_message(
                context,
                chat_id=chat_id,
                text=(
                    "Multiple models matched that key. Please retry with an exact identifier:\n"
                    f"{lines}"
                ),
            )
            return

        if not updated_model or not updated_identifier:
            await self._send_message(
                context,
                chat_id=chat_id,
                text="No model was updated.",
            )
            return

        label = self._format_model_name(updated_model, updated_identifier)
        if new_tag:
            await self._send_message(
                context,
                chat_id=chat_id,
                text=f"✅ Tag added to {label} [{target_source}].",
                parse_mode="HTML",
            )
            if new_tag != previous_tag:
                await self._broadcast_tag_set(
                    context,
                    model=updated_model,
                    identifier=updated_identifier,
                    source=target_source or "unknown",
                )
        else:
            await self._send_message(
                context,
                chat_id=chat_id,
                text=f"Tag cleared for {label} [{target_source}].",
                parse_mode="HTML",
            )

    async def _handle_my_chat_member(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        chat_member_update = update.my_chat_member
        if not chat_member_update:
            return
        chat = chat_member_update.chat
        if not chat or chat.type != ChatType.CHANNEL:
            return

        chat_id = chat.id
        new_status = chat_member_update.new_chat_member.status

        if new_status in self._CHANNEL_ACTIVE_STATUSES:
            async with self._state_lock:
                if chat_id in self._state.chats:
                    return
                self._state.chats.add(chat_id)
                self._store.save(self._state)
            try:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=(
                        "Thanks for adding me! I'll post Arena Battle mode updates here. "
                        "Remove me from the channel to stop the notifications."
                    ),
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to greet channel %s: %s", chat_id, exc)
        elif new_status in self._CHANNEL_INACTIVE_STATUSES:
            async with self._state_lock:
                if chat_id in self._state.chats:
                    self._state.chats.remove(chat_id)
                    self._store.save(self._state)

    def _keep_missing_models(
        self,
        source_key: str,
        previous: dict[str, TrackedModel],
        api_snapshots: dict[str, TrackedModel],
    ) -> tuple[dict[str, TrackedModel], set[str], set[str], bool]:
        """Merge the fetched models into the known ones without ever dropping a model.

        Used while the source only exposes part of its catalog, so a model missing from
        a response says nothing about whether it was actually removed.
        """
        snapshots = dict(previous)
        snapshots.update(api_snapshots)
        waitlist_updated = self._state.removal_waitlist.pop(source_key, None) is not None
        return snapshots, set(api_snapshots) - set(previous), set(), waitlist_updated

    def _apply_removal_waitlist(
        self,
        source_key: str,
        previous: dict[str, TrackedModel],
        api_snapshots: dict[str, TrackedModel],
    ) -> tuple[dict[str, TrackedModel], set[str], set[str], bool]:
        waitlist_seconds = float(self._config.removal_waitlist_seconds)
        waitlist_updated = False
        api_added_ids = set(api_snapshots) - set(previous)
        api_removed_ids = set(previous) - set(api_snapshots)

        if waitlist_seconds <= 0:
            if source_key in self._state.removal_waitlist:
                self._state.removal_waitlist.pop(source_key, None)
                waitlist_updated = True
            return api_snapshots, api_added_ids, api_removed_ids, waitlist_updated

        now = time.time()
        existing_waitlist = self._state.removal_waitlist.get(source_key)
        if existing_waitlist is None:
            waitlist: dict[str, float] = {}
        elif isinstance(existing_waitlist, dict):
            waitlist = {}
            for identifier, timestamp in existing_waitlist.items():
                try:
                    waitlist[str(identifier)] = float(timestamp)
                except (TypeError, ValueError):
                    continue
        else:
            waitlist = {}
            waitlist_updated = True

        for identifier in list(waitlist):
            if identifier in api_snapshots:
                waitlist.pop(identifier, None)
                waitlist_updated = True

        effective_snapshots = dict(api_snapshots)
        if api_removed_ids and not api_added_ids:
            for identifier in api_removed_ids:
                if identifier in previous:
                    effective_snapshots[identifier] = previous[identifier]
                if identifier not in waitlist:
                    waitlist[identifier] = now
                    waitlist_updated = True
        else:
            for identifier in api_removed_ids:
                if identifier in waitlist:
                    waitlist.pop(identifier, None)
                    waitlist_updated = True

        expired_ids: set[str] = set()
        for identifier, timestamp in list(waitlist.items()):
            if identifier in api_snapshots:
                continue
            if now - float(timestamp) >= waitlist_seconds:
                expired_ids.add(identifier)
                waitlist.pop(identifier, None)
                waitlist_updated = True
                effective_snapshots.pop(identifier, None)

        if waitlist:
            if waitlist != (existing_waitlist or {}):
                self._state.removal_waitlist[source_key] = waitlist
                waitlist_updated = True
        else:
            if existing_waitlist:
                self._state.removal_waitlist.pop(source_key, None)
                waitlist_updated = True

        added_ids = set(effective_snapshots) - set(previous)
        removed_ids = set(previous) - set(effective_snapshots)
        return effective_snapshots, added_ids, removed_ids, waitlist_updated

    async def _poll_arena(self, context: CallbackContext) -> None:
        try:
            models = await asyncio.to_thread(self._arena_client.fetch_models)
        except ArenaFetchError as exc:
            logger.warning("Arena fetch failed: %s", exc)
            return

        logger.debug("Fetched %d models from arena.", len(models))

        async with self._state_lock:
            previous = dict(self._state.known_models)
            api_snapshots = {
                entry.identifier: self._snapshot_model(entry, previous.get(entry.identifier))
                for entry in models
            }
            if self._config.arena_removals_enabled:
                snapshots, added_ids, removed_ids, waitlist_updated = self._apply_removal_waitlist(
                    "arena", previous, api_snapshots
                )
            else:
                snapshots, added_ids, removed_ids, waitlist_updated = self._keep_missing_models(
                    "arena", previous, api_snapshots
                )
            self._last_snapshot = snapshots

            overlapping_ids = set(previous).intersection(snapshots)
            capability_updates: list[CapabilityDiff] = []
            name_updates: list[tuple[str, str, TrackedModel]] = []
            for identifier in overlapping_ids:
                diff = self._capability_changes(identifier, previous[identifier], snapshots[identifier])
                if diff.has_changes():
                    capability_updates.append(diff)
                before_name = previous[identifier].name
                after_model = snapshots[identifier]
                if before_name != after_model.name:
                    name_updates.append((identifier, before_name, after_model))

            if (
                not added_ids
                and not removed_ids
                and not capability_updates
                and not name_updates
                and not waitlist_updated
            ):
                logger.debug("No changes detected in arena models.")
                return

            added_models = sorted(
                (
                    ArenaModelChange(identifier=identifier, model=snapshots[identifier])
                    for identifier in added_ids
                ),
                key=lambda item: item.model.name.lower(),
            )
            removed_models = sorted(
                ((identifier, previous[identifier]) for identifier in removed_ids),
                key=lambda item: item[1].name.lower(),
            )

            self._state.known_models = snapshots
            self._store.save(self._state)

        if added_models or removed_models or capability_updates or name_updates:
            await self._notify_changes(
                context,
                added=added_models,
                removed=removed_models,
                capability_updates=capability_updates,
                name_updates=name_updates,
            )

    async def _poll_google_models(self, context: CallbackContext) -> None:
        if not self._google_client:
            return

        try:
            models = await asyncio.to_thread(self._google_client.fetch_models)
        except GoogleModelFetchError as exc:
            logger.warning("Google models fetch failed: %s", exc)
            return

        logger.debug("Fetched %d models from Google.", len(models))

        async with self._state_lock:
            previous = dict(self._state.google_models)
            api_snapshots = {
                entry.identifier: self._snapshot_google_model(entry, previous.get(entry.identifier))
                for entry in models
            }
            snapshots, added_ids, removed_ids, waitlist_updated = self._apply_removal_waitlist(
                "google", previous, api_snapshots
            )

            overlapping_ids = set(previous).intersection(snapshots)
            name_updates: list[tuple[str, str, TrackedModel]] = []
            for identifier in overlapping_ids:
                before_name = previous[identifier].name
                after_model = snapshots[identifier]
                if before_name != after_model.name:
                    name_updates.append((identifier, before_name, after_model))

            if not added_ids and not removed_ids and not name_updates and not waitlist_updated:
                logger.debug("No changes detected in Google model list.")
                return

            added_models = sorted(
                (snapshots[identifier] for identifier in added_ids),
                key=lambda model: model.name.lower(),
            )
            removed_models = sorted(
                ((identifier, previous[identifier]) for identifier in removed_ids),
                key=lambda item: item[1].name.lower(),
            )

            self._state.google_models = snapshots
            self._store.save(self._state)

        if added_models or removed_models or name_updates:
            await self._notify_google_changes(
                context,
                added=added_models,
                removed=removed_models,
                name_updates=name_updates,
            )

    async def _poll_openai_models(self, context: CallbackContext) -> None:
        if not self._openai_client:
            return

        try:
            models = await asyncio.to_thread(self._openai_client.fetch_models)
        except OpenAIModelFetchError as exc:
            logger.warning("OpenAI models fetch failed: %s", exc)
            return

        logger.debug("Fetched %d models from OpenAI.", len(models))

        async with self._state_lock:
            previous = dict(self._state.openai_models)
            api_snapshots = {
                entry.identifier: self._snapshot_openai_model(entry, previous.get(entry.identifier))
                for entry in models
            }
            snapshots, added_ids, removed_ids, waitlist_updated = self._apply_removal_waitlist(
                "openai", previous, api_snapshots
            )

            overlapping_ids = set(previous).intersection(snapshots)
            name_updates: list[tuple[str, str, TrackedModel]] = []
            for identifier in overlapping_ids:
                before_name = previous[identifier].name
                after_model = snapshots[identifier]
                if before_name != after_model.name:
                    name_updates.append((identifier, before_name, after_model))

            if not added_ids and not removed_ids and not name_updates and not waitlist_updated:
                logger.debug("No changes detected in OpenAI model list.")
                return

            added_models = sorted(
                (snapshots[identifier] for identifier in added_ids),
                key=lambda model: model.name.lower(),
            )
            removed_models = sorted(
                ((identifier, previous[identifier]) for identifier in removed_ids),
                key=lambda item: item[1].name.lower(),
            )

            self._state.openai_models = snapshots
            self._store.save(self._state)

        if added_models or removed_models or name_updates:
            await self._notify_openai_changes(
                context,
                added=added_models,
                removed=removed_models,
                name_updates=name_updates,
            )

    async def _poll_anthropic_models(self, context: CallbackContext) -> None:
        if not self._anthropic_client:
            return

        try:
            models = await asyncio.to_thread(self._anthropic_client.fetch_models)
        except AnthropicModelFetchError as exc:
            logger.warning("Anthropic models fetch failed: %s", exc)
            return

        logger.debug("Fetched %d models from Anthropic.", len(models))

        async with self._state_lock:
            previous = dict(self._state.anthropic_models)
            api_snapshots = {
                entry.identifier: self._snapshot_anthropic_model(entry, previous.get(entry.identifier))
                for entry in models
            }
            snapshots, added_ids, removed_ids, waitlist_updated = self._apply_removal_waitlist(
                "anthropic", previous, api_snapshots
            )

            overlapping_ids = set(previous).intersection(snapshots)
            name_updates: list[tuple[str, str, TrackedModel]] = []
            for identifier in overlapping_ids:
                before_name = previous[identifier].name
                after_model = snapshots[identifier]
                if before_name != after_model.name:
                    name_updates.append((identifier, before_name, after_model))

            if not added_ids and not removed_ids and not name_updates and not waitlist_updated:
                logger.debug("No changes detected in Anthropic model list.")
                return

            added_models = sorted(
                (snapshots[identifier] for identifier in added_ids),
                key=lambda model: model.name.lower(),
            )
            removed_models = sorted(
                ((identifier, previous[identifier]) for identifier in removed_ids),
                key=lambda item: item[1].name.lower(),
            )

            self._state.anthropic_models = snapshots
            self._store.save(self._state)

        if added_models or removed_models or name_updates:
            await self._notify_anthropic_changes(
                context,
                added=added_models,
                removed=removed_models,
                name_updates=name_updates,
            )

    async def _poll_designarena_models(self, context: CallbackContext) -> None:
        if not self._designarena_client:
            return

        try:
            models = await asyncio.to_thread(self._designarena_client.fetch_models)
        except DesignArenaFetchError as exc:
            logger.warning("DesignArena models fetch failed: %s", exc)
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected DesignArena parsing error: %s", exc)
            return

        logger.debug("Fetched %d models from DesignArena.", len(models))

        async with self._state_lock:
            previous = dict(self._state.designarena_models)
            api_snapshots = {
                entry.identifier: self._snapshot_designarena_model(entry, previous.get(entry.identifier))
                for entry in models
            }
            snapshots, added_ids, removed_ids, waitlist_updated = self._apply_removal_waitlist(
                "designarena", previous, api_snapshots
            )

            overlapping_ids = set(previous).intersection(snapshots)
            name_updates: list[tuple[str, str, TrackedModel]] = []
            for identifier in overlapping_ids:
                before_name = previous[identifier].name
                after_model = snapshots[identifier]
                if before_name != after_model.name:
                    name_updates.append((identifier, before_name, after_model))

            if not added_ids and not removed_ids and not name_updates and not waitlist_updated:
                logger.debug("No changes detected in DesignArena model list.")
                return

            added_models = sorted(
                (snapshots[identifier] for identifier in added_ids),
                key=lambda model: model.name.lower(),
            )
            removed_models = sorted(
                ((identifier, previous[identifier]) for identifier in removed_ids),
                key=lambda item: item[1].name.lower(),
            )

            self._state.designarena_models = snapshots
            self._store.save(self._state)

        if added_models or removed_models or name_updates:
            await self._notify_designarena_changes(
                context,
                added=added_models,
                removed=removed_models,
                name_updates=name_updates,
            )

    def _snapshot_model(self, entry: ModelEntry, existing: TrackedModel | None = None) -> TrackedModel:
        input_caps, output_caps = self._capability_lists(entry)
        user_selectable = entry.raw.get("userSelectable") if isinstance(entry.raw, dict) else None
        return TrackedModel(
            name=entry.name,
            input_capabilities=input_caps,
            output_capabilities=output_caps,
            tag=existing.tag if existing else None,
            user_selectable=user_selectable if isinstance(user_selectable, bool) else None,
        )

    def _snapshot_google_model(self, entry: ModelEntry, existing: TrackedModel | None = None) -> TrackedModel:
        return TrackedModel(
            name=entry.name,
            output_capabilities=None,
            tag=existing.tag if existing else None,
        )

    def _snapshot_openai_model(self, entry: ModelEntry, existing: TrackedModel | None = None) -> TrackedModel:
        return TrackedModel(
            name=entry.name,
            output_capabilities=None,
            tag=existing.tag if existing else None,
        )

    def _snapshot_anthropic_model(
        self, entry: ModelEntry, existing: TrackedModel | None = None
    ) -> TrackedModel:
        return TrackedModel(
            name=entry.name,
            output_capabilities=None,
            tag=existing.tag if existing else None,
        )

    def _snapshot_designarena_model(
        self, entry: ModelEntry, existing: TrackedModel | None = None
    ) -> TrackedModel:
        return TrackedModel(
            name=entry.name,
            modes=self._designarena_modes(entry),
            tag=existing.tag if existing else None,
        )

    @staticmethod
    def _capability_changes(
        identifier: str,
        before: TrackedModel,
        after: TrackedModel,
    ) -> CapabilityDiff:
        input_added, input_removed = ArenaWatcherBot._diff_capabilities(
            before.input_capabilities, after.input_capabilities
        )
        output_added, output_removed = ArenaWatcherBot._diff_capabilities(
            before.output_capabilities, after.output_capabilities
        )
        return CapabilityDiff(
            identifier=identifier,
            model=after,
            input_added=input_added,
            input_removed=input_removed,
            output_added=output_added,
            output_removed=output_removed,
        )

    @staticmethod
    def _diff_capabilities(
        previous: Optional[Sequence[str]],
        current: Optional[Sequence[str]],
    ) -> tuple[list[str], list[str]]:
        prev_set = set(previous or [])
        curr_set = set(current or [])
        added = sorted(curr_set - prev_set)
        removed = sorted(prev_set - curr_set)
        return added, removed

    @staticmethod
    def _capability_lists(
        entry: ModelEntry,
    ) -> tuple[Optional[list[str]], Optional[list[str]]]:
        capabilities = entry.raw.get("capabilities") if isinstance(entry.raw, dict) else None
        if not isinstance(capabilities, dict):
            return None, None
        return (
            ArenaWatcherBot._truthy_capability_keys(capabilities.get("inputCapabilities")),
            ArenaWatcherBot._truthy_capability_keys(capabilities.get("outputCapabilities")),
        )

    @staticmethod
    def _truthy_capability_keys(node: Any) -> list[str]:
        if not isinstance(node, dict):
            return []
        return [str(key) for key, value in node.items() if value]

    @staticmethod
    def _designarena_modes(entry: ModelEntry) -> Optional[list[str]]:
        raw = entry.raw if isinstance(entry.raw, dict) else {}
        if not raw:
            return None

        arenas = raw.get("arenas") if isinstance(raw.get("arenas"), dict) else {}
        modes: set[str] = set()
        for namespace, raw_modes in arenas.items():
            if not isinstance(raw_modes, list):
                continue
            for mode in raw_modes:
                if not isinstance(mode, str) or not mode:
                    continue
                if namespace == "models":
                    modes.add(mode)
                else:
                    modes.add(f"{namespace}:{mode}")

        if not modes:
            return None
        return sorted(modes)

    def _format_model_name(self, model: TrackedModel, fallback_identifier: str | None = None) -> str:
        base_name = model.name or fallback_identifier or "unknown"
        formatted = self._escape(base_name)
        if model.tag:
            formatted += f" <i>({self._escape(model.tag)})</i>"
        return formatted

    def _format_name_change(
        self,
        before_name: str,
        after_model: TrackedModel,
        identifier: str,
    ) -> str:
        before = self._escape(before_name or identifier)
        after = self._format_model_name(after_model, identifier)
        return f"{before} → {after}"

    def _format_capabilities(
        self,
        input_capabilities: Optional[Sequence[str]],
        output_capabilities: Optional[Sequence[str]],
    ) -> str:
        if input_capabilities is None and output_capabilities is None:
            return ""

        def summarize(values: Optional[Sequence[str]]) -> str:
            if values is None:
                return "n/a"
            return ", ".join(self._escape(value) for value in values) if values else "none"

        input_summary = summarize(input_capabilities)
        output_summary = summarize(output_capabilities)
        return f" (input: {input_summary}; output: {output_summary})"

    def _format_modes(self, modes: Optional[Sequence[str]]) -> str:
        if modes is None:
            return ""
        if not modes:
            return " (modes: none)"
        return " (modes: " + ", ".join(self._escape(mode) for mode in modes) + ")"

    def _format_capability_change(self, diff: CapabilityDiff) -> str:
        segments = []
        input_segment = self._format_capability_delta("input", diff.input_added, diff.input_removed)
        output_segment = self._format_capability_delta("output", diff.output_added, diff.output_removed)
        for segment in (input_segment, output_segment):
            if segment:
                segments.append(segment)
        if not segments:
            return ""
        return f" ({'; '.join(segments)})"

    def _format_capability_delta(
        self,
        label: str,
        added: Sequence[str],
        removed: Sequence[str],
    ) -> str:
        fragments = []
        if added:
            fragments.append("+" + ", +".join(self._escape(item) for item in added))
        if removed:
            fragments.append("-" + ", -".join(self._escape(item) for item in removed))
        if not fragments:
            return ""
        return f"{label}: {'; '.join(fragments)}"

    async def _notify_changes(
        self,
        context: CallbackContext,
        added: Sequence[ArenaModelChange],
        removed: Sequence[tuple[str, TrackedModel]],
        capability_updates: Sequence[CapabilityDiff],
        name_updates: Sequence[tuple[str, str, TrackedModel]],
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for model changes.")
            return

        probes = await self._collect_arena_probes(added)

        added_message = ""
        if added:
            lines = "\n".join(
                f"• {self._format_model_name(item.model, item.identifier)}"
                f"{self._format_capabilities(item.model.input_capabilities, item.model.output_capabilities)}"
                for item in added
            )
            added_message = f"<b>🆕 New models on Arena:</b>\n{lines}"

        removed_message = ""
        if removed:
            lines = "\n".join(
                f"• {self._format_model_name(model, identifier)}"
                f"{self._format_capabilities(model.input_capabilities, model.output_capabilities)}"
                for identifier, model in removed
            )
            removed_message = f"<b>❌ Removed from Arena:</b>\n{lines}"

        capability_message = ""
        if capability_updates:
            lines = "\n".join(
                f"• {self._format_model_name(diff.model)}{self._format_capability_change(diff)}"
                for diff in capability_updates
            )
            capability_message = f"<b>⚙️ Capability updates on Arena:</b>\n{lines}"

        name_message = ""
        if name_updates:
            lines = "\n".join(
                f"• {self._format_name_change(before_name, after_model, identifier)}"
                for identifier, before_name, after_model in name_updates
            )
            name_message = f"<b>✏️ Name updates on Arena:</b>\n{lines}"

        message_parts = [
            part for part in (added_message, removed_message, capability_message, name_message) if part
        ]
        if not message_parts:
            return

        message = "\n\n".join(message_parts)

        for chat_id in list(self._state.chats):
            try:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                )
                await self._send_arena_probe_notifications(context, chat_id, probes)
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send update to chat %s: %s", chat_id, exc)

    async def _collect_arena_probes(
        self,
        added: Sequence[ArenaModelChange],
    ) -> list[ArenaProbeNotification]:
        if not added or not self._arena_direct_client:
            return []

        tasks = [asyncio.to_thread(self._probe_arena_model, item) for item in added]
        results = await asyncio.gather(*tasks)
        return [result for result in results if result is not None]

    def _probe_arena_model(self, item: ArenaModelChange) -> ArenaProbeNotification | None:
        if not self._arena_direct_client:
            return None

        kind = infer_probe_kind(
            item.identifier,
            item.model.name,
            input_capabilities=item.model.input_capabilities,
            output_capabilities=item.model.output_capabilities,
            modes=item.model.modes,
        )
        try:
            result = self._arena_direct_client.probe_model(item.identifier, kind)
        except ArenaDirectProbeError as exc:
            result = ModelProbeResult(
                kind=kind,
                prompt=probe_prompt_for(kind),
                error=str(exc),
            )
        except Exception as exc:  # pragma: no cover - defensive
            result = ModelProbeResult(
                kind=kind,
                prompt=probe_prompt_for(kind),
                error=f"Unexpected Arena probe error: {exc}",
            )
        return ArenaProbeNotification(identifier=item.identifier, model=item.model, result=result)

    async def _send_arena_probe_notifications(
        self,
        context: CallbackContext,
        chat_id: int,
        probes: Sequence[ArenaProbeNotification],
    ) -> None:
        for probe in probes:
            try:
                if probe.result.failed:
                    await self._send_message(
                        context,
                        chat_id=chat_id,
                        text=self._format_arena_probe_error(probe),
                        parse_mode="HTML",
                    )
                    continue

                if probe.result.kind == "text":
                    await self._send_message(
                        context,
                        chat_id=chat_id,
                        text=self._format_arena_text_probe(probe),
                        parse_mode="HTML",
                    )
                    continue

                caption = self._format_arena_image_probe_caption(probe)
                if probe.result.image_url:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=probe.result.image_url,
                        caption=caption,
                        parse_mode="HTML",
                    )
                    continue

                if probe.result.image_bytes:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=InputFile(
                            io.BytesIO(probe.result.image_bytes),
                            filename=self._probe_image_filename(probe.result.image_mime_type),
                        ),
                        caption=caption,
                        parse_mode="HTML",
                    )
                    continue

                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=self._format_arena_probe_error(
                        ArenaProbeNotification(
                            identifier=probe.identifier,
                            model=probe.model,
                            result=ModelProbeResult(
                                kind="image",
                                prompt=probe.result.prompt,
                                error="Probe completed but did not include image data.",
                            ),
                        )
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning(
                    "Failed to send Arena probe notification for %s to chat %s: %s",
                    probe.identifier,
                    chat_id,
                    exc,
                )

    def _format_arena_text_probe(self, probe: ArenaProbeNotification) -> str:
        label = self._format_model_name(probe.model, probe.identifier)
        prompt = self._escape(probe.result.prompt)
        response = self._escape(self._truncate_text(probe.result.text or "", 1500))
        return (
            f"<b>🧪 Direct Arena probe:</b>\n"
            f"Model: {label}\n"
            f"Prompt: {prompt}\n"
            f"Response:\n<pre>{response}</pre>"
        )

    def _format_arena_image_probe_caption(self, probe: ArenaProbeNotification) -> str:
        label = self._format_model_name(probe.model, probe.identifier)
        prompt = self._escape(self._truncate_text(probe.result.prompt, 300))
        return f"<b>🧪 Direct Arena image probe:</b>\nModel: {label}\nPrompt: {prompt}"

    def _format_arena_probe_error(self, probe: ArenaProbeNotification) -> str:
        label = self._format_model_name(probe.model, probe.identifier)
        prompt = self._escape(probe.result.prompt)
        error = self._escape(self._truncate_text(probe.result.error or "Unknown error.", 800))
        return (
            f"<b>⚠️ Direct Arena probe failed:</b>\n"
            f"Model: {label}\n"
            f"Prompt: {prompt}\n"
            f"Error: {error}"
        )

    @staticmethod
    def _probe_image_filename(mime_type: str | None) -> str:
        if mime_type == "image/jpeg":
            return "arena-probe.jpg"
        if mime_type == "image/webp":
            return "arena-probe.webp"
        return "arena-probe.png"

    @staticmethod
    def _truncate_text(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3].rstrip() + "..."

    async def _notify_google_changes(
        self,
        context: CallbackContext,
        added: Sequence[TrackedModel],
        removed: Sequence[tuple[str, TrackedModel]],
        name_updates: Sequence[tuple[str, str, TrackedModel]],
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for Google model changes.")
            return

        added_message = ""
        if added:
            lines = "\n".join(
                f"• {self._format_model_name(model)}"
                for model in added
            )
            added_message = f"<b>🆕 New Google AI models available:</b>\n{lines}"

        removed_message = ""
        if removed:
            lines = "\n".join(
                f"• {self._format_model_name(model, identifier)}"
                for identifier, model in removed
            )
            removed_message = f"<b>❌ Removed models from Google AI:</b>\n{lines}"

        name_message = ""
        if name_updates:
            lines = "\n".join(
                f"• {self._format_name_change(before_name, after_model, identifier)}"
                for identifier, before_name, after_model in name_updates
            )
            name_message = f"<b>✏️ Name updates on Google:</b>\n{lines}"

        message_parts = [part for part in (added_message, removed_message, name_message) if part]
        if not message_parts:
            return

        message = "\n\n".join(message_parts)

        for chat_id in list(self._state.chats):
            try:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send Google model update to chat %s: %s", chat_id, exc)

    async def _notify_openai_changes(
        self,
        context: CallbackContext,
        added: Sequence[TrackedModel],
        removed: Sequence[tuple[str, TrackedModel]],
        name_updates: Sequence[tuple[str, str, TrackedModel]],
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for OpenAI model changes.")
            return

        added_message = ""
        if added:
            lines = "\n".join(
                f"• {self._format_model_name(model)}"
                for model in added
            )
            added_message = f"<b>🆕 New OpenAI API models available:</b>\n{lines}"

        removed_message = ""
        if removed:
            lines = "\n".join(
                f"• {self._format_model_name(model, identifier)}"
                for identifier, model in removed
            )
            removed_message = f"<b>❌ Removed models from OpenAI API:</b>\n{lines}"

        name_message = ""
        if name_updates:
            lines = "\n".join(
                f"• {self._format_name_change(before_name, after_model, identifier)}"
                for identifier, before_name, after_model in name_updates
            )
            name_message = f"<b>✏️ Name updates on OpenAI:</b>\n{lines}"

        message_parts = [part for part in (added_message, removed_message, name_message) if part]
        if not message_parts:
            return

        message = "\n\n".join(message_parts)

        for chat_id in list(self._state.chats):
            try:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send OpenAI model update to chat %s: %s", chat_id, exc)

    async def _notify_anthropic_changes(
        self,
        context: CallbackContext,
        added: Sequence[TrackedModel],
        removed: Sequence[tuple[str, TrackedModel]],
        name_updates: Sequence[tuple[str, str, TrackedModel]],
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for Anthropic model changes.")
            return

        added_message = ""
        if added:
            lines = "\n".join(
                f"• {self._format_model_name(model)}"
                for model in added
            )
            added_message = f"<b>🆕 New Anthropic API models available:</b>\n{lines}"

        removed_message = ""
        if removed:
            lines = "\n".join(
                f"• {self._format_model_name(model, identifier)}"
                for identifier, model in removed
            )
            removed_message = f"<b>❌ Removed models from Anthropic API:</b>\n{lines}"

        name_message = ""
        if name_updates:
            lines = "\n".join(
                f"• {self._format_name_change(before_name, after_model, identifier)}"
                for identifier, before_name, after_model in name_updates
            )
            name_message = f"<b>✏️ Name updates on Anthropic:</b>\n{lines}"

        message_parts = [part for part in (added_message, removed_message, name_message) if part]
        if not message_parts:
            return

        message = "\n\n".join(message_parts)

        for chat_id in list(self._state.chats):
            try:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning(
                    "Failed to send Anthropic model update to chat %s: %s", chat_id, exc
                )

    async def _notify_designarena_changes(
        self,
        context: CallbackContext,
        added: Sequence[TrackedModel],
        removed: Sequence[tuple[str, TrackedModel]],
        name_updates: Sequence[tuple[str, str, TrackedModel]],
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for DesignArena model changes.")
            return

        added_message = ""
        if added:
            lines = "\n".join(
                f"• {self._format_model_name(model)}"
                f"{self._format_modes(model.modes)}"
                for model in added
            )
            added_message = f"<b>🆕 New DesignArena models available:</b>\n{lines}"

        removed_message = ""
        if removed:
            lines = "\n".join(
                f"• {self._format_model_name(model, identifier)}"
                f"{self._format_modes(model.modes)}"
                for identifier, model in removed
            )
            removed_message = f"<b>❌ Removed models from DesignArena:</b>\n{lines}"

        name_message = ""
        if name_updates:
            lines = "\n".join(
                f"• {self._format_name_change(before_name, after_model, identifier)}"
                for identifier, before_name, after_model in name_updates
            )
            name_message = f"<b>✏️ Name updates on DesignArena:</b>\n{lines}"

        message_parts = [part for part in (added_message, removed_message, name_message) if part]
        if not message_parts:
            return

        message = "\n\n".join(message_parts)

        for chat_id in list(self._state.chats):
            try:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send DesignArena model update to chat %s: %s", chat_id, exc)

    async def _broadcast_tag_set(
        self,
        context: CallbackContext,
        model: TrackedModel,
        identifier: str,
        source: str,
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for tag updates.")
            return

        label = self._format_model_name(model, identifier)
        message = f"🏷️ New tag added for {label} [{source}]."

        for chat_id in list(self._state.chats):
            try:
                await self._send_message(
                    context,
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send tag update to chat %s: %s", chat_id, exc)

    async def _send_message(
        self,
        context: CallbackContext,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        chunks = (
            split_html_message(text)
            if parse_mode == "HTML"
            else split_text_message(text)
        )
        if len(chunks) > 1:
            logger.info(
                "Splitting an oversized Telegram message into %d parts for chat %s.",
                len(chunks),
                chat_id,
            )

        for chunk in chunks:
            if parse_mode:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=parse_mode,
                )
            else:
                await context.bot.send_message(chat_id=chat_id, text=chunk)

    def _crowd_models_payload(self) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        for identifier, model in sorted(self._state.known_models.items()):
            item: dict[str, Any] = {
                "id": identifier,
                "name": model.name,
                "input": model.input_capabilities or [],
                "output": model.output_capabilities or [],
            }
            if model.user_selectable is not None:
                item["userSelectable"] = model.user_selectable
            models.append(item)
        return models

    async def _handle_crowd_sightings(
        self, install_id: str, network: str, models: list[dict[str, Any]]
    ) -> dict[str, int]:
        now = time.time()
        async with self._state_lock:
            known_ids = set(self._state.known_models)
            outcome = self._crowd_ledger.record(install_id, network, models, known_ids, now)
            pruned = self._crowd_ledger.prune(now, known_ids)
            added = [self._add_crowd_model(raw) for raw in outcome.promoted]
            if added or outcome.newly_pending or pruned or outcome.known < len(models):
                self._store.save(self._state)

        if added or outcome.newly_pending:
            logger.info(
                "Crowd sighting from %s: %d published, %d pending review.",
                install_id,
                len(added),
                len(outcome.newly_pending),
            )
            self._spawn(self._announce_crowd(added, outcome.newly_pending, install_id))
        return outcome.summary()

    def _add_crowd_model(self, raw: dict[str, Any]) -> ArenaModelChange:
        identifier = raw["id"]
        entry = ModelEntry(identifier, ArenaClient._extract_name(raw, identifier), raw)
        model = self._snapshot_model(entry)
        self._state.known_models[identifier] = model
        return ArenaModelChange(identifier=identifier, model=model)

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _announce_crowd(
        self,
        added: Sequence[ArenaModelChange],
        pending: Sequence[dict[str, Any]],
        install_id: str,
    ) -> None:
        context = SimpleNamespace(bot=self._app.bot)
        try:
            if added:
                await self._notify_changes(
                    context,
                    added=sorted(added, key=lambda item: item.model.name.lower()),
                    removed=[],
                    capability_updates=[],
                    name_updates=[],
                )
            for raw in pending:
                if not self._crowd_review_limiter.allow("review"):
                    logger.warning("Crowd review DMs are rate limited; see /crowd.")
                    break
                await self._notify_admins_pending(context, raw, install_id)
        except Exception:  # pragma: no cover - network failure
            logger.exception("Failed to announce crowd sightings.")

    def _format_crowd_model(self, raw: dict[str, Any]) -> str:
        identifier = raw["id"]
        entry = ModelEntry(identifier, ArenaClient._extract_name(raw, identifier), raw)
        model = self._snapshot_model(entry)
        lines = [
            f"Model: {self._format_model_name(model, identifier)}"
            f"{self._format_capabilities(model.input_capabilities, model.output_capabilities)}",
            f"ID: <code>{self._escape(identifier)}</code>",
        ]
        organization = raw.get("organization") or raw.get("provider")
        if organization:
            lines.append(f"Organization: {self._escape(str(organization))}")
        if raw.get("userSelectable") is False:
            lines.append("Not selectable in Direct (battle-only)")
        return "\n".join(lines)

    async def _notify_admins_pending(
        self, context: Any, raw: dict[str, Any], install_id: str
    ) -> None:
        identifier = raw["id"]
        text = (
            "<b>🕵️ Battle sighting awaiting review</b>\n"
            f"{self._format_crowd_model(raw)}\n"
            f"Reported by install <code>{self._escape(install_id)}</code> "
            f"(1/{self._crowd_ledger.quorum} confirmations)"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Publish", callback_data=f"crowd:approve:{identifier}"),
                    InlineKeyboardButton("🚫 Reject", callback_data=f"crowd:reject:{identifier}"),
                ]
            ]
        )
        for admin_id in self._admin_user_ids:
            try:
                await context.bot.send_message(
                    chat_id=admin_id, text=text, parse_mode="HTML", reply_markup=keyboard
                )
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send crowd review to admin %s: %s", admin_id, exc)

    async def _handle_crowd_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if not query or not query.data:
            return
        if not user or not self._is_admin(user.id):
            await query.answer("Not allowed.")
            return

        _, action, identifier = query.data.split(":", 2)
        now = time.time()
        added: list[ArenaModelChange] = []
        async with self._state_lock:
            if action == "approve":
                raw = self._crowd_ledger.approve(identifier, now)
                if raw and identifier not in self._state.known_models:
                    added.append(self._add_crowd_model(raw))
                status = "✅ Published" if added else "Already handled"
            else:
                self._crowd_ledger.reject(identifier, now)
                status = "🚫 Rejected"
            self._store.save(self._state)

        try:
            await query.answer(status)
        except TelegramError as exc:
            # Presses queued while the bot was not polling arrive too old to answer;
            # the decision above already stands, so keep going.
            logger.info("Could not answer crowd review button: %s", exc)
        try:
            await query.edit_message_text(
                f"{query.message.text_html}\n\n<b>{status}</b> by {self._escape(user.full_name)}",
                parse_mode="HTML",
            )
        except Exception as exc:  # pragma: no cover - network failure
            logger.warning("Failed to update crowd review message: %s", exc)
        if added:
            await self._notify_changes(
                context,
                added=added,
                removed=[],
                capability_updates=[],
                name_updates=[],
            )

    async def _handle_crowd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user or not self._is_admin(user.id):
            return
        ledger = self._crowd_ledger
        lines = [
            "<b>Battle crowd sightings</b>",
            f"Pending: {len(ledger.pending)}, published: {len(ledger.data['published'])}, "
            f"rejected: {len(ledger.data['rejected'])}",
            f"Trusted installs: {', '.join(ledger.data['trusted']) or 'none'}",
            f"Banned installs: {', '.join(ledger.data['banned']) or 'none'}",
        ]
        for identifier, entry in list(ledger.pending.items())[:15]:
            name = self._escape(str(entry["model"].get("publicName")))
            reporters = ", ".join(entry["reports"]) or "-"
            lines.append(f"• {name} <code>{identifier}</code> — {self._escape(reporters)}")
        await self._send_message(context, chat_id=chat.id, text="\n".join(lines), parse_mode="HTML")

    async def _handle_crowd_trust(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._handle_crowd_flag(update, context, "trusted")

    async def _handle_crowd_ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._handle_crowd_flag(update, context, "banned")

    async def _handle_crowd_flag(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, list_name: str
    ) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user or not self._is_admin(user.id):
            return
        if not context.args:
            await self._send_message(
                context,
                chat_id=chat.id,
                text=f"Usage: /{'crowdtrust' if list_name == 'trusted' else 'crowdban'} <install> [off]",
            )
            return
        install_id = context.args[0]
        enabled = not (len(context.args) > 1 and context.args[1].lower() == "off")
        async with self._state_lock:
            self._crowd_ledger.set_flag(list_name, install_id, enabled)
            self._store.save(self._state)
        await self._send_message(
            context,
            chat_id=chat.id,
            text=f"Install {install_id} {'added to' if enabled else 'removed from'} {list_name}.",
        )

    def _is_admin(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id in self._admin_user_ids

    @staticmethod
    def _escape(value: str) -> str:
        return escape(value, quote=False)

    def run(self) -> None:
        logger.info(
            "Starting arena watcher loop. Poll interval: %s seconds.",
            self._config.poll_interval_seconds,
        )
        self._app.run_polling()
