"""The identity area in memory."""

import base64
import hashlib
import secrets
from dataclasses import replace
from urllib.parse import parse_qs, urlencode, urlsplit

from forge.domain.errors import Forbidden
from forge.domain.identity import PLATFORM, Credential, User
from forge.forges.fake.state import State
from forge.port.identity import SignedIn


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
