from __future__ import annotations

import html
import re
import unittest

from arena_watcher.telegram_messages import (
    split_html_message,
    split_text_message,
    telegram_text_length,
)


TAG_RE = re.compile(r"<[^>]+>")


def visible_text(value: str) -> str:
    return html.unescape(TAG_RE.sub("", value))


class TelegramMessageSplittingTests(unittest.TestCase):
    def test_plain_text_is_sent_in_order_across_multiple_chunks(self) -> None:
        text = ("model-name " * 700).rstrip()

        chunks = split_text_message(text)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(telegram_text_length(chunk) <= 4096 for chunk in chunks))

    def test_utf16_length_keeps_astral_emoji_below_telegram_limit(self) -> None:
        text = "😀" * 3000

        chunks = split_text_message(text)

        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(telegram_text_length(chunk) <= 4096 for chunk in chunks))

    def test_html_entities_and_formatting_survive_line_splits(self) -> None:
        header = "<b>🆕 New models:</b>\n"
        line = "• model &lt;preview&gt; <i>(tag &amp; note)</i>\n"
        message = header + line * 150

        chunks = split_html_message(message)

        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(visible_text(chunk) for chunk in chunks), visible_text(message))
        self.assertTrue(
            all(telegram_text_length(visible_text(chunk)) <= 4096 for chunk in chunks)
        )
        self.assertTrue(all(chunk.count("<b>") == chunk.count("</b>") for chunk in chunks))
        self.assertTrue(all(chunk.count("<i>") == chunk.count("</i>") for chunk in chunks))

    def test_long_formatted_value_is_closed_and_reopened_at_hard_split(self) -> None:
        message = "<b>Title</b>\n<i>" + "x" * 9000 + "</i>"

        chunks = split_html_message(message)

        self.assertEqual("".join(visible_text(chunk) for chunk in chunks), visible_text(message))
        self.assertTrue(all(chunk.count("<i>") == chunk.count("</i>") for chunk in chunks))
        self.assertTrue(
            all(telegram_text_length(visible_text(chunk)) <= 4096 for chunk in chunks)
        )


if __name__ == "__main__":
    unittest.main()
