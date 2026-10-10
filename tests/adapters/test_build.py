"""`build` hands each implementation what the settings say of the CI: how
long the org account's login lasts, the session's hard lifetime, which is
when its sign-in at the CI is renewed, and which CI it grades with.
"""

from datetime import timedelta

import pytest

from forge import adapters
from forge.adapters.ci.woodpecker import WoodpeckerCi
from forge.adapters.fakes import FakeForge
from forge.adapters.git.forgejo import ForgejoForge
from forge.adapters.mail.smtp import NoMail
from forge.adapters.objects.s3 import S3Objects
from forge.domain.errors import Misconfigured
from forge.settings import ForgejoSettings, S3Settings, Settings, WoodpeckerSettings

SHORTER = timedelta(days=6)
IDLE = timedelta(days=2)
IMAGE = "ghcr.io/uniconhq/{}@sha256:" + "1" * 64


def test_the_fakes_ci_login_lasts_the_sessions_hard_lifetime() -> None:
    forge = adapters.build(
        Settings.for_tests(session_hard_ttl=SHORTER, session_idle_ttl=IDLE),
        sign_in_redirect_uri="http://app.test/cb",
    )

    assert isinstance(forge.grading, type(FakeForge().grading))
    assert forge.grading.login_lifetime == SHORTER


def _forgejo_settings() -> Settings:
    return Settings.for_tests(
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
        ),
        woodpecker=WoodpeckerSettings(
            url="http://woodpecker:8000", public_url="http://ci.test", token="ci-admin"
        ),
        s3=S3Settings(
            endpoint="http://garage:3900",
            access_key="key",
            secret_key="secret",
        ),
    )


async def test_forgejo_is_joined_with_the_ci_the_settings_pick_over_its_ci_host() -> None:
    """Woodpecker is handed Forgejo's `CiHost` and the session's hard
    lifetime, and the store and the mail server are built from their own
    settings.
    """
    forge = adapters.build(_forgejo_settings(), sign_in_redirect_uri="http://app.test/cb")
    try:
        joined = forge._inner  # type: ignore[attr-defined]
        assert isinstance(joined, adapters.JoinedForge)
        assert isinstance(joined.git, ForgejoForge)
        assert isinstance(joined.ci, WoodpeckerCi)
        assert joined.ci.grading._host is joined.git.ci_host
        assert joined.ci.grading._login_lifetime == SHORTER
        assert forge.grading is joined.ci.grading
        assert isinstance(forge.objects, S3Objects)
        assert isinstance(forge.mail, NoMail)
    finally:
        await forge.aclose()


def test_a_ci_the_platform_does_not_grade_with_is_misconfigured() -> None:
    settings = _forgejo_settings().model_copy(update={"ci": "jenkins"})

    with pytest.raises(Misconfigured, match="UNICON_CI=jenkins"):
        adapters.build(settings, sign_in_redirect_uri="http://app.test/cb")
