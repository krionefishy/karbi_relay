from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.config import ChannelConfig, Config, TelegramConfig

TELEGRAM = "https://api.telegram.invalid"
SECRET = "test-secret-that-is-long-enough-for-validation"


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        channel=ChannelConfig(
            jwt_secret=SECRET,
            issuer="marketplace-auto",
            inbound_audience="relay",
            outbound_audience="main",
            ttl_seconds=300,
        ),
        telegram=TelegramConfig(api_base_url=TELEGRAM, poll_timeout_seconds=1, request_timeout_seconds=5),
        encryption_keys=(Fernet.generate_key().decode(),),
        fingerprint_key="fingerprint-key-for-tests",
        database_path=str(tmp_path / "relay.sqlite3"),
        # High enough that the supervisor never runs a second pass mid-test.
        bot_refresh_seconds=3600,
    )


def token(*, audience: str = "relay", issuer: str = "marketplace-auto", expired: bool = False) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": "main",
            "jti": "test-token",
            "iat": now - timedelta(seconds=600 if expired else 0),
            "exp": now - timedelta(seconds=300) if expired else now + timedelta(seconds=300),
            "iss": issuer,
            "aud": audience,
        },
        SECRET,
        algorithm="HS256",
    )


def auth_header(**kwargs) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(**kwargs)}"}


@pytest.fixture
def client(config: Config) -> Iterator[TestClient]:
    with TestClient(create_app(config)) as test_client:
        yield test_client
