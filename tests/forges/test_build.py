"""`build` hands each implementation what the settings say of the CI: how
long the org account's login lasts, the session's hard lifetime, which is
when its sign-in at the CI is renewed, and which CI it grades with.
"""

from datetime import timedelta
from typing import Any

import pytest

from forge import forges
from forge.forges.fake import FakeForge
from forge.settings import ForgejoSettings, S3Settings, Settings

SHORTER = timedelta(days=6)
IDLE = timedelta(days=2)
IMAGE = "ghcr.io/uniconhq/{}@sha256:" + "1" * 64


def test_the_fakes_ci_login_lasts_the_sessions_hard_lifetime() -> None:
    forge = forges.build(
        Settings.for_tests(session_hard_ttl=SHORTER, session_idle_ttl=IDLE),
        sign_in_redirect_uri="http://app.test/cb",
    )

    assert isinstance(forge.grading, type(FakeForge().grading))
    assert forge.grading.login_lifetime == SHORTER


def test_forgejo_is_given_the_sessions_hard_lifetime_and_the_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Any] = []

    class Recorded(FakeForge):
        def __init__(self, config: Any) -> None:
            built.append(config)
            super().__init__()

    monkeypatch.setattr(forges, "ForgejoForge", Recorded)
    settings = Settings.for_tests(
        forge="forgejo",
        forge_public_url="http://forge.test",
        session_hard_ttl=SHORTER,
        session_idle_ttl=IDLE,
        harness_image=IMAGE.format("harness"),
        clone_image=IMAGE.format("clone"),
        forgejo=ForgejoSettings(
            internal_url="http://forgejo:3000",
            admin_token="admin",
            oauth_client_id="client",
            oauth_client_secret="secret",
            platform_account="platform",
            woodpecker_url="http://woodpecker:8000",
            woodpecker_public_url="http://ci.test",
            woodpecker_token="ci-admin",
        ),
        s3=S3Settings(
            endpoint="http://garage:3900",
            access_key="key",
            secret_key="secret",
        ),
    )

    forges.build(settings, sign_in_redirect_uri="http://app.test/cb")

    [config] = built
    assert (config.ci_login_lifetime, config.ci) == (SHORTER, "woodpecker")
