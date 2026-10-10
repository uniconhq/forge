"""What the live tests share: the running Forgejo and Woodpecker they are
pointed at, the platform account's own client, a `ForgejoForge` over them,
and the people a test makes and removes. `UNICON_LIVE_FORGE_URL` and
`UNICON_LIVE_FORGE_ADMIN_TOKEN` name the forge; a test that needs the CI
also needs `UNICON_LIVE_CI_URL`, `UNICON_LIVE_CI_PUBLIC_URL`,
`UNICON_LIVE_CI_ADMIN_TOKEN` and `UNICON_LIVE_FORGE_PUBLIC_URL`. `ci` is the
CI administrator's own client and `live_setup` a setup over both and the
test Postgres. Every name a test makes carries a random suffix.
"""

import os
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from forge.adapters import JoinedForge
from forge.adapters.git.forgejo import ForgejoConfig, ForgejoForge
from forge.adapters.mail.smtp import NoMail
from forge.adapters.objects.s3 import NoStore
from forge.domain.identity import AsUser, Credential
from forge.domain.keys import key_from_name
from forge.port import ObjectStore
from forge.runtime.setup import Setup
from forge.settings import Settings
from forge.testing import APP_URL, CALLBACK_PATH

URL = os.environ.get("UNICON_LIVE_FORGE_URL")
ADMIN_TOKEN = os.environ.get("UNICON_LIVE_FORGE_ADMIN_TOKEN")
FORGE_PUBLIC_URL = os.environ.get("UNICON_LIVE_FORGE_PUBLIC_URL")
CI_URL = os.environ.get("UNICON_LIVE_CI_URL")
CI_PUBLIC_URL = os.environ.get("UNICON_LIVE_CI_PUBLIC_URL")
CI_ADMIN_TOKEN = os.environ.get("UNICON_LIVE_CI_ADMIN_TOKEN")
HAS_CI = bool(FORGE_PUBLIC_URL and CI_URL and CI_PUBLIC_URL and CI_ADMIN_TOKEN)
PASSWORD = "live-password-123"

LIVE = [
    pytest.mark.live,
    pytest.mark.skipif(not URL or not ADMIN_TOKEN, reason="no live forge configured"),
]
needs_ci = pytest.mark.skipif(not HAS_CI, reason="no live CI configured")


@pytest.fixture(scope="module")
def stamp() -> str:
    return secrets.token_hex(3)


@pytest.fixture(scope="module")
def admin() -> httpx.Client:
    assert URL and ADMIN_TOKEN
    return httpx.Client(
        base_url=URL.rstrip("/"), headers={"Authorization": f"token {ADMIN_TOKEN}"}, timeout=30
    )


def forge_config(admin: httpx.Client) -> ForgejoConfig:
    assert URL and ADMIN_TOKEN
    return ForgejoConfig(
        public_url=FORGE_PUBLIC_URL or URL,
        internal_url=URL,
        admin_token=ADMIN_TOKEN,
        platform_account=platform_account(admin),
        oauth_client_id="unused",
        oauth_client_secret="unused",
        sign_in_redirect_uri="http://unused/callback",
        sign_ups_open=True,
        ci_url=CI_URL or "http://unused",
        ci_public_url=CI_PUBLIC_URL or "http://unused",
        ci_admin_token=CI_ADMIN_TOKEN or "unused",
    )


def platform_account(admin: httpx.Client) -> str:
    return str(admin.get("/api/v1/user").json()["login"])


@pytest.fixture
def ci() -> Iterator[httpx.Client]:
    """The CI's administrator's own client."""
    assert CI_URL and CI_ADMIN_TOKEN
    with httpx.Client(
        base_url=CI_URL.rstrip("/"),
        headers={"Authorization": f"Bearer {CI_ADMIN_TOKEN}"},
        timeout=30,
    ) as client:
        yield client


@pytest.fixture
async def live_setup(migrated_database_url: str, admin: httpx.Client) -> AsyncIterator[Setup]:
    """A setup over the live forge and CI and the test Postgres, with the
    platform reached at the backend's name inside the deployment.
    """
    settings = Settings.for_tests(
        database_url=migrated_database_url,
        public_url=APP_URL,
        forge_public_url=FORGE_PUBLIC_URL,
        internal_url="http://backend:8000",
    )
    built = Setup.build(
        settings,
        callback_path=CALLBACK_PATH,
        forge=live_forge(forge_config(admin)),
        keys=key_from_name,
    )
    try:
        yield built
    finally:
        await built.stop()


def live_forge(config: ForgejoConfig, objects: ObjectStore | None = None) -> JoinedForge:
    """The live forge and CI joined as the runtime joins them, with no
    object store or mail server unless `objects` is one.
    """
    git = ForgejoForge(config)
    return JoinedForge(
        git=git,
        ci=git,
        objects=objects if objects is not None else NoStore(),
        mail=NoMail(),
    )


@pytest.fixture
async def forge(admin: httpx.Client) -> AsyncIterator[ForgejoForge]:
    built = ForgejoForge(forge_config(admin))
    try:
        yield built
    finally:
        await built.aclose()


def make_user(admin: httpx.Client, username: str) -> dict[str, Any]:
    """A person at the forge with the shared test password."""
    created = admin.post(
        "/api/v1/admin/users",
        json={
            "username": username,
            "email": f"{username}@unicon.invalid",
            "password": PASSWORD,
            "must_change_password": False,
        },
    )
    assert created.status_code == 201, created.text
    person: dict[str, Any] = created.json()
    return person


def delete_user(admin: httpx.Client, username: str) -> None:
    admin.delete(f"/api/v1/admin/users/{username}", params={"purge": "true"})


def credential_of(admin: httpx.Client, username: str) -> Credential:
    """A personal token standing in for the credential a sign-in yields:
    Forgejo takes either as a bearer.
    """
    minted = httpx.post(
        f"{admin.base_url}/api/v1/users/{username}/tokens",
        auth=(username, PASSWORD),
        json={"name": f"live-{secrets.token_hex(3)}", "scopes": ["all"]},
        timeout=30,
    )
    assert minted.status_code == 201, minted.text
    return Credential(
        access=str(minted.json()["sha1"]),
        refresh="",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def as_person(admin: httpx.Client, person: dict[str, Any]) -> AsUser:
    return AsUser(int(person["id"]), credential_of(admin, str(person["login"])))


def delete_org(admin: httpx.Client, org: str) -> None:
    """The org and every repository in it, when there is one."""
    found = admin.get(f"/api/v1/orgs/{org}/repos", params={"limit": 50})
    for repo in found.json() if found.status_code == 200 else []:
        admin.delete(f"/api/v1/repos/{org}/{repo['name']}")
    admin.delete(f"/api/v1/orgs/{org}")
