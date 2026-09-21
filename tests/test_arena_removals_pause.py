from __future__ import annotations

import unittest
from types import SimpleNamespace

from arena_watcher.state_store import TrackedModel, WatcherState
from arena_watcher.telegram_bot import ArenaWatcherBot


def _model(name: str) -> TrackedModel:
    return TrackedModel(name=name, input_capabilities=["text"], output_capabilities=["text"])


class ArenaRemovalsPauseTests(unittest.TestCase):
    def _bot(self, waitlist: dict[str, dict[str, float]] | None = None) -> ArenaWatcherBot:
        bot = object.__new__(ArenaWatcherBot)
        bot._config = SimpleNamespace(arena_removals_enabled=False, removal_waitlist_seconds=0)
        bot._state = WatcherState(removal_waitlist=waitlist or {})
        return bot

    def test_missing_models_are_kept_and_additions_reported(self) -> None:
        bot = self._bot()
        previous = {"a": _model("a"), "b": _model("b")}
        api = {"a": _model("a-renamed"), "c": _model("c")}

        snapshots, added, removed, updated = bot._keep_missing_models("arena", previous, api)

        self.assertEqual(set(snapshots), {"a", "b", "c"})
        self.assertEqual(snapshots["a"].name, "a-renamed")
        self.assertEqual(added, {"c"})
        self.assertEqual(removed, set())
        self.assertFalse(updated)

    def test_pending_arena_waitlist_is_dropped(self) -> None:
        bot = self._bot({"arena": {"b": 1.0}, "google": {"x": 1.0}})

        _, _, removed, updated = bot._keep_missing_models("arena", {"b": _model("b")}, {})

        self.assertEqual(removed, set())
        self.assertTrue(updated)
        self.assertEqual(set(bot._state.removal_waitlist), {"google"})


if __name__ == "__main__":
    unittest.main()
