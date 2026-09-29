"""The identity area over Forgejo."""

from collections.abc import Sequence

from forge.domain.identity import Credential, User
from forge.forges.forgejo.oauth import OAuth
from forge.forges.forgejo.users import Users
from forge.port.identity import AccountVisibility, SignedIn

SIGN_UP_PATH = "/user/sign_up"


class ForgejoIdentity:
    def __init__(self, oauth: OAuth, users: Users, *, public_url: str, sign_ups_open: bool) -> None:
        self._oauth = oauth
        self._users = users
        self._public_url = public_url.rstrip("/")
        self._sign_ups_open = sign_ups_open

    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        return self._oauth.sign_in_url(state=state, code_challenge=code_challenge, nonce=nonce)

    def sign_up_url(self) -> str | None:
        return f"{self._public_url}{SIGN_UP_PATH}" if self._sign_ups_open else None

    async def complete_sign_in(self, *, code: str, verifier: str) -> SignedIn:
        return await self._oauth.complete_sign_in(code=code, verifier=verifier)

    async def refresh_credential(self, credential: Credential) -> Credential:
        return await self._oauth.refresh(credential)

    async def user_of(self, credential: Credential) -> User:
        return await self._oauth.user_of(credential)

    async def find_user(self, user_id: int) -> User:
        return await self._users.find(user_id)

    async def find_user_by_username(self, username: str) -> User:
        return await self._users.find_by_username(username)

    async def verified_emails(self, user_id: int) -> tuple[str, ...]:
        return await self._users.verified_emails(user_id)

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        must_change_password: bool,
        visibility: AccountVisibility = "public",
    ) -> User:
        return await self._users.create(
            username,
            email,
            password,
            must_change_password=must_change_password,
            visibility=visibility,
        )

    async def set_password(self, user_id: int, password: str) -> None:
        await self._users.set_password(user_id, password)

    async def mint_token(
        self, username: str, password: str, *, name: str, scopes: Sequence[str]
    ) -> str:
        return await self._users.mint_token(username, password, name=name, scopes=scopes)

    async def deactivate_user(self, user_id: int) -> None:
        await self._users.deactivate(user_id)

    async def delete_user(self, user_id: int) -> None:
        await self._users.delete(user_id)
