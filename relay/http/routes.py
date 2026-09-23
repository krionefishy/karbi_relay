"""HTTP surface. Four endpoints, no more.

Bodies are never logged here: they carry bot tokens and message text.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from relay.core.auth import ChannelAuth, ChannelAuthError
from relay.http.schemas import (
    BotReadiness,
    BotRequest,
    BotResponse,
    ReadyResponse,
    SendRequest,
    SendResponse,
    UpdatesResponse,
)
from relay.telegram.client import (
    MessengerAuthError,
    MessengerConflictError,
    MessengerPermanentError,
    MessengerRateLimited,
    MessengerTemporaryError,
    Photo,
)

logger = logging.getLogger("relay.http")

router = APIRouter()
public_router = APIRouter()


async def authenticated(request: Request, authorization: str | None = Header(default=None)) -> str:
    auth: ChannelAuth = request.app.state.channel_auth
    try:
        return auth.verify(authorization)
    except ChannelAuthError as error:
        logger.warning("channel_auth_rejected reason=%s", error)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized") from error


@router.post("/api/v1/bots", response_model=BotResponse, dependencies=[Depends(authenticated)])
async def upsert_bot(payload: BotRequest, request: Request) -> BotResponse:
    storage = request.app.state.storage
    cipher = request.app.state.cipher
    client = request.app.state.messenger

    if payload.action == "delete":
        await storage.delete_bot(payload.bot_code)
        logger.info("bot_deleted bot=%s", payload.bot_code)
        return BotResponse(bot_code=payload.bot_code, title="", invite_link_template="")

    if payload.token is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="token is required for upsert")

    token = payload.token.get_secret_value().strip()
    fingerprint = cipher.fingerprint(token)
    owner = await storage.owner_of_fingerprint(fingerprint)
    if owner is not None and owner != payload.bot_code:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"this token is already registered as {owner}",
        )

    try:
        identity = await client.identity(token)
    except (MessengerAuthError, MessengerPermanentError) as error:
        # A bot that cannot introduce itself is not worth storing.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error
    except (MessengerTemporaryError, MessengerRateLimited, MessengerConflictError) as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error

    await storage.upsert_bot(
        bot_code=payload.bot_code,
        encrypted_token=cipher.encrypt(token),
        token_fingerprint=fingerprint,
        title=identity.title,
        invite_link_template=identity.invite_link_template,
    )
    logger.info("bot_registered bot=%s", payload.bot_code)
    return BotResponse(
        bot_code=payload.bot_code,
        title=identity.title,
        invite_link_template=identity.invite_link_template,
    )


@router.post("/api/v1/messages/send", response_model=SendResponse, dependencies=[Depends(authenticated)])
async def send_message(payload: SendRequest, request: Request) -> SendResponse:
    storage = request.app.state.storage
    cipher = request.app.state.cipher
    client = request.app.state.messenger

    seen, message_ref = await storage.previous_send(payload.idempotency_key)
    if seen:
        # A retry after a lost response must not reach the seller twice.
        return SendResponse(message_ref=message_ref, deduplicated=True)

    bot = await storage.bot(payload.bot_code)
    if bot is None or not bot.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown bot {payload.bot_code}")

    token = cipher.decrypt(bot.encrypted_token)
    try:
        photo = (
            Photo.from_base64(payload.attachment.png_base64, payload.attachment.caption)
            if payload.attachment is not None
            else None
        )
        sent_ref = await client.send(token, payload.recipient, payload.text, photo)
    except MessengerRateLimited as error:
        # The header has to ride on the exception: headers set on a Response
        # object are dropped when FastAPI renders an HTTPException instead.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
            headers={"Retry-After": str(error.retry_after)},
        ) from error
    except MessengerAuthError as error:
        # Not this message's fault: park the bot so the operator notices, and let
        # the caller retry once the token is replaced.
        await storage.deactivate_bot(payload.bot_code, str(error))
        logger.error("bot_parked bot=%s reason=%s", payload.bot_code, error)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    except (MessengerTemporaryError, MessengerConflictError) as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    except MessengerPermanentError as error:
        # Unknown chat, blocked bot, empty text: retrying sends it into the same wall.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)) from error

    await storage.remember_send(payload.idempotency_key, sent_ref)
    return SendResponse(message_ref=sent_ref, deduplicated=False)


@router.get("/api/v1/updates", response_model=UpdatesResponse, dependencies=[Depends(authenticated)])
async def updates(
    request: Request,
    bot_code: str = Query(min_length=1, max_length=64),
    after: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    wait: int = Query(25, ge=0, le=60),
) -> UpdatesResponse:
    """Long polling, our side of it.

    The main server cannot be reached from here, so it comes and asks. Holding
    the request for `wait` seconds keeps a quiet channel from turning into a
    request per second while a message still arrives without delay.
    """
    storage = request.app.state.storage
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        buffered = await storage.updates_after(bot_code, after, limit)
        if buffered or asyncio.get_running_loop().time() >= deadline:
            return UpdatesResponse(updates=[json.loads(payload) for payload in buffered])
        await asyncio.sleep(0.5)


@public_router.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Never calls the messenger: a probe every few seconds would
    spend rate limit budget and flap on every transient error."""
    return {"status": "ok"}


@router.get("/ready", response_model=ReadyResponse, dependencies=[Depends(authenticated)])
async def ready(request: Request) -> ReadyResponse:
    supervisor = request.app.state.supervisor
    storage = request.app.state.storage
    poll_timeout = request.app.state.config.telegram.poll_timeout_seconds
    # A poll that returns nothing still counts as success, so a bot that has not
    # reported one in three poll windows is not talking to the messenger.
    stale_after = timedelta(seconds=poll_timeout * 3 + 30)
    now = datetime.now(UTC)

    bots: dict[str, BotReadiness] = {}
    healthy = True
    for code, state in supervisor.states.items():
        fresh = state.last_successful_poll_at is not None and now - state.last_successful_poll_at < stale_after
        if state.parked or not fresh:
            healthy = False
        bots[code] = BotReadiness(
            last_successful_poll_at=(
                state.last_successful_poll_at.isoformat() if state.last_successful_poll_at else None
            ),
            last_buffered_event_id=state.last_buffered_event_id,
            consecutive_errors=state.consecutive_errors,
            last_error=state.last_error,
            parked=state.parked,
        )

    return ReadyResponse(
        ok=healthy,
        bots=bots,
        pending_dedupe_keys=await storage.count_sent_keys(),
        buffered_updates=await storage.count_inbound_updates(),
    )
