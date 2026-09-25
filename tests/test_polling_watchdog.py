from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.request import HTTPXRequest

from arena_watcher.telegram_bot import (
    POLLING_STALL_SECONDS,
    ArenaWatcherBot,
    PollingActivityRequest,
)


class PollingWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_records_finish_even_on_error(self) -> None:
        request = PollingActivityRequest(connection_pool_size=1)
        request.last_finished = 0.0
        with patch.object(HTTPXRequest, "do_request", AsyncMock(side_effect=OSError("boom"))):
            with self.assertRaises(OSError):
                await request.do_request("https://api.telegram.org/x", "POST")
        self.assertGreater(request.last_finished, 0.0)

    async def test_exits_only_when_polling_is_stalled(self) -> None:
        bot = object.__new__(ArenaWatcherBot)
        bot._polling_request = SimpleNamespace(last_finished=time.monotonic())
        with patch("arena_watcher.telegram_bot.os._exit") as exit_:
            await bot._check_polling(None)
            exit_.assert_not_called()

            bot._polling_request.last_finished = time.monotonic() - POLLING_STALL_SECONDS - 1
            await bot._check_polling(None)
            exit_.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
