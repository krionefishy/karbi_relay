"""Application wiring and lifespan."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from relay.core.auth import ChannelAuth
from relay.core.config import Config, load_config
from relay.core.crypto import CredentialCipher
from relay.core.storage import Storage
from relay.http.routes import public_router, router
from relay.telegram.client import TelegramClient
from relay.telegram.poller import BotSupervisor


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx пишет каждый запрос с полным URL, а у Telegram токен бота — часть URL.
    # Ниже WARNING эти логгеры не опускаются, что бы ни стояло в log_level.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Answer 422 without echoing the input.

    FastAPI's default handler returns the offending value inside `detail`, which
    for POST /api/v1/bots would hand a bot token straight back to the caller and
    into any log that records response bodies.
    """
    errors: list[dict[str, Any]] = []
    if isinstance(exc, RequestValidationError):
        errors = [{"loc": list(error.get("loc", ())), "msg": error.get("msg", "invalid")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


def create_app(config: Config | None = None) -> FastAPI:
    settings = config or load_config()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        storage = Storage(settings.database_path)
        await storage.connect()
        cipher = CredentialCipher(settings.encryption_keys, settings.fingerprint_key)
        channel_auth = ChannelAuth(settings.channel)
        messenger = TelegramClient(
            base_url=settings.telegram.api_base_url,
            request_timeout_seconds=settings.telegram.request_timeout_seconds,
            poll_timeout_seconds=settings.telegram.poll_timeout_seconds,
            connect_timeout_seconds=settings.telegram.connect_timeout_seconds,
        )
        supervisor = BotSupervisor(
            storage=storage,
            client=messenger,
            cipher=cipher,
            refresh_seconds=settings.bot_refresh_seconds,
            dedupe_ttl_hours=settings.dedupe_ttl_hours,
            inbound_retention_days=settings.inbound_retention_days,
            retry_seconds=settings.poller_retry_seconds,
        )

        app.state.config = settings
        app.state.storage = storage
        app.state.cipher = cipher
        app.state.channel_auth = channel_auth
        app.state.messenger = messenger
        app.state.supervisor = supervisor

        supervisor_task = asyncio.create_task(supervisor.run(), name="bot-supervisor")
        try:
            yield
        finally:
            supervisor.stop()
            supervisor_task.cancel()
            await asyncio.gather(supervisor_task, return_exceptions=True)
            await supervisor.shutdown()
            await storage.close()

    app = FastAPI(title="Marketplace Auto Relay", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(public_router)
    app.include_router(router)
    return app
