"""Long polling: one asyncio task per bot, supervised by the bot table.

One token, one poller — the messenger answers 409 to a second getUpdates on the
same token and both pollers stop receiving anything. Different bots poll in
parallel without coordination, since the limit is per token.

Updates land in the local buffer and the offset moves straight away: durability
is ours now, not the main server's. The main server comes and takes them when it
is ready, because from abroad it cannot be reached at all — no port, no ICMP.
"""

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from relay.crypto import CredentialCipher, CredentialDecryptionError
from relay.storage import BotRecord, Storage
from relay.telegram import (
    MessengerAuthError,
    MessengerConflictError,
    MessengerPermanentError,
    MessengerRateLimited,
    MessengerTemporaryError,
    TelegramClient,
)


@dataclass
class PollerState:
    """What /ready reports about one bot."""

    last_successful_poll_at: datetime | None = None
    last_buffered_event_id: int | None = None
    consecutive_errors: int = 0
    last_error: str = ""
    parked: bool = False


@dataclass
class _Running:
    task: asyncio.Task[None]
    encrypted_token: str
    state: PollerState = field(default_factory=PollerState)


class BotPoller:
    def __init__(
        self,
        *,
        storage: Storage,
        client: TelegramClient,
        bot_code: str,
        token: str,
        state: PollerState,
        retry_seconds: float,
    ) -> None:
        self._storage = storage
        self._client = client
        self._bot_code = bot_code
        self._token = token
        self._state = state
        self._retry_seconds = retry_seconds
        self._logger = logging.getLogger("relay.poller")

    async def run(self) -> None:
        record = await self._storage.bot(self._bot_code)
        offset = (record.last_update_id + 1) if record and record.last_update_id else 0
        self._logger.info("poller_started bot=%s offset=%s", self._bot_code, offset)

        while True:
            try:
                updates = await self._client.updates(self._token, offset)
            except MessengerAuthError as error:
                # The token is dead. Retrying forever would just log forever.
                self._logger.error("poller_token_rejected bot=%s error=%s", self._bot_code, error)
                self._state.parked = True
                self._state.last_error = str(error)
                await self._storage.deactivate_bot(self._bot_code, str(error))
                return
            except MessengerRateLimited as error:
                await self._back_off(error.retry_after, "rate limited")
                continue
            except MessengerConflictError as error:
                # Someone else holds this token: back off instead of fighting.
                await self._back_off(self._retry_seconds, f"conflict: {error}")
                continue
            except (MessengerTemporaryError, MessengerPermanentError) as error:
                await self._back_off(self._retry_seconds, str(error))
                continue

            self._state.last_successful_poll_at = datetime.now(UTC)
            self._state.consecutive_errors = 0
            self._state.last_error = ""

            for update in updates:
                # Stored first, acknowledged second: a crash between the two
                # replays the update, and the buffer's primary key absorbs it.
                await self._storage.store_update(
                    self._bot_code, update.event_id, json.dumps(update.as_payload(self._bot_code), ensure_ascii=False)
                )
                offset = update.event_id + 1
                await self._storage.save_cursor(self._bot_code, update.event_id)
                self._state.last_buffered_event_id = update.event_id

    async def _back_off(self, seconds: float, reason: str) -> None:
        self._state.consecutive_errors += 1
        self._state.last_error = reason
        self._logger.warning(
            "poller_retry bot=%s errors=%s reason=%s",
            self._bot_code,
            self._state.consecutive_errors,
            reason,
        )
        await asyncio.sleep(seconds)


class BotSupervisor:
    """Follow the bot table: a bot registered today starts polling without a deploy."""

    def __init__(
        self,
        *,
        storage: Storage,
        client: TelegramClient,
        cipher: CredentialCipher,
        refresh_seconds: int,
        dedupe_ttl_hours: int,
        inbound_retention_days: int,
        retry_seconds: float,
    ) -> None:
        self._storage = storage
        self._client = client
        self._cipher = cipher
        self._refresh_seconds = refresh_seconds
        self._dedupe_ttl_hours = dedupe_ttl_hours
        self._inbound_retention_days = inbound_retention_days
        self._retry_seconds = retry_seconds
        self._running: dict[str, _Running] = {}
        self._stop = asyncio.Event()
        self._logger = logging.getLogger("relay.supervisor")

    @property
    def states(self) -> dict[str, PollerState]:
        return {code: running.state for code, running in self._running.items()}

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._sync()
                await self._storage.prune_sent_keys(self._dedupe_ttl_hours)
                await self._storage.prune_inbound_updates(self._inbound_retention_days)
            except Exception:
                self._logger.exception("supervisor_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._refresh_seconds)
        await self.shutdown()

    async def shutdown(self) -> None:
        for running in self._running.values():
            running.task.cancel()
        tasks = [running.task for running in self._running.values()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    async def _sync(self) -> None:
        bots = {bot.bot_code: bot for bot in await self._storage.active_bots()}

        for code in list(self._running):
            running = self._running[code]
            bot = bots.get(code)
            if bot is None or bot.encrypted_token != running.encrypted_token:
                # Gone, deactivated, or the token was rotated under us.
                running.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await running.task
                del self._running[code]

        for code, bot in bots.items():
            existing = self._running.get(code)
            if existing is not None and not existing.task.done():
                continue
            if existing is not None:
                self._logger.warning("poller_died bot=%s, restarting", code)
                del self._running[code]
            self._start(bot)

    def _start(self, bot: BotRecord) -> None:
        try:
            token = self._cipher.decrypt(bot.encrypted_token)
        except CredentialDecryptionError:
            # Wrong key on this machine: parking beats a crash loop.
            self._logger.error("poller_token_undecryptable bot=%s", bot.bot_code)
            return
        state = PollerState()
        poller = BotPoller(
            storage=self._storage,
            client=self._client,
            bot_code=bot.bot_code,
            token=token,
            state=state,
            retry_seconds=self._retry_seconds,
        )
        task = asyncio.create_task(poller.run(), name=f"poller:{bot.bot_code}")
        self._running[bot.bot_code] = _Running(task=task, encrypted_token=bot.encrypted_token, state=state)
