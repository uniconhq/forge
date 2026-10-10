"""What an org account holds at the CI becomes one value, `ci_state`, that the
CI's implementation alone reads: the three columns Woodpecker's sign-in
filled, `ci_token`, `ci_user_id` and `ci_signed_in_at`, go into it as the
JSON object that implementation keeps (`forges/forgejo/ci_state.py`), with
the account's id at the forge beside them, encrypted the way the token was. Each token is moved as it is, so no org
signs in again; an account that was never signed in at the CI holds an
empty state. Going back gives the three columns back from the state.

Moving a token means opening it and sealing it again, so this revision
reads `UNICON_TOKEN_ENCRYPTION_KEY` from the environment whenever there is
a token to move, in either direction, and stops before it changes anything
when the key is not there. Nothing it opens is written anywhere but the
row.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-10 12:00:00
"""

import base64
import json
import os
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY_VARIABLE = "UNICON_TOKEN_ENCRYPTION_KEY"
NONCE_BYTES = 12

accounts = sa.table(
    "org_accounts",
    sa.column("id", sa.Uuid()),
    sa.column("forge_user_id", sa.BigInteger()),
    sa.column("ci_token", sa.LargeBinary()),
    sa.column("ci_user_id", sa.BigInteger()),
    sa.column("ci_signed_in_at", sa.TIMESTAMP(timezone=True)),
    sa.column("ci_state", sa.LargeBinary()),
)


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.select(
            accounts.c.id,
            accounts.c.forge_user_id,
            accounts.c.ci_token,
            accounts.c.ci_user_id,
            accounts.c.ci_signed_in_at,
        )
    ).all()
    moving = [row for row in rows if row.ci_token]
    key = _key() if moving else None
    op.add_column("org_accounts", sa.Column("ci_state", sa.LargeBinary(), nullable=True))
    for row in rows:
        state = b""
        if row.ci_token:
            assert key is not None
            token = _open(key, row.ci_token).decode()
            at = row.ci_signed_in_at
            signed_in_at = at.astimezone(UTC).isoformat() if at else None
            plaintext = json.dumps(
                {
                    "user_id": row.ci_user_id,
                    "token": token,
                    "signed_in_at": signed_in_at,
                    "account_id": row.forge_user_id,
                }
            )
            state = _seal(key, plaintext.encode())
        connection.execute(accounts.update().where(accounts.c.id == row.id).values(ci_state=state))
    op.alter_column("org_accounts", "ci_state", nullable=False)
    op.drop_column("org_accounts", "ci_signed_in_at")
    op.drop_column("org_accounts", "ci_user_id")
    op.drop_column("org_accounts", "ci_token")


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.select(accounts.c.id, accounts.c.ci_state)).all()
    moving = [row for row in rows if row.ci_state]
    key = _key() if moving else None
    op.add_column("org_accounts", sa.Column("ci_token", sa.LargeBinary(), nullable=True))
    op.add_column("org_accounts", sa.Column("ci_user_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "org_accounts", sa.Column("ci_signed_in_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    for row in rows:
        token, user_id, signed_in_at = b"", None, None
        if row.ci_state:
            assert key is not None
            state = json.loads(_open(key, row.ci_state))
            token = _seal(key, str(state["token"]).encode())
            user_id = state["user_id"]
            signed_in_at = (
                datetime.fromisoformat(state["signed_in_at"]) if state["signed_in_at"] else None
            )
        connection.execute(
            accounts.update()
            .where(accounts.c.id == row.id)
            .values(ci_token=token, ci_user_id=user_id, ci_signed_in_at=signed_in_at)
        )
    op.alter_column("org_accounts", "ci_token", nullable=False)
    op.drop_column("org_accounts", "ci_state")


def _key() -> bytes:
    raw = os.environ.get(KEY_VARIABLE, "")
    if not raw:
        raise RuntimeError(
            f"{KEY_VARIABLE} is needed to move the org accounts' CI credentials; "
            "set it for this migration, as the backend has it"
        )
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _seal(key: bytes, plaintext: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _open(key: bytes, blob: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], None)
    except InvalidTag:
        raise RuntimeError(
            f"an org account's CI credential does not open with {KEY_VARIABLE}"
        ) from None
