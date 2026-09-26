"""Encryption of credentials at rest. A credential stored in the database is
AES-256-GCM ciphertext under `UNICON_TOKEN_ENCRYPTION_KEY`, unreadable without
the key.
"""

import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


class CannotDecrypt(Exception):
    """The ciphertext was produced under a different key or has been altered."""


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def decrypt(blob: bytes, key: bytes) -> bytes:
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise CannotDecrypt("the stored value does not decrypt with this key") from exc
