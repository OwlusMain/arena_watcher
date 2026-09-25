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


def json_response(payload: object) -> SimpleNamespace:
    return SimpleNamespace(
        headers={"content-type": "application/json"},
        status_code=200,
        json=lambda: payload,
        url="https://arena.ai/nextjs-api/model-catalog",
    )


class ArenaClientTests(unittest.TestCase):
    def test_model_catalog_sections_are_merged(self) -> None:
        client = ArenaClient("https://arena.ai/nextjs-api/model-catalog")
        shared = {"id": "model-1", "displayName": "Model One", "rank": 3}
        client._scraper = Mock()
        client._scraper.get.return_value = json_response(
            [
                {"arena": "text", "models": [shared, {"id": "model-2", "publicName": "Two"}], "complete": True},
                {"arena": "code", "models": [dict(shared, rank=9)], "complete": True},
            ]
        )

        models = client.fetch_models()

        self.assertEqual([(m.identifier, m.name) for m in models], [("model-1", "Model One"), ("model-2", "Two")])

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
