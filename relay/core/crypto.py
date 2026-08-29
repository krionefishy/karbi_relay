"""Token encryption at rest.

Deliberately identical in shape to the main server's CredentialCipher: same
library, same rotation story, same fingerprint scheme. The key itself is
different and never leaves this machine.
"""

import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CredentialDecryptionError(ValueError):
    pass


class CredentialCipher:
    """Authenticated encryption with first-key writes and old-key reads for rotation."""

    def __init__(self, keys: tuple[str, ...], fingerprint_key: str) -> None:
        if not keys:
            raise ValueError("At least one credential encryption key is required")
        if not fingerprint_key:
            raise ValueError("A credential fingerprint key is required")
        self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])
        self._fingerprint_key = fingerprint_key.encode()

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as error:
            raise CredentialDecryptionError("Credential cannot be decrypted") from error

    def fingerprint(self, value: str) -> str:
        return hmac.new(self._fingerprint_key, value.encode(), hashlib.sha256).hexdigest()

    def matches_fingerprint(self, value: str, fingerprint: str) -> bool:
        return hmac.compare_digest(self.fingerprint(value), fingerprint)
