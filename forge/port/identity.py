"""Identity: signing a user in through the host, keeping their credential
alive, and the account lifecycle, including the accounts the platform makes
itself: a person's when sign-up is closed, and an org's service account.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from forge.domain.identity import Credential, User

AccountVisibility = Literal["public", "private"]


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

    async def find_user_by_username(self, username: str) -> User:
        """`NotFound` when no user has that username."""
        ...

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        must_change_password: bool,
        visibility: AccountVisibility = "public",
    ) -> User:
        """Make an account at the host, as the platform. `private` keeps it out
        of the host's own listings, for a service account. `Conflict` when the
        username or the email is taken.
        """
        ...

    async def set_password(self, user_id: int, password: str) -> None:
        """Replace the user's password, as the platform, without knowing the
        old one. How the platform signs a service account in again without
        ever storing its password.
        """
        ...

    async def mint_token(
        self, username: str, password: str, *, name: str, scopes: Sequence[str]
    ) -> str:
        """A long-lived credential for the user, made with their password and
        named so a rerun replaces it rather than adding another. `Forbidden`
        when the password is wrong.
        """
        ...

    async def deactivate_user(self, user_id: int) -> None:
        """Stop the user signing in, reversibly, without removing anything."""
        ...

    async def delete_user(self, user_id: int) -> None:
        """Remove the user and what they own. What other people still read,
        their questions and answers, stays and reads as nobody's.
        """
        ...
