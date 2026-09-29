"""A credential at rest. A credential stored in the database is AES-256-GCM
ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`, unreadable without the key.

The plaintext of a session's credential is a JSON object with three members,
`access`, `refresh` and `expires_at`, and it is a stored format: every live
session row holds one, so renaming a member is a migration of every such row.
An org account's two tokens and its event secret are stored the same way,
each as the one string it is, with `encrypt_text` and `decrypt_text`.
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
    return seal(plaintext, settings)


def decrypt(blob: bytes, settings: Settings) -> Credential:
    """The credential in `blob`. `CannotDecrypt` when it was written under
    another key or has been altered.
    """
    payload = json.loads(open_sealed(blob, settings))
    return Credential(
        access=str(payload["access"]),
        refresh=str(payload["refresh"]),
        expires_at=datetime.fromisoformat(str(payload["expires_at"])),
    )


def encrypt_text(text: str, settings: Settings) -> bytes:
    """One string as ciphertext, for a token or a secret held on its own."""
    return seal(text.encode(), settings)


def decrypt_text(blob: bytes, settings: Settings) -> str:
    """The string in `blob`. `CannotDecrypt` when it was written under another
    key or has been altered.
    """
    return open_sealed(blob, settings).decode()


def seal(plaintext: bytes, settings: Settings) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(settings.token_encryption_key_bytes).encrypt(nonce, plaintext, None)


def open_sealed(blob: bytes, settings: Settings) -> bytes:
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(settings.token_encryption_key_bytes).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise CannotDecrypt("the stored value does not decrypt with this key") from exc
