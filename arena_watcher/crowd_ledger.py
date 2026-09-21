"""Decides when crowd-reported Arena models are trustworthy enough to announce.

A model reported by extension users is announced when one of these holds:
* the report comes from a trusted install (set by an admin);
* ``quorum`` distinct installs from distinct networks reported the same id
  with the same public name;
* an admin approved it from Telegram.

Everything else waits in ``pending`` and expires after ``PENDING_TTL_SECONDS``.
The ledger works on the plain dict persisted in the bot state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

PENDING_TTL_SECONDS = 7 * 24 * 3600
MAX_PENDING_PER_INSTALL = 10
MAX_PENDING_TOTAL = 500


@dataclass(slots=True)
class SightingOutcome:
    promoted: list[dict[str, Any]] = field(default_factory=list)
    newly_pending: list[dict[str, Any]] = field(default_factory=list)
    known: int = 0
    ignored: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "published": len(self.promoted),
            "pending": len(self.newly_pending),
            "known": self.known,
            "ignored": self.ignored,
        }


class CrowdLedger:
    def __init__(self, data: dict[str, Any], quorum: int, trusted: Iterable[str] = ()) -> None:
        self.data = data
        self.quorum = max(1, quorum)
        for key in ("pending", "rejected", "published"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        for key in ("banned", "trusted"):
            if not isinstance(data.get(key), list):
                data[key] = []
        for install_id in trusted:
            if install_id not in data["trusted"]:
                data["trusted"].append(install_id)

    @property
    def pending(self) -> dict[str, dict[str, Any]]:
        return self.data["pending"]

    def is_banned(self, install_id: str) -> bool:
        return install_id in self.data["banned"]

    def is_trusted(self, install_id: str) -> bool:
        return install_id in self.data["trusted"]

    def set_flag(self, list_name: str, install_id: str, enabled: bool) -> None:
        members: list[str] = self.data[list_name]
        if enabled and install_id not in members:
            members.append(install_id)
        elif not enabled and install_id in members:
            members.remove(install_id)

    def record(
        self,
        install_id: str,
        network: str,
        models: list[dict[str, Any]],
        known_ids: set[str],
        now: float,
    ) -> SightingOutcome:
        outcome = SightingOutcome()
        if self.is_banned(install_id):
            outcome.ignored = len(models)
            return outcome

        for model in models:
            identifier = model["id"]
            if identifier in known_ids:
                outcome.known += 1
                self.pending.pop(identifier, None)
                continue
            if identifier in self.data["rejected"]:
                outcome.ignored += 1
                continue

            entry = self.pending.get(identifier)
            if entry is None:
                if (
                    len(self.pending) >= MAX_PENDING_TOTAL
                    or self._pending_opened_by(install_id) >= MAX_PENDING_PER_INSTALL
                ):
                    outcome.ignored += 1
                    continue
                entry = {"model": model, "first_seen": now, "opened_by": install_id, "reports": {}}
                self.pending[identifier] = entry
                is_new = True
            else:
                is_new = False

            if model["publicName"] == entry["model"]["publicName"]:
                entry["reports"][install_id] = {"network": network, "ts": now}

            if self.is_trusted(install_id):
                entry["model"] = model
                outcome.promoted.append(self._publish(identifier, now, "trusted"))
            elif self._confirmations(entry) >= self.quorum:
                outcome.promoted.append(self._publish(identifier, now, "quorum"))
            elif is_new:
                outcome.newly_pending.append(entry["model"])
        return outcome

    def approve(self, identifier: str, now: float) -> Optional[dict[str, Any]]:
        if identifier not in self.pending:
            return None
        return self._publish(identifier, now, "admin")

    def reject(self, identifier: str, now: float) -> Optional[dict[str, Any]]:
        entry = self.pending.pop(identifier, None)
        self.data["rejected"][identifier] = now
        return entry["model"] if entry else None

    def reporters(self, identifier: str) -> list[str]:
        entry = self.pending.get(identifier)
        return list(entry["reports"]) if entry else []

    def prune(self, now: float, known_ids: set[str]) -> bool:
        stale = [
            identifier
            for identifier, entry in self.pending.items()
            if identifier in known_ids or now - float(entry.get("first_seen", 0)) > PENDING_TTL_SECONDS
        ]
        for identifier in stale:
            self.pending.pop(identifier, None)
        return bool(stale)

    def _publish(self, identifier: str, now: float, reason: str) -> dict[str, Any]:
        entry = self.pending.pop(identifier)
        self.data["published"][identifier] = {
            "ts": now,
            "reason": reason,
            "reporters": sorted(entry["reports"]),
        }
        return entry["model"]

    def _pending_opened_by(self, install_id: str) -> int:
        return sum(1 for entry in self.pending.values() if entry.get("opened_by") == install_id)

    @staticmethod
    def _confirmations(entry: dict[str, Any]) -> int:
        networks = {report["network"] for report in entry["reports"].values()}
        return min(len(entry["reports"]), len(networks))
