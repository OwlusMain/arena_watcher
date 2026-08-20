from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from arena_watcher.arena_client import ArenaClient, ArenaFetchError


def html_response(body: str) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"content-type": "text/html; charset=utf-8"},
        status_code=200,
        text=body,
        url="https://arena.ai/",
    )


class ArenaClientTests(unittest.TestCase):
    def test_retries_parse_failure_with_fresh_session(self) -> None:
        client = ArenaClient("https://arena.ai/")
        stale_scraper = Mock()
        stale_scraper.get.return_value = html_response("<html>incomplete response</html>")
        fresh_scraper = Mock()
        fresh_scraper.get.return_value = html_response(
            r'prefix initialModels\":[{\"id\":\"model-1\",\"name\":\"Model One\"}] suffix'
        )
        client._scraper = stale_scraper

        with patch(
            "arena_watcher.arena_client.cloudscraper.create_scraper",
            return_value=fresh_scraper,
        ) as create_scraper:
            models = client.fetch_models()

        self.assertEqual([model.identifier for model in models], ["model-1"])
        self.assertEqual([model.name for model in models], ["Model One"])
        create_scraper.assert_called_once_with()
        self.assertIs(client._scraper, fresh_scraper)

    def test_fails_after_one_fresh_session_retry(self) -> None:
        client = ArenaClient("https://arena.ai/")
        stale_scraper = Mock()
        stale_scraper.get.return_value = html_response("<html>first incomplete response</html>")
        fresh_scraper = Mock()
        fresh_scraper.get.return_value = html_response("<html>second incomplete response</html>")
        client._scraper = stale_scraper

        with patch(
            "arena_watcher.arena_client.cloudscraper.create_scraper",
            return_value=fresh_scraper,
        ):
            with self.assertRaisesRegex(
                ArenaFetchError,
                "did not contain an initialModels array",
            ):
                client.fetch_models()

        stale_scraper.get.assert_called_once()
        fresh_scraper.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
