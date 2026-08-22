"""JWT on the channel between the main server and the relay.

One shared secret, two audiences. Tokens are short-lived and carry a `jti`, so a
token scraped from a log is useless within minutes and cannot be aimed at the
other direction of the channel.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from relay.config import ChannelConfig


class ChannelAuthError(Exception):
    """The caller did not present a usable token."""


class ChannelAuth:
    def __init__(self, config: ChannelConfig) -> None:
        self._config = config

    def issue(self) -> str:
        """Mint a token for an outgoing call to the main server."""
        now = datetime.now(UTC)
        payload = {
            "sub": "relay",
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(seconds=self._config.ttl_seconds),
            "iss": self._config.issuer,
            "aud": self._config.outbound_audience,
        }
        return jwt.encode(payload, self._config.jwt_secret, algorithm=self._config.algorithm)

    def verify(self, header: str | None) -> str:
        """Check an inbound Authorization header, returning the caller's subject."""
        if not header or not header.lower().startswith("bearer "):
            raise ChannelAuthError("missing bearer token")
        token = header.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(
                token,
                self._config.jwt_secret,
                algorithms=[self._config.algorithm],
                issuer=self._config.issuer,
                audience=self._config.inbound_audience,
                leeway=self._config.leeway_seconds,
                options={"require": ["sub", "jti", "iat", "exp", "iss", "aud"]},
            )
        except jwt.InvalidTokenError as error:
            raise ChannelAuthError(str(error)) from error
        return str(payload["sub"])
