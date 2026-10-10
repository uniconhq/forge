"""What a CI needs from the git host it is paired with, and nothing more.
`adapters.build` hands the git host's answer to the CI it picks. Each is a
`Protocol`, so a git host meets it without importing anything from here.

`WebSignIn` is the part a CI that admits only people who signed in through
the git host needs: a CI that keeps its own accounts uses none of it. Each
git host has its own sign-in pages, so each writes its own, once, and every
CI that signs people in through it uses that.
"""

from typing import Protocol

import httpx

from forge.adapters.browser import Browser
from forge.domain.identity import User


class WebSignIn(Protocol):
    @property
    def web_address(self) -> tuple[str, str]:
        """The git host's public address, which its pages and redirects are
        under, and the internal one the platform reaches it at.
        """
        ...

    async def sign_in(self, browser: Browser, username: str, password: str) -> None:
        """Sign `browser` in as `username` through the git host's own sign-in
        page. `Forbidden` when the git host refuses the password.
        """
        ...

    async def approve_consent(self, browser: Browser, location: str, page: httpx.Response) -> str:
        """Approve the consent page `page`, found at `location`, and return
        where the git host sends the browser next. `Rejected` when it is not
        the consent page.
        """
        ...


class CiHost(WebSignIn, Protocol):
    @property
    def default_branch(self) -> str:
        """The branch every repository the platform makes has, which a run
        is started on.
        """
        ...

    async def account(self, account_id: int) -> User:
        """The account with that id at the git host. `NotFound` when there is
        none.
        """
        ...

    async def set_password(self, account_id: int, password: str) -> None:
        """Give the account with that id a new password at the git host."""
        ...

    async def repo_id(self, owner: str, repo: str) -> int:
        """The git host's own id of the repository `owner/repo`."""
        ...

    async def remove_hooks(self, owner: str, repo: str, url_prefix: str) -> None:
        """Delete the repository's webhooks whose address is under
        `url_prefix`, the ones the CI put there.
        """
        ...
