"""What a CI needs from the git host in memory (`adapters.ci.host.CiHost`).
Its sign-in takes no pages: the password is checked against the account's
as the git host's sign-in form would check it, and there is no consent to
approve.
"""

import httpx

from forge.adapters.browser import Browser
from forge.adapters.git.fake.state import State
from forge.domain.errors import Forbidden
from forge.domain.identity import User

DEFAULT_BRANCH = "main"


class FakeCiHost:
    def __init__(self, state: State, *, public_url: str) -> None:
        self._state = state
        self._public_url = public_url.rstrip("/")

    @property
    def web_address(self) -> tuple[str, str]:
        return (self._public_url, self._public_url)

    async def sign_in(self, browser: Browser, username: str, password: str) -> None:
        user = self._state.user_named(username)
        if self._state.passwords.get(user.id) != password:
            raise Forbidden(f"the forge did not accept the sign-in as {username}")

    async def approve_consent(self, browser: Browser, location: str, page: httpx.Response) -> str:
        return location

    @property
    def default_branch(self) -> str:
        return DEFAULT_BRANCH

    async def account(self, account_id: int) -> User:
        return self._state.user(account_id)

    async def set_password(self, account_id: int, password: str) -> None:
        self._state.passwords[self._state.user(account_id).id] = password

    async def repo_id(self, owner: str, repo: str) -> int:
        return self._state.repo(owner, repo).id

    async def remove_hooks(self, owner: str, repo: str, url_prefix: str) -> None:
        return None
