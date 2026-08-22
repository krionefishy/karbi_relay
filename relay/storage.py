"""SQLite storage: a handful of bots and a short-lived dedupe table.

WAL mode because the poller writes cursors while the API writes bots; both are
tiny and rare, so one file and one writer lock are plenty.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
    bot_code             TEXT PRIMARY KEY,
    encrypted_token      TEXT NOT NULL,
    token_fingerprint    TEXT NOT NULL UNIQUE,
    title                TEXT NOT NULL DEFAULT '',
    invite_link_template TEXT NOT NULL DEFAULT '',
    last_update_id       INTEGER NOT NULL DEFAULT 0,
    is_active            INTEGER NOT NULL DEFAULT 1,
    last_error           TEXT NOT NULL DEFAULT '',
    updated_at           TEXT NOT NULL
);

-- Апдейты копятся здесь, пока основной сервер за ними не придёт. Он и только
-- он инициирует соединение: из-за границы наш сервер недоступен ни по одному
-- порту, проверено.
CREATE TABLE IF NOT EXISTS inbound_updates (
    bot_code   TEXT    NOT NULL,
    event_id   INTEGER NOT NULL,
    payload    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    PRIMARY KEY (bot_code, event_id)
);

CREATE INDEX IF NOT EXISTS ix_inbound_updates_created_at ON inbound_updates (created_at);

CREATE TABLE IF NOT EXISTS sent_keys (
    idempotency_key TEXT PRIMARY KEY,
    message_ref     TEXT,
    sent_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sent_keys_sent_at ON sent_keys (sent_at);
"""


@dataclass(frozen=True)
class BotRecord:
    bot_code: str
    encrypted_token: str
    title: str
    invite_link_template: str
    last_update_id: int
    is_active: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._connection = await aiosqlite.connect(self._path)
        self._connection.row_factory = aiosqlite.Row
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.executescript(SCHEMA)
        await self._connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Storage.connect() has not been called")
        return self._connection

    async def upsert_bot(
        self,
        *,
        bot_code: str,
        encrypted_token: str,
        token_fingerprint: str,
        title: str,
        invite_link_template: str,
    ) -> None:
        # ON CONFLICT on the code keeps the cursor: re-registering a rotated
        # token must not replay the updates the bot already handled.
        await self._db.execute(
            """
            INSERT INTO bots (bot_code, encrypted_token, token_fingerprint, title,
                              invite_link_template, is_active, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, '', ?)
            ON CONFLICT (bot_code) DO UPDATE SET
                encrypted_token = excluded.encrypted_token,
                token_fingerprint = excluded.token_fingerprint,
                title = excluded.title,
                invite_link_template = excluded.invite_link_template,
                is_active = 1,
                last_error = '',
                updated_at = excluded.updated_at
            """,
            (bot_code, encrypted_token, token_fingerprint, title, invite_link_template, _now()),
        )
        await self._db.commit()

    async def delete_bot(self, bot_code: str) -> bool:
        cursor = await self._db.execute("DELETE FROM bots WHERE bot_code = ?", (bot_code,))
        await self._db.commit()
        return cursor.rowcount > 0

    async def bot(self, bot_code: str) -> BotRecord | None:
        cursor = await self._db.execute(
            """
            SELECT bot_code, encrypted_token, title, invite_link_template, last_update_id, is_active
            FROM bots WHERE bot_code = ?
            """,
            (bot_code,),
        )
        row = await cursor.fetchone()
        return self._record(row) if row else None

    async def owner_of_fingerprint(self, token_fingerprint: str) -> str | None:
        cursor = await self._db.execute(
            "SELECT bot_code FROM bots WHERE token_fingerprint = ?",
            (token_fingerprint,),
        )
        row = await cursor.fetchone()
        return str(row["bot_code"]) if row else None

    async def active_bots(self) -> list[BotRecord]:
        cursor = await self._db.execute(
            """
            SELECT bot_code, encrypted_token, title, invite_link_template, last_update_id, is_active
            FROM bots WHERE is_active = 1 ORDER BY bot_code
            """
        )
        return [self._record(row) for row in await cursor.fetchall()]

    async def save_cursor(self, bot_code: str, last_update_id: int) -> None:
        await self._db.execute(
            "UPDATE bots SET last_update_id = ?, updated_at = ? WHERE bot_code = ?",
            (last_update_id, _now(), bot_code),
        )
        await self._db.commit()

    async def deactivate_bot(self, bot_code: str, reason: str) -> None:
        """Park a bot whose token the messenger rejected, keeping its cursor."""
        await self._db.execute(
            "UPDATE bots SET is_active = 0, last_error = ?, updated_at = ? WHERE bot_code = ?",
            (reason[:500], _now(), bot_code),
        )
        await self._db.commit()

    async def store_update(self, bot_code: str, event_id: int, payload: str) -> None:
        """Keep one update until the main server asks for it.

        INSERT OR IGNORE: the same update may arrive twice if the messenger
        replays it, and the pair (bot, event) is what makes it the same one.
        """
        await self._db.execute(
            "INSERT OR IGNORE INTO inbound_updates (bot_code, event_id, payload, created_at) VALUES (?, ?, ?, ?)",
            (bot_code, event_id, payload, _now()),
        )
        await self._db.commit()

    async def updates_after(self, bot_code: str, after: int, limit: int) -> list[str]:
        """Everything newer than the main server's cursor, oldest first."""
        cursor = await self._db.execute(
            """
            SELECT payload FROM inbound_updates
            WHERE bot_code = ? AND event_id > ?
            ORDER BY event_id
            LIMIT ?
            """,
            (bot_code, after, limit),
        )
        return [str(row["payload"]) for row in await cursor.fetchall()]

    async def prune_inbound_updates(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        cursor = await self._db.execute("DELETE FROM inbound_updates WHERE created_at < ?", (cutoff,))
        await self._db.commit()
        return cursor.rowcount

    async def count_inbound_updates(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) AS total FROM inbound_updates")
        row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    async def remember_send(self, idempotency_key: str, message_ref: str | None) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO sent_keys (idempotency_key, message_ref, sent_at) VALUES (?, ?, ?)",
            (idempotency_key, message_ref, _now()),
        )
        await self._db.commit()

    async def previous_send(self, idempotency_key: str) -> tuple[bool, str | None]:
        cursor = await self._db.execute(
            "SELECT message_ref FROM sent_keys WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return False, None
        return True, None if row["message_ref"] is None else str(row["message_ref"])

    async def prune_sent_keys(self, ttl_hours: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=ttl_hours)).isoformat()
        cursor = await self._db.execute("DELETE FROM sent_keys WHERE sent_at < ?", (cutoff,))
        await self._db.commit()
        return cursor.rowcount

    async def count_sent_keys(self) -> int:
        cursor = await self._db.execute("SELECT COUNT(*) AS total FROM sent_keys")
        row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    @staticmethod
    def _record(row: aiosqlite.Row) -> BotRecord:
        return BotRecord(
            bot_code=str(row["bot_code"]),
            encrypted_token=str(row["encrypted_token"]),
            title=str(row["title"]),
            invite_link_template=str(row["invite_link_template"]),
            last_update_id=int(row["last_update_id"]),
            is_active=bool(row["is_active"]),
        )
