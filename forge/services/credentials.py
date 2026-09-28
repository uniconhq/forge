"""A credential at rest. A credential stored in the database is AES-256-GCM
ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`, unreadable without the key.

The plaintext is a JSON object with three members, `access`, `refresh` and
`expires_at`, and it is a stored format: every live session row holds one,
so renaming a member is a migration of every such row.
"""

import json
import secrets
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from forge.domain.identity import Credential
from forge.settings import Settings

NONCE_BYTES = 12


class CannotDecrypt(Exception):
    """The ciphertext was produced under a different key or has been altered."""


def encrypt(credential: Credential, settings: Settings) -> bytes:
    plaintext = json.dumps(
        {
            "access": credential.access,
            "refresh": credential.refresh,
            "expires_at": credential.expires_at.isoformat(),
        }
    ).encode()
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(settings.token_encryption_key_bytes).encrypt(nonce, plaintext, None)


def decrypt(blob: bytes, settings: Settings) -> Credential:
    """The credential in `blob`. `CannotDecrypt` when it was written under
    another key or has been altered.
    """
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        plaintext = AESGCM(settings.token_encryption_key_bytes).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise CannotDecrypt("the stored value does not decrypt with this key") from exc
    payload = json.loads(plaintext)
    return Credential(
        access=str(payload["access"]),
        refresh=str(payload["refresh"]),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
    )
