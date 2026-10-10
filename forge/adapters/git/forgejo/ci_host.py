"""What a CI needs from Forgejo (`adapters.ci.host.CiHost`): the service
account by its id and a fresh password for it, a repository's id, the
webhooks a CI left on a repository, the branch runs start on, and Forgejo's
two sign-in pages for a CI that admits only people who signed in here.
"""

from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from forge.adapters.browser import REDIRECTS, Browser
from forge.adapters.git.forgejo.http import Http, segment
from forge.adapters.git.forgejo.repos import DEFAULT_BRANCH, Repos
from forge.adapters.git.forgejo.users import Users
from forge.domain.errors import Forbidden, Rejected
from forge.domain.identity import PLATFORM, User


class ForgejoWebSignIn:
    """Forgejo's sign-in form and its consent form, as a browser fills them
    in.
    """

    def __init__(self, *, public_url: str, internal_url: str) -> None:
        self._public = public_url.rstrip("/")
        self._internal = internal_url.rstrip("/")

    @property
    def web_address(self) -> tuple[str, str]:
        return (self._public, self._internal)

    async def sign_in(self, browser: Browser, username: str, password: str) -> None:
        """The way the browser form does it. Forgejo 15 puts no CSRF field on
        this form; whatever hidden field is there is sent back anyway.
        """
        page = await browser.request("GET", f"{self._public}/user/login")
        fields = hidden_inputs(page.text)
        fields.update({"user_name": username, "password": password})
        response = await browser.request(
            "POST",
            f"{self._public}/user/login",
            data=fields,
            headers={"Referer": browser.rewrite(f"{self._public}/user/login")},
        )
        if response.status_code not in REDIRECTS:
            raise Forbidden(f"the forge did not accept the sign-in as {username}")

    async def approve_consent(self, browser: Browser, location: str, page: httpx.Response) -> str:
        """Post the consent form and return where Forgejo sends the browser
        next. The approve control is a button, not an input, so `granted` is
        added by hand; without it Forgejo reads the post as a refusal.
        """
        fields = hidden_inputs(page.text)
        if "client_id" not in fields:
            raise Rejected(f"{location} is not the forge's consent page")
        fields["granted"] = "true"
        response = await browser.request(
            "POST",
            f"{self._public}/login/oauth/grant",
            data=fields,
            headers={"Referer": browser.rewrite(location)},
        )
        if response.status_code not in REDIRECTS:
            raise Rejected(f"consent answered {response.status_code} without a redirect")
        return urljoin(location, response.headers["location"])


class ForgejoCiHost(ForgejoWebSignIn):
    def __init__(
        self, http: Http, users: Users, repos: Repos, *, public_url: str, internal_url: str
    ) -> None:
        super().__init__(public_url=public_url, internal_url=internal_url)
        self._http = http
        self._users = users
        self._repos = repos

    @property
    def default_branch(self) -> str:
        return DEFAULT_BRANCH

    async def account(self, account_id: int) -> User:
        return await self._users.find(account_id)

    async def set_password(self, account_id: int, password: str) -> None:
        await self._users.set_password(account_id, password)

    async def repo_id(self, owner: str, repo: str) -> int:
        return int((await self._repos.record(owner, repo))["id"])

    async def remove_hooks(self, owner: str, repo: str, url_prefix: str) -> None:
        hooks = f"/api/v1/repos/{segment(owner)}/{segment(repo)}/hooks"
        for hook in await self._http.get_all(PLATFORM, hooks):
            url = str((hook.get("config") or {}).get("url", ""))
            if url.startswith(url_prefix):
                await self._http.call(PLATFORM, "DELETE", f"{hooks}/{hook['id']}")


class _HiddenInputs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        attributes = dict(attrs)
        if attributes.get("type") != "hidden":
            return
        name = attributes.get("name")
        if name:
            self.fields[name] = attributes.get("value") or ""


def hidden_inputs(html: str) -> dict[str, str]:
    """The hidden fields of the forms on a page, by name."""
    collector = _HiddenInputs()
    collector.feed(html)
    return collector.fields
