"""Silently put Arena models back into the bot state.

The bot announces a model when it shows up in a poll but is missing from
``known_models``. Writing models straight into the state file therefore marks
them as already known, and nothing is posted when they appear again.

Sources can be local files or URLs:
  * JSON: a list of Arena model objects, a dict keyed by model id (the
    lmarena-tracker ``snapshot.json``), or a bot state file (``known_models``);
  * HTML: an arena.ai page with the ``initialModels`` bootstrap, e.g. a Web
    Archive copy ``https://web.archive.org/web/20260918000000id_/https://arena.ai/``.

Stop the bot before applying, otherwise it overwrites the file from memory.
Arena removal tracking must be off (ARENA_REMOVALS_ENABLED unset), or the next
poll announces every restored model that the HTML no longer lists as removed.

    python scripts/restore_arena_models.py --state data/state.json SOURCE...           # dry run
    python scripts/restore_arena_models.py --state data/state.json SOURCE... --apply --removals-paused
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arena_watcher.arena_client import ArenaClient, ModelEntry  # noqa: E402
from arena_watcher.state_store import StateStore, TrackedModel  # noqa: E402
from arena_watcher.telegram_bot import ArenaWatcherBot  # noqa: E402


def _read_source(source: str) -> str:
    if source.startswith(("http://", "https://")):
        import cloudscraper

        response = cloudscraper.create_scraper().get(source, timeout=60)
        response.raise_for_status()
        return response.text
    return Path(source).read_text()


def load_models(source: str) -> dict[str, TrackedModel]:
    text = _read_source(source)
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        payload = ArenaClient._parse_initial_models(ArenaClient.__new__(ArenaClient), text)

    if isinstance(payload, dict) and isinstance(payload.get("known_models"), dict):
        return {
            str(identifier): TrackedModel.from_json(model)
            for identifier, model in payload["known_models"].items()
        }
    raw_models = list(payload.values()) if isinstance(payload, dict) else payload
    if not isinstance(raw_models, list):
        raise SystemExit(f"{source}: expected a list or dict of models")

    models: dict[str, TrackedModel] = {}
    for raw in raw_models:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        identifier = str(raw["id"])
        entry = ModelEntry(identifier, ArenaClient._extract_name(raw, identifier), raw)
        input_caps, output_caps = ArenaWatcherBot._capability_lists(entry)
        models[identifier] = TrackedModel(
            name=entry.name,
            input_capabilities=input_caps,
            output_capabilities=output_caps,
        )
    return models


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="+", help="JSON/HTML files or URLs; earlier sources win on conflicts")
    parser.add_argument("--state", type=Path, required=True, help="bot state file (STATE_PATH)")
    parser.add_argument("--apply", action="store_true", help="write the state file (default: dry run)")
    parser.add_argument(
        "--removals-paused",
        action="store_true",
        help="confirm the deployed bot runs with Arena removal tracking off",
    )
    args = parser.parse_args()

    if args.apply and not args.removals_paused:
        parser.error("--apply requires --removals-paused (see the module docstring)")

    store = StateStore(args.state)
    state = store.load()
    if not state.known_models and not args.state.exists():
        parser.error(f"{args.state} does not exist")

    restored: dict[str, TrackedModel] = {}
    for source in args.sources:
        models = load_models(source)
        print(f"{source}: {len(models)} models")
        for identifier, model in models.items():
            if identifier not in state.known_models:
                restored.setdefault(identifier, model)

    print(f"known before: {len(state.known_models)}, to restore: {len(restored)}")
    for identifier, model in sorted(restored.items(), key=lambda item: item[1].name.lower())[:20]:
        print(f"  + {model.name} ({identifier})")
    if len(restored) > 20:
        print(f"  … and {len(restored) - 20} more")

    if not args.apply:
        print("dry run: nothing written (pass --apply --removals-paused to write)")
        return 0
    if not restored:
        return 0

    backup = args.state.with_name(f"{args.state.name}.backup-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    shutil.copy2(args.state, backup)
    state.known_models.update(restored)
    state.removal_waitlist.pop("arena", None)
    store.save(state)
    print(f"written {args.state} ({len(state.known_models)} models), backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
