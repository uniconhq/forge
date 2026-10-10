"""What the org account holds at Woodpecker, as the platform stores it whole
and encrypted, and only this implementation reads it: the account's user id
at the CI, the token its last sign-in minted, when that sign-in was, and
the account's id at the forge, by which a refresh finds it: anyone may sign
up under a name that looks like the account's, so the name decides nothing.

It is a stored format. Every org's row holds one, and the migration that
made the opaque state (`0017`) wrote it from the three columns the platform
kept before, so renaming a member is a migration of every row.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from forge.domain.errors import Rejected
from forge.domain.identity import CiState


@dataclass(frozen=True, slots=True)
class WoodpeckerState:
    user_id: int | None
    token: str
    signed_in_at: datetime | None
    account_id: int | None = None


def read_state(state: CiState) -> WoodpeckerState:
    """The state the platform handed back. `Rejected` for one this
    implementation did not write.
    """
    try:
        found = json.loads(state)
        user_id, token, signed_in_at = found["user_id"], found["token"], found["signed_in_at"]
        account_id = found.get("account_id")
        when = datetime.fromisoformat(signed_in_at) if signed_in_at is not None else None
    except ValueError, TypeError, KeyError, AttributeError:
        raise Rejected("the org account's CI state is not Woodpecker's") from None
    numbers = (user_id, account_id)
    if not isinstance(token, str) or any(
        number is not None and not isinstance(number, int) for number in numbers
    ):
        raise Rejected("the org account's CI state is not Woodpecker's")
    return WoodpeckerState(user_id=user_id, token=token, signed_in_at=when, account_id=account_id)


def written(state: WoodpeckerState) -> CiState:
    return CiState(
        json.dumps(
            {
                "user_id": state.user_id,
                "token": state.token,
                "signed_in_at": state.signed_in_at.isoformat() if state.signed_in_at else None,
                "account_id": state.account_id,
            }
        )
    )
