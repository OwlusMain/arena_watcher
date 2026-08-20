from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from arena_watcher.telegram_bot import ArenaWatcherBot
from arena_watcher.telegram_messages import telegram_text_length


class TelegramBotSendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_html_is_awaited_as_sequential_messages(self) -> None:
        bot = object.__new__(ArenaWatcherBot)
        send_message = AsyncMock()
        context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
        message = "<b>Models:</b>\n" + "• model <i>(tag)</i>\n" * 400

        await bot._send_message(context, chat_id=123, text=message, parse_mode="HTML")

        self.assertGreater(send_message.await_count, 1)
        sent_chunks = [call.kwargs["text"] for call in send_message.await_args_list]
        visible_chunks = [
            chunk.replace("<b>", "")
            .replace("</b>", "")
            .replace("<i>", "")
            .replace("</i>", "")
            for chunk in sent_chunks
        ]
        self.assertTrue(all(telegram_text_length(chunk) <= 4096 for chunk in visible_chunks))
        self.assertTrue(all(call.kwargs["chat_id"] == 123 for call in send_message.await_args_list))
        self.assertTrue(
            all(call.kwargs["parse_mode"] == "HTML" for call in send_message.await_args_list)
        )


if __name__ == "__main__":
    unittest.main()
