from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Optional, Sequence

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
    JobQueue,
)

from .arena_client import ArenaClient, ArenaFetchError, ModelEntry
from .config import Config
from .google_models_client import GoogleModelFetchError, GoogleModelsClient
from .openai_models_client import OpenAIModelFetchError, OpenAIModelsClient
from .designarena_client import DesignArenaClient, DesignArenaFetchError
from .state_store import StateStore, TrackedModel, WatcherState


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
        google_models_client: GoogleModelsClient | None = None,
        openai_models_client: OpenAIModelsClient | None = None,
        designarena_client: DesignArenaClient | None = None,
    ) -> None:
        self._config = config
        self._arena_client = arena_client
        self._google_client = google_models_client
        self._openai_client = openai_models_client
        self._designarena_client = designarena_client
        self._store = state_store
        self._state = self._store.load()
        self._state_lock = asyncio.Lock()
        self._last_snapshot: dict[str, TrackedModel] = dict(self._state.known_models)
        self._admin_user_ids: set[int] = set(config.admin_user_ids)
        self._app: Application = (
            ApplicationBuilder()
            .token(config.telegram_token)
            .rate_limiter(AIORateLimiter(max_retries=3))
            .job_queue(JobQueue())
            .post_init(self._on_startup)
            .build()
        )
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("stop", self._handle_stop))
        self._app.add_handler(CommandHandler("tag", self._handle_tag))
        self._app.add_handler(
            ChatMemberHandler(self._handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
        )

        job_queue = self._app.job_queue
        if job_queue is None:  # pragma: no cover - guard for PTB configuration changes
            raise RuntimeError("Job queue is not available in this Application configuration.")

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
        if self._designarena_client:
            job_queue.run_repeating(
                self._poll_designarena_models,
                interval=self._config.designarena_poll_interval_seconds
                or self._config.poll_interval_seconds,
                first=15,
                name="designarena-model-poller",
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
                "on lmarena.ai."
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
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="I'll stop sending Battle mode updates to this chat.",
                )
            else:
                await context.bot.send_message(
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
            await context.bot.send_message(
                chat_id=chat_id,
                text="You are not allowed to set model tags.",
            )
            return

        if not context.args:
            await context.bot.send_message(
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
                ("LMArena", self._state.known_models),
                ("Google", self._state.google_models),
                ("OpenAI", self._state.openai_models),
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
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Could not find a model matching {self._escape(target_key)}.",
            )
            return

        if status == "ambiguous":
            lines = "\n".join(
                f"• {self._escape(identifier)} ({source})" for identifier, source in ambiguous_matches
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Multiple models matched that key. Please retry with an exact identifier:\n"
                    f"{lines}"
                ),
            )
            return

        if not updated_model or not updated_identifier:
            await context.bot.send_message(
                chat_id=chat_id,
                text="No model was updated.",
            )
            return

        label = self._format_model_name(updated_model, updated_identifier)
        if new_tag:
            await context.bot.send_message(
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
            await context.bot.send_message(
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
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "Thanks for adding me! I'll post LMArena Battle mode updates here. "
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
            models = self._arena_client.fetch_models()
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
            snapshots, added_ids, removed_ids, waitlist_updated = self._apply_removal_waitlist(
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
                (snapshots[identifier] for identifier in added_ids),
                key=lambda model: model.name.lower(),
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
            models = self._google_client.fetch_models()
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
            models = self._openai_client.fetch_models()
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

    async def _poll_designarena_models(self, context: CallbackContext) -> None:
        if not self._designarena_client:
            return

        try:
            models = self._designarena_client.fetch_models()
        except DesignArenaFetchError as exc:
            logger.warning("DesignArena models fetch failed: %s", exc)
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
        return TrackedModel(
            name=entry.name,
            input_capabilities=input_caps,
            output_capabilities=output_caps,
            tag=existing.tag if existing else None,
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

    def _snapshot_designarena_model(
        self, entry: ModelEntry, existing: TrackedModel | None = None
    ) -> TrackedModel:
        return TrackedModel(
            name=entry.name,
            output_capabilities=None,
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
        added: Sequence[TrackedModel],
        removed: Sequence[tuple[str, TrackedModel]],
        capability_updates: Sequence[CapabilityDiff],
        name_updates: Sequence[tuple[str, str, TrackedModel]],
    ) -> None:
        if not self._state.chats:
            logger.debug("No chats to notify for model changes.")
            return

        added_message = ""
        if added:
            lines = "\n".join(
                f"• {self._format_model_name(model)}"
                f"{self._format_capabilities(model.input_capabilities, model.output_capabilities)}"
                for model in added
            )
            added_message = f"<b>🆕 New models on LMArena:</b>\n{lines}"

        removed_message = ""
        if removed:
            lines = "\n".join(
                f"• {self._format_model_name(model, identifier)}"
                f"{self._format_capabilities(model.input_capabilities, model.output_capabilities)}"
                for identifier, model in removed
            )
            removed_message = f"<b>❌ Removed from LMArena:</b>\n{lines}"

        capability_message = ""
        if capability_updates:
            lines = "\n".join(
                f"• {self._format_model_name(diff.model)}{self._format_capability_change(diff)}"
                for diff in capability_updates
            )
            capability_message = f"<b>⚙️ Capability updates on LMArena:</b>\n{lines}"

        name_message = ""
        if name_updates:
            lines = "\n".join(
                f"• {self._format_name_change(before_name, after_model, identifier)}"
                for identifier, before_name, after_model in name_updates
            )
            name_message = f"<b>✏️ Name updates on LMArena:</b>\n{lines}"

        message_parts = [
            part for part in (added_message, removed_message, capability_message, name_message) if part
        ]
        if not message_parts:
            return

        message = "\n\n".join(message_parts)

        for chat_id in list(self._state.chats):
            try:
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send update to chat %s: %s", chat_id, exc)

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
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
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
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send OpenAI model update to chat %s: %s", chat_id, exc)

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
                for model in added
            )
            added_message = f"<b>🆕 New DesignArena models available:</b>\n{lines}"

        removed_message = ""
        if removed:
            lines = "\n".join(
                f"• {self._format_model_name(model, identifier)}"
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
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
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
                await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning("Failed to send tag update to chat %s: %s", chat_id, exc)

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
