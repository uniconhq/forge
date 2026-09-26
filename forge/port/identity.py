"""Identity: signing a user in through the host, keeping their credential
alive, and the account lifecycle.
"""

from dataclasses import dataclass
from typing import Protocol

from forge.domain.identity import Credential, User


@dataclass(frozen=True, slots=True)
class SignedIn:
    """What completing a sign-in yields: who signed in, the credential to act
    as them, and the nonce the identity answer carried, when it carried one.
    """

    user: User
    credential: Credential
    nonce: str | None


class IdentityPort(Protocol):
    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        """Where to send a browser to sign in, carrying the checks for the
        answer.
        """
        ...

    def sign_up_url(self) -> str | None:
        """Where a person creates an account at the host, or none when the
        host takes no sign-ups.
        """
        ...

    async def complete_sign_in(self, *, code: str, verifier: str) -> SignedIn:
        """Exchange the code the host sent back and read who signed in.
        `Forbidden` when the code or the verifier is not accepted;
        `Misconfigured` when the host refuses the platform's own registration.
        """
        ...

    async def refresh_credential(self, credential: Credential) -> Credential:
        """A fresh credential for the same user. `Forbidden` once the host
        will no longer renew it.
        """
        ...

    async def user_of(self, credential: Credential) -> User:
        """Who a credential belongs to. `Forbidden` when the host does not
        accept it.
        """
        ...

    async def find_user(self, user_id: int) -> User:
        """`NotFound` when no user has that id."""
        ...

    async def deactivate_user(self, user_id: int) -> None:
        """Stop the user signing in, reversibly, without removing anything."""
        ...

    async def delete_user(self, user_id: int) -> None:
        """Remove the user and whatever they still own."""
        ...
