"""The identity area in memory."""

import base64
import hashlib
import secrets
from collections.abc import Sequence
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit

from forge.domain.errors import Conflict, Forbidden
from forge.domain.identity import PLATFORM, Credential, User
from forge.forges.fake.state import State
from forge.port.identity import AccountVisibility, SignedIn


class FakeIdentity:
    def __init__(self, state: State, *, public_url: str, redirect_uri: str) -> None:
        self._state = state
        self._public_url = public_url.rstrip("/")
        self._redirect_uri = redirect_uri
        self.sign_ups_open = True

    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        query = urlencode(
            {
                "state": state,
                "code_challenge": code_challenge,
                "nonce": nonce,
                "redirect_uri": self._redirect_uri,
            }
        )
        return f"{self._public_url}/login/oauth/authorize?{query}"

    def sign_up_url(self) -> str | None:
        return f"{self._public_url}/user/sign_up" if self.sign_ups_open else None

    def consent_redirect(self, sign_in_url: str) -> str:
        """Follow the sign-in URL the way a browser would: the signed-in user
        approves and the forge sends the browser back with a code.
        """
        query = parse_qs(urlsplit(sign_in_url).query)
        code = self._authorize(query["code_challenge"][0], query["nonce"][0])
        return f"{self._redirect_uri}?{urlencode({'code': code, 'state': query['state'][0]})}"

    async def complete_sign_in(self, *, code: str, verifier: str) -> SignedIn:
        self._state.record("complete_sign_in", PLATFORM, code=code)
        self._state.check_up()
        entry = self._state.codes.pop(code, None)
        if entry is None:
            raise Forbidden("the code is not accepted")
        user_id, challenge, nonce = entry
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        if base64.urlsafe_b64encode(digest).decode().rstrip("=") != challenge:
            raise Forbidden("the verifier does not match")
        return SignedIn(
            user=self._state.user(user_id), credential=self._state.mint(user_id), nonce=nonce
        )

    async def refresh_credential(self, credential: Credential) -> Credential:
        self._state.record("refresh_credential", PLATFORM)
        self._state.check_up()
        if self._state.refuse_refresh:
            raise Forbidden("the forge will not renew this credential")
        user_id = self._state.refresh_tokens.pop(credential.refresh, None)
        if user_id is None:
            raise Forbidden("unknown refresh token")
        self._state.credentials.pop(credential.access, None)
        self._state.refreshes += 1
        return self._state.mint(user_id)

    async def user_of(self, credential: Credential) -> User:
        self._state.record("user_of", PLATFORM)
        self._state.check_up()
        user_id = self._state.credentials.get(credential.access)
        if user_id is None:
            raise Forbidden("unknown credential")
        return self._state.user(user_id)

    async def find_user(self, user_id: int) -> User:
        self._state.record("find_user", PLATFORM, user_id=user_id)
        self._state.check_up()
        return self._state.user(user_id)

    async def find_user_by_username(self, username: str) -> User:
        self._state.record("find_user_by_username", PLATFORM, username=username)
        self._state.check_up()
        return self._state.user_named(username)

    async def verified_emails(self, user_id: int) -> tuple[str, ...]:
        """The user's address, unless a test marked it unconfirmed."""
        self._state.record("verified_emails", PLATFORM, user_id=user_id)
        self._state.check_up()
        email = self._state.user(user_id).email
        return (email,) if email and email not in self._state.unverified else ()

    async def create_user(
        self,
        username: str,
        email: str,
        password: str,
        *,
        must_change_password: bool,
        visibility: AccountVisibility = "public",
    ) -> User:
        self._state.record(
            "create_user",
            PLATFORM,
            username=username,
            email=email,
            must_change_password=must_change_password,
            visibility=visibility,
        )
        self._state.check_up()
        if any(user.username.lower() == username.lower() for user in self._state.users.values()):
            raise Conflict(f"user already exists [name: {username}]")
        user = User(id=self._state.new_user_id(), username=username, email=email)
        self._state.users[user.id] = user
        self._state.passwords[user.id] = password
        return user

    async def set_password(self, user_id: int, password: str) -> None:
        self._state.record("set_password", PLATFORM, user_id=user_id)
        self._state.check_up()
        self._state.user(user_id)
        self._state.passwords[user_id] = password

    async def mint_token(
        self, username: str, password: str, *, name: str, scopes: Sequence[str]
    ) -> str:
        self._state.record(
            "mint_token", PLATFORM, username=username, name=name, scopes=list(scopes)
        )
        self._state.check_up()
        user = self._state.user_named(username)
        if self._state.passwords.get(user.id) != password:
            raise Forbidden(f"the forge did not accept the password of {username}")
        token = secrets.token_urlsafe(16)
        self._state.tokens[token] = user.id
        return token

    async def deactivate_user(self, user_id: int) -> None:
        self._state.record("deactivate_user", PLATFORM, user_id=user_id)
        self._state.check_up()
        user = self._state.user(user_id)
        self._state.users[user_id] = User(
            id=user.id,
            username=user.username,
            name=user.name,
            email=user.email,
            avatar_url=user.avatar_url,
            active=False,
        )
        self._state.revoke_credentials(user_id)

    async def delete_user(self, user_id: int) -> None:
        self._state.record("delete_user", PLATFORM, user_id=user_id)
        self._state.check_up()
        username = self._state.username(user_id)
        del self._state.users[user_id]
        self._state.passwords.pop(user_id, None)
        self._state.revoke_credentials(user_id)
        for org in self._state.orgs.values():
            for members in org.roles.values():
                members.discard(user_id)
        for key in [key for key, repo in self._state.repos.items() if repo.owner == username]:
            del self._state.repos[key]
        for thread_id, thread in self._state.threads.items():
            self._state.threads[thread_id] = replace(
                thread,
                author_id=None if thread.author_id == user_id else thread.author_id,
                comments=tuple(
                    replace(comment, author_id=None) if comment.author_id == user_id else comment
                    for comment in thread.comments
                ),
            )

    def _authorize(self, code_challenge: str, nonce: str) -> str:
        code = secrets.token_urlsafe(16)
        self._state.codes[code] = (self._state.signed_in_user_id, code_challenge, nonce)
        return code
