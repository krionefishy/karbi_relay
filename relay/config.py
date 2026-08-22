"""Configuration, read once from the environment.

Secrets have no defaults on purpose: a relay that starts with a guessable JWT
secret is worse than one that refuses to start.
"""

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} must be set")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from error


def _keys(name: str) -> tuple[str, ...]:
    raw = _required(name)
    keys = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not keys:
        raise ConfigError(f"{name} must list at least one key")
    return keys


@dataclass(frozen=True)
class ChannelConfig:
    """The shared JWT channel between the main server and this relay.

    One secret, two audiences: a token minted for a call *to* the relay must not
    be replayable *back* at the main server, and vice versa.
    """

    jwt_secret: str = field(repr=False)
    issuer: str
    inbound_audience: str
    outbound_audience: str
    algorithm: str = "HS256"
    ttl_seconds: int = 300
    leeway_seconds: int = 30


@dataclass(frozen=True)
class TelegramConfig:
    api_base_url: str = "https://api.telegram.org"
    poll_timeout_seconds: int = 25
    request_timeout_seconds: int = 40
    connect_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class Config:
    channel: ChannelConfig
    telegram: TelegramConfig
    encryption_keys: tuple[str, ...] = field(repr=False)
    fingerprint_key: str = field(repr=False)
    database_path: str = "/data/relay.sqlite3"
    bot_refresh_seconds: int = 60
    dedupe_ttl_hours: int = 24
    # Telegram сам хранит апдейты сутки; неделя буфера здесь — запас на случай,
    # когда основной сервер лежит дольше, чем кто-либо рассчитывал.
    inbound_retention_days: int = 7
    poller_retry_seconds: float = 5.0
    log_level: str = "INFO"

    def validate(self) -> None:
        if self.telegram.request_timeout_seconds <= self.telegram.poll_timeout_seconds:
            # Long polling holds the connection for the whole poll timeout, so a
            # request timeout below it would cancel every idle poll.
            raise ConfigError("TELEGRAM_REQUEST_TIMEOUT_SECONDS must exceed TELEGRAM_POLL_TIMEOUT_SECONDS")
        if self.channel.inbound_audience == self.channel.outbound_audience:
            raise ConfigError("CHANNEL_INBOUND_AUDIENCE and CHANNEL_OUTBOUND_AUDIENCE must differ")
        if len(self.channel.jwt_secret) < 32:
            raise ConfigError("CHANNEL_JWT_SECRET must be at least 32 characters")


def load_config() -> Config:
    config = Config(
        channel=ChannelConfig(
            jwt_secret=_required("CHANNEL_JWT_SECRET"),
            issuer=os.environ.get("CHANNEL_ISSUER", "marketplace-auto"),
            inbound_audience=os.environ.get("CHANNEL_INBOUND_AUDIENCE", "relay"),
            outbound_audience=os.environ.get("CHANNEL_OUTBOUND_AUDIENCE", "main"),
            ttl_seconds=_int("CHANNEL_JWT_TTL_SECONDS", 300),
            leeway_seconds=_int("CHANNEL_JWT_LEEWAY_SECONDS", 30),
        ),
        telegram=TelegramConfig(
            api_base_url=os.environ.get("TELEGRAM_API_BASE_URL", "https://api.telegram.org"),
            poll_timeout_seconds=_int("TELEGRAM_POLL_TIMEOUT_SECONDS", 25),
            request_timeout_seconds=_int("TELEGRAM_REQUEST_TIMEOUT_SECONDS", 40),
        ),
        encryption_keys=_keys("CREDENTIALS_ENCRYPTION_KEYS"),
        fingerprint_key=_required("CREDENTIALS_FINGERPRINT_KEY"),
        database_path=os.environ.get("RELAY_DATABASE_PATH", "/data/relay.sqlite3"),
        bot_refresh_seconds=_int("BOT_REFRESH_SECONDS", 60),
        dedupe_ttl_hours=_int("DEDUPE_TTL_HOURS", 24),
        inbound_retention_days=_int("INBOUND_RETENTION_DAYS", 7),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    config.validate()
    return config
