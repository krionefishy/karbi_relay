import asyncio
import contextlib
import json

import pytest

from relay.core.storage import Storage
from relay.telegram.client import NormalizedUpdate, Sender, TelegramClient
from relay.telegram.poller import BotPoller, PollerState

UPDATE = {
    "update_id": 500,
    "message": {
        "chat": {"id": -100777},
        "text": "/start abc",
        "from": {"id": 42, "username": "ivan", "first_name": "Иван"},
    },
}


class FakeMessenger:
    """Hands out one batch, then blocks so the poller's loop parks."""

    def __init__(self, batches: list[list[NormalizedUpdate]]) -> None:
        self.batches = batches
        self.offsets: list[int] = []

    async def updates(self, token: str, offset: int) -> list[NormalizedUpdate]:
        self.offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(3600)
        return []


def make_update(event_id: int = 500) -> NormalizedUpdate:
    return NormalizedUpdate(
        event_id=event_id,
        recipient="-100777",
        text="/start abc",
        sender=Sender(external_id="42", username="ivan", display_name="Иван"),
    )


@pytest.fixture
async def storage(tmp_path) -> Storage:
    store = Storage(str(tmp_path / "relay.sqlite3"))
    await store.connect()
    await store.upsert_bot(
        bot_code="turnover-alerts",
        encrypted_token="unused-here",
        token_fingerprint="fp",
        title="T",
        invite_link_template="https://t.me/bot?start={token}",
    )
    yield store
    await store.close()


async def run_briefly(poller: BotPoller) -> None:
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


class TestNormalization:
    def test_a_text_message_becomes_a_flat_update(self) -> None:
        update = TelegramClient._normalize(UPDATE)
        assert update is not None
        assert update.event_id == 500
        # The chat id leaves as an opaque string, never as a number.
        assert update.recipient == "-100777"
        assert isinstance(update.recipient, str)
        assert update.sender.external_id == "42"

    @pytest.mark.parametrize(
        "raw",
        [
            {"update_id": 1},
            {"update_id": 1, "message": {"chat": {}}},
            {"message": {"chat": {"id": 5}}},
            {"update_id": 1, "edited_message": {"chat": {"id": 5}}},
            "not a dict",
        ],
    )
    def test_anything_else_is_dropped(self, raw) -> None:
        assert TelegramClient._normalize(raw) is None

    def test_the_payload_carries_no_telegram_shapes(self) -> None:
        payload = make_update().as_payload("turnover-alerts")
        assert set(payload) == {"bot_code", "event_id", "recipient", "text", "sender"}
        assert "chat" not in payload and "message" not in payload


def poller(storage, messenger) -> BotPoller:
    return BotPoller(
        storage=storage,
        client=messenger,
        bot_code="turnover-alerts",
        token="t",
        state=PollerState(),
        retry_seconds=0.01,
    )


class TestPolling:
    async def test_an_update_is_buffered_and_the_offset_moves(self, storage) -> None:
        await run_briefly(poller(storage, FakeMessenger([[make_update(500)]])))

        buffered = await storage.updates_after("turnover-alerts", 0, 10)
        assert len(buffered) == 1
        assert json.loads(buffered[0])["event_id"] == 500
        # Durability is local now: the messenger is acknowledged straight away,
        # because nothing downstream has to accept the update first.
        bot = await storage.bot("turnover-alerts")
        assert bot is not None and bot.last_update_id == 500

    async def test_the_same_update_is_buffered_once(self, storage) -> None:
        await run_briefly(poller(storage, FakeMessenger([[make_update(500)], [make_update(500)]])))

        assert len(await storage.updates_after("turnover-alerts", 0, 10)) == 1

    async def test_the_main_server_cursor_decides_what_it_gets(self, storage) -> None:
        await run_briefly(poller(storage, FakeMessenger([[make_update(500), make_update(501)]])))

        # Whatever the main server already handled it never sees again; there is
        # no acknowledgement to lose, only its own cursor.
        assert len(await storage.updates_after("turnover-alerts", 0, 10)) == 2
        assert len(await storage.updates_after("turnover-alerts", 500, 10)) == 1
        assert await storage.updates_after("turnover-alerts", 501, 10) == []

    async def test_polling_resumes_after_the_stored_cursor(self, storage) -> None:
        await storage.save_cursor("turnover-alerts", 900)
        messenger = FakeMessenger([])
        await run_briefly(poller(storage, messenger))
        assert messenger.offsets[0] == 901

    async def test_old_updates_are_pruned(self, storage) -> None:
        await storage.store_update("turnover-alerts", 1, "{}")
        assert await storage.count_inbound_updates() == 1
        # Nothing is old enough yet, so retention must not touch it.
        assert await storage.prune_inbound_updates(7) == 0
        assert await storage.prune_inbound_updates(0) == 1
