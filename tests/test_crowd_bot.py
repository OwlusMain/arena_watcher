from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from arena_watcher.crowd_api import RateLimiter
from arena_watcher.crowd_ledger import CrowdLedger
from arena_watcher.state_store import TrackedModel, WatcherState
from arena_watcher.telegram_bot import ArenaWatcherBot

NEW_ID = "019e080d-c29d-7d9a-aa54-000000000001"


def _bot(trusted: list[str]) -> ArenaWatcherBot:
    bot = object.__new__(ArenaWatcherBot)
    bot._state = WatcherState(known_models={"old": TrackedModel(name="old")}, chats={1})
    bot._state_lock = asyncio.Lock()
    bot._store = MagicMock()
    bot._crowd_ledger = CrowdLedger(bot._state.crowd, quorum=2, trusted=trusted)
    bot._background_tasks = set()
    bot._admin_user_ids = {42}
    bot._crowd_review_limiter = RateLimiter(capacity=20, per_seconds=3600)
    bot._arena_direct_client = None
    bot._app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    return bot


class CrowdBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_trusted_sighting_is_stored_and_announced(self) -> None:
        bot = _bot(trusted=["me"])
        model = {
            "id": NEW_ID,
            "publicName": "hidden-model",
            "capabilities": {"inputCapabilities": {"text": True}, "outputCapabilities": {"text": True}},
            "userSelectable": False,
        }

        result = await bot._handle_crowd_sightings("me", "net", [model])
        await asyncio.gather(*bot._background_tasks)

        self.assertEqual(result["published"], 1)
        stored = bot._state.known_models[NEW_ID]
        self.assertEqual((stored.name, stored.user_selectable), ("hidden-model", False))
        text = bot._app.bot.send_message.await_args.kwargs["text"]
        self.assertIn("New models on Arena:", text)
        self.assertIn("hidden-model", text)
        self.assertEqual(bot._crowd_models_payload()[0]["id"], NEW_ID)

    async def test_untrusted_sighting_goes_to_admin_review(self) -> None:
        bot = _bot(trusted=[])

        result = await bot._handle_crowd_sightings("anon", "net", [{"id": NEW_ID, "publicName": "<x>"}])
        await asyncio.gather(*bot._background_tasks)

        self.assertEqual(result["pending"], 1)
        self.assertNotIn(NEW_ID, bot._state.known_models)
        call = bot._app.bot.send_message.await_args.kwargs
        self.assertEqual(call["chat_id"], 42)
        self.assertIn("&lt;x&gt;", call["text"])
        self.assertIsNotNone(call["reply_markup"])


class CrowdCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_too_old_press_still_publishes_and_announces(self) -> None:
        from telegram.error import BadRequest

        bot = _bot(trusted=[])
        await bot._handle_crowd_sightings("anon", "net", [{"id": NEW_ID, "publicName": "late-model"}])
        await asyncio.gather(*bot._background_tasks)
        bot._app.bot.send_message.reset_mock()
        bot._is_admin = lambda user_id: True
        query = SimpleNamespace(
            data=f"crowd:approve:{NEW_ID}",
            answer=AsyncMock(side_effect=BadRequest("Query is too old")),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(text_html="review"),
        )
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42, full_name="Admin"))
        context = SimpleNamespace(bot=bot._app.bot)

        await bot._handle_crowd_callback(update, context)

        self.assertIn(NEW_ID, bot._state.known_models)
        query.edit_message_text.assert_awaited_once()
        texts = [call.kwargs["text"] for call in bot._app.bot.send_message.await_args_list]
        self.assertTrue(any("late-model" in text and "New models on Arena" in text for text in texts))


if __name__ == "__main__":
    unittest.main()
