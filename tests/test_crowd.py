from __future__ import annotations

import hashlib
import itertools
import unittest

from aiohttp.test_utils import TestClient, TestServer

from arena_watcher.crowd_api import (
    CrowdApi,
    client_network,
    leading_zero_bits,
    sanitize_model,
)
from arena_watcher.crowd_ledger import MAX_PENDING_PER_INSTALL, CrowdLedger

MODEL_ID = "019e080d-c29d-7d9a-aa54-faed41da0763"


def _model(identifier: str = MODEL_ID, name: str = "secret-model") -> dict:
    return {"id": identifier, "publicName": name}


def _uuid(index: int) -> str:
    return f"019e080d-c29d-7d9a-aa54-{index:012x}"


class CrowdLedgerTests(unittest.TestCase):
    def test_quorum_needs_distinct_installs_and_networks(self) -> None:
        ledger = CrowdLedger({}, quorum=2)

        first = ledger.record("a", "1.2.3.0/24", [_model()], set(), now=0)
        same_net = ledger.record("b", "1.2.3.0/24", [_model()], set(), now=1)
        other_net = ledger.record("c", "5.6.7.0/24", [_model()], set(), now=2)

        self.assertEqual(len(first.newly_pending), 1)
        self.assertEqual(same_net.promoted, [])
        self.assertEqual([model["id"] for model in other_net.promoted], [MODEL_ID])
        self.assertNotIn(MODEL_ID, ledger.pending)

    def test_mismatched_name_does_not_count_towards_quorum(self) -> None:
        ledger = CrowdLedger({}, quorum=2)
        ledger.record("a", "net-a", [_model()], set(), now=0)

        outcome = ledger.record("b", "net-b", [_model(name="spoofed")], set(), now=1)

        self.assertEqual(outcome.promoted, [])
        self.assertEqual(ledger.reporters(MODEL_ID), ["a"])

    def test_trusted_install_publishes_immediately(self) -> None:
        ledger = CrowdLedger({}, quorum=3, trusted=["me"])

        outcome = ledger.record("me", "net", [_model()], set(), now=0)

        self.assertEqual(len(outcome.promoted), 1)
        self.assertEqual(ledger.data["published"][MODEL_ID]["reason"], "trusted")

    def test_known_rejected_and_banned_are_ignored(self) -> None:
        ledger = CrowdLedger({"banned": ["bad"]}, quorum=1)
        ledger.reject(_uuid(1), now=0)

        known = ledger.record("a", "n", [_model()], {MODEL_ID}, now=0)
        rejected = ledger.record("a", "n", [_model(_uuid(1))], set(), now=0)
        banned = ledger.record("bad", "n", [_model(_uuid(2))], set(), now=0)

        self.assertEqual((known.known, rejected.ignored, banned.ignored), (1, 1, 1))
        self.assertEqual(ledger.pending, {})

    def test_single_install_cannot_flood_pending(self) -> None:
        ledger = CrowdLedger({}, quorum=2)
        models = [_model(_uuid(i), f"m{i}") for i in range(MAX_PENDING_PER_INSTALL + 5)]

        outcome = ledger.record("a", "n", models, set(), now=0)

        self.assertEqual(len(outcome.newly_pending), MAX_PENDING_PER_INSTALL)
        self.assertEqual(outcome.ignored, 5)

    def test_admin_approve_and_prune(self) -> None:
        ledger = CrowdLedger({}, quorum=2)
        ledger.record("a", "n", [_model(), _model(_uuid(1), "other")], set(), now=0)

        self.assertEqual(ledger.approve(MODEL_ID, now=1)["publicName"], "secret-model")
        self.assertIsNone(ledger.approve(MODEL_ID, now=2))
        self.assertTrue(ledger.prune(now=10**9, known_ids=set()))
        self.assertEqual(ledger.pending, {})


class SanitizeTests(unittest.TestCase):
    def test_keeps_only_known_fields(self) -> None:
        raw = {
            "id": MODEL_ID,
            "publicName": "  new   model ",
            "organization": "x" * 500,
            "capabilities": {
                "inputCapabilities": {"text": True, "image": {"multipleImages": True}, "<b>": True},
                "outputCapabilities": {"text": True, "video": False},
            },
            "userSelectable": False,
            "prompt": "should not be stored",
        }

        model = sanitize_model(raw)

        self.assertEqual(
            model,
            {
                "id": MODEL_ID,
                "publicName": "new model",
                "capabilities": {
                    "inputCapabilities": {"text": True, "image": True},
                    "outputCapabilities": {"text": True},
                },
                "userSelectable": False,
            },
        )

    def test_rejects_bad_ids_and_names(self) -> None:
        self.assertIsNone(sanitize_model({"id": "not-a-uuid", "publicName": "x"}))
        self.assertIsNone(sanitize_model({"id": MODEL_ID.upper(), "publicName": "x"}))
        self.assertIsNone(sanitize_model({"id": MODEL_ID, "publicName": ""}))

    def test_client_network(self) -> None:
        self.assertEqual(client_network("203.0.113.77"), "203.0.113.0/24")
        self.assertEqual(client_network("2001:db8:1:2::1"), "2001:db8:1::/48")


def _solve(challenge: str, bits: int) -> str:
    for counter in itertools.count():
        solution = format(counter, "x")
        digest = hashlib.sha256(f"{challenge}:{solution}".encode()).digest()
        if leading_zero_bits(digest) >= bits:
            return solution
    raise AssertionError("unreachable")


class CrowdApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.sightings: list[tuple[str, str, list[dict]]] = []
        self.banned: set[str] = set()

        async def handler(install_id: str, network: str, models: list[dict]) -> dict:
            self.sightings.append((install_id, network, models))
            return {"published": 0, "pending": len(models), "known": 0, "ignored": 0}

        api = CrowdApi(
            secret="s" * 32,
            pow_bits=8,
            models_provider=lambda: [{"id": MODEL_ID, "name": "known"}],
            sightings_handler=handler,
            is_banned=self.banned.__contains__,
        )
        self.client = TestClient(TestServer(api.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    async def _register(self) -> tuple[str, str]:
        challenge = await (await self.client.get("/v1/challenge")).json()
        solution = _solve(challenge["challenge"], challenge["bits"])
        response = await self.client.post(
            "/v1/register", json={"challenge": challenge["challenge"], "solution": solution}
        )
        self.assertEqual(response.status, 200)
        body = await response.json()
        return body["install"], body["token"]

    async def test_register_models_and_sightings_flow(self) -> None:
        install_id, token = await self._register()
        auth = {"Authorization": f"Bearer {token}"}

        models = await self.client.get("/v1/models", headers=auth)
        self.assertEqual((await models.json())["models"][0]["id"], MODEL_ID)
        cached = await self.client.get(
            "/v1/models", headers={**auth, "If-None-Match": models.headers["ETag"]}
        )
        self.assertEqual(cached.status, 304)

        response = await self.client.post(
            "/v1/sightings", headers=auth, json={"models": [_model(_uuid(5)), {"id": "junk"}]}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.sightings), 1)
        self.assertEqual(self.sightings[0][0], install_id)
        self.assertEqual([model["id"] for model in self.sightings[0][2]], [_uuid(5)])

    async def test_rejects_forged_tokens_reused_challenges_and_bans(self) -> None:
        forged = await self.client.get("/v1/models", headers={"Authorization": "Bearer abc.def"})
        self.assertEqual(forged.status, 401)

        challenge = (await (await self.client.get("/v1/challenge")).json())["challenge"]
        solution = _solve(challenge, 8)
        first = await self.client.post("/v1/register", json={"challenge": challenge, "solution": solution})
        replay = await self.client.post("/v1/register", json={"challenge": challenge, "solution": solution})
        self.assertEqual((first.status, replay.status), (200, 403))

        install_id = (await first.json())["install"]
        self.banned.add(install_id)
        token = (await first.json())["token"]
        banned = await self.client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(banned.status, 401)

    async def test_register_rate_limit_and_body_limits(self) -> None:
        statuses = []
        for _ in range(4):
            response = await self.client.post("/v1/register", json={"challenge": "x", "solution": "y"})
            statuses.append(response.status)
        self.assertEqual(statuses, [403, 403, 403, 429])

        _, token = await self._register_from_other_ip()
        auth = {"Authorization": f"Bearer {token}"}
        too_many = await self.client.post(
            "/v1/sightings", headers=auth, json={"models": [_model(_uuid(i)) for i in range(9)]}
        )
        huge = await self.client.post(
            "/v1/sightings",
            headers={**auth, "Content-Type": "application/json"},
            data=b'{"models":[' + b" " * 40_000 + b"]}",
        )
        self.assertEqual((too_many.status, huge.status), (400, 413))

    async def _register_from_other_ip(self) -> tuple[str, str]:
        challenge = await (await self.client.get("/v1/challenge")).json()
        solution = _solve(challenge["challenge"], challenge["bits"])
        response = await self.client.post(
            "/v1/register",
            json={"challenge": challenge["challenge"], "solution": solution},
            headers={"X-Forwarded-For": "198.51.100.7"},
        )
        body = await response.json()
        return body["install"], body["token"]


if __name__ == "__main__":
    unittest.main()
