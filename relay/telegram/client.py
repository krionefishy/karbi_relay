"""Everything the relay knows about Telegram lives in this file.

Swapping the messenger means writing a sibling of this module and leaving the
rest of the service alone: the API, the poller and the storage speak in
`bot_code`, `recipient` and `NormalizedUpdate`, never in Telegram's shapes.
"""

import base64
import binascii
import logging
from dataclasses import dataclass
from typing import Any

import httpx


class MessengerError(Exception):
    """Base class: something went wrong talking to the messenger."""


class MessengerTemporaryError(MessengerError):
    """Network trouble or a 5xx — the same call may succeed later."""


class MessengerRateLimited(MessengerError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited for {retry_after}s")
        self.retry_after = retry_after


class MessengerConflictError(MessengerError):
    """Another poller holds this token."""


class MessengerAuthError(MessengerError):
    """The token is not valid — parking the bot is the only sane response."""


class MessengerPermanentError(MessengerError):
    """The messenger refused for a reason that will not change on retry."""


@dataclass(frozen=True)
class Sender:
    external_id: str | None
    username: str
    display_name: str


@dataclass(frozen=True)
class NormalizedUpdate:
    """The only shape the main server ever sees."""

    event_id: int
    recipient: str
    text: str
    sender: Sender

    def as_payload(self, bot_code: str) -> dict[str, Any]:
        return {
            "bot_code": bot_code,
            "event_id": self.event_id,
            "recipient": self.recipient,
            "text": self.text,
            "sender": {
                "external_id": self.sender.external_id,
                "username": self.sender.username,
                "display_name": self.sender.display_name,
            },
        }


@dataclass(frozen=True)
class BotIdentity:
    title: str
    invite_link_template: str


@dataclass(frozen=True)
class Photo:
    """A PNG to send next to the text, with its own short caption."""

    png: bytes
    caption: str = ""

    @classmethod
    def from_base64(cls, encoded: str, caption: str = "") -> "Photo":
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise MessengerPermanentError("attachment is not valid base64") from error
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise MessengerPermanentError("attachment is not a PNG")
        return cls(png=raw, caption=caption)


# Telegram caps a photo caption at 1024 characters; a longer text goes as its own message.
CAPTION_LIMIT = 1024


class TelegramClient:
    """Every call carries the token explicitly: one process serves many bots."""

    def __init__(
        self,
        base_url: str = "https://api.telegram.org",
        request_timeout_seconds: float = 40.0,
        poll_timeout_seconds: int = 25,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(request_timeout_seconds, connect=connect_timeout_seconds)
        self.poll_timeout_seconds = poll_timeout_seconds
        self.logger = logging.getLogger("relay.telegram")

    async def identity(self, token: str) -> BotIdentity:
        """Ask the messenger who this token belongs to.

        The invite link template is built here and nowhere else: the main server
        stores it as an opaque string and only substitutes its own invite token.
        """
        payload = await self._call(token, "getMe", {})
        if not isinstance(payload, dict):
            raise MessengerPermanentError("getMe returned an unexpected body")
        username = str(payload.get("username") or "")
        if not username:
            raise MessengerPermanentError("the messenger returned a bot without a username")
        title = str(payload.get("first_name") or username)
        return BotIdentity(
            title=title,
            invite_link_template=f"https://t.me/{username}?start={{token}}",
        )

    async def updates(self, token: str, offset: int) -> list[NormalizedUpdate]:
        payload = await self._call(
            token,
            "getUpdates",
            {"offset": offset, "timeout": self.poll_timeout_seconds, "allowed_updates": ["message"]},
        )
        if not isinstance(payload, list):
            raise MessengerTemporaryError("getUpdates returned an unexpected body")
        return [update for raw in payload if (update := self._normalize(raw)) is not None]

    async def send(self, token: str, recipient: str, text: str, photo: Photo | None = None) -> str | None:
        """Text, or text with a picture.

        A short text rides as the photo's caption — one message in the chat.
        A long one goes first as its own message, then the photo with its short
        caption; the reference returned is the text's, that is what the caller
        deduplicates on.
        """
        if photo is not None and len(text) <= CAPTION_LIMIT:
            return self._message_ref(await self._send_photo(token, recipient, photo.png, text))
        payload = await self._call(
            token,
            "sendMessage",
            {"chat_id": recipient, "text": text, "disable_web_page_preview": True},
        )
        reference = self._message_ref(payload)
        if photo is not None:
            await self._send_photo(token, recipient, photo.png, photo.caption)
        return reference

    async def _send_photo(self, token: str, recipient: str, png: bytes, caption: str) -> Any:
        return await self._call_multipart(
            token,
            "sendPhoto",
            data={"chat_id": recipient, "caption": caption[:CAPTION_LIMIT]},
            files={"photo": ("qr.png", png, "image/png")},
        )

    @staticmethod
    def _message_ref(payload: Any) -> str | None:
        if isinstance(payload, dict) and isinstance(payload.get("message_id"), int):
            return str(payload["message_id"])
        return None

    @staticmethod
    def _normalize(raw: Any) -> NormalizedUpdate | None:
        """Drop anything that is not a plain text message from a chat."""
        if not isinstance(raw, dict):
            return None
        message = raw.get("message")
        event_id = raw.get("update_id")
        if not isinstance(message, dict) or not isinstance(event_id, int):
            return None
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return None
        sender = message.get("from") or {}
        external_id = sender.get("id")
        return NormalizedUpdate(
            event_id=event_id,
            recipient=str(chat_id),
            text=str(message.get("text") or ""),
            sender=Sender(
                external_id=str(external_id) if isinstance(external_id, int) else None,
                username=str(sender.get("username") or ""),
                display_name=str(sender.get("first_name") or ""),
            ),
        )

    async def _call(self, token: str, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}/bot{token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as error:
            raise MessengerTemporaryError(f"{method} failed: {type(error).__name__}") from error
        return self._unwrap(method, response)

    async def _call_multipart(
        self, token: str, method: str, *, data: dict[str, Any], files: dict[str, tuple[str, bytes, str]]
    ) -> Any:
        url = f"{self.base_url}/bot{token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, data=data, files=files)
        except httpx.HTTPError as error:
            raise MessengerTemporaryError(f"{method} failed: {type(error).__name__}") from error
        return self._unwrap(method, response)

    @staticmethod
    def _unwrap(method: str, response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError as error:
            raise MessengerTemporaryError(f"{method} returned a non-JSON body") from error
        if body.get("ok"):
            return body.get("result")

        code = body.get("error_code")
        description = str(body.get("description") or "the messenger rejected the request")
        if code == 409:
            raise MessengerConflictError(description)
        if code == 401:
            raise MessengerAuthError(description)
        if code == 429:
            parameters = body.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            raise MessengerRateLimited(int(retry_after) if isinstance(retry_after, int) else 1)
        if isinstance(code, int) and code >= 500:
            raise MessengerTemporaryError(description)
        # 400 and 403 mean the request itself is wrong: unknown chat, blocked
        # bot, empty text. Retrying sends the same message into the same wall.
        raise MessengerPermanentError(description)
