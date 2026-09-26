"""Configuration is read once at start, a mistake names the variable, and
the Forgejo settings are required only when Forgejo is chosen.
"""

import pytest
from pydantic import ValidationError

from forge.settings import Settings, load_settings

COMPLETE = {
    "UNICON_PUBLIC_URL": "http://localhost:8080",
    "UNICON_DATABASE_URL": "postgresql+psycopg://unicon:pw@postgres:5432/unicon",
    "UNICON_TOKEN_ENCRYPTION_KEY": "dW5pY29uIHRlc3QgdG9rZW4gZW5jcnlwdGlvbiBrZXk",
    "UNICON_FORGE_PUBLIC_URL": "http://localhost:3300",
    "UNICON_FORGE_ADMIN_TOKEN": "admin-token",
    "UNICON_FORGE_OAUTH_CLIENT_ID": "client-id",
    "UNICON_FORGE_OAUTH_CLIENT_SECRET": "client-secret",
    "UNICON_WOODPECKER_URL": "http://woodpecker-server:8000",
    "UNICON_WOODPECKER_TOKEN": "woodpecker-token",
    "UNICON_S3_ENDPOINT": "http://garage:3900",
    "UNICON_S3_REGION": "garage",
    "UNICON_S3_ACCESS_KEY": "access",
    "UNICON_S3_SECRET_KEY": "secret",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [*COMPLETE, "UNICON_FORGE_INTERNAL_URL", "UNICON_FORGE", "UNICON_LOG_LEVEL"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in COMPLETE.items():
        monkeypatch.setenv(name, value)


def test_a_complete_environment_loads(environment: None) -> None:
    settings = load_settings()
    assert str(settings.forge_internal_url) == "http://localhost:3300/"
    assert settings.forge == "forgejo"
    assert settings.forge_admin_token is not None
    assert settings.forge_admin_token.get_secret_value() == "admin-token"


def test_a_missing_variable_stops_the_process_naming_it(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("UNICON_TOKEN_ENCRYPTION_KEY")

    with pytest.raises(SystemExit) as stopped:
        load_settings()

    assert stopped.value.code == 2
    lines = capsys.readouterr().err
    assert "UNICON_TOKEN_ENCRYPTION_KEY: Field required" in lines
    assert "UNICON_FORGE_ADMIN_TOKEN" not in lines


def test_forgejo_names_the_settings_it_needs(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("UNICON_FORGE_PUBLIC_URL")
    monkeypatch.delenv("UNICON_WOODPECKER_TOKEN")

    with pytest.raises(SystemExit):
        load_settings()

    lines = capsys.readouterr().err
    assert "UNICON_FORGE=forgejo needs UNICON_FORGE_PUBLIC_URL, UNICON_WOODPECKER_TOKEN" in lines


def test_the_fake_needs_no_forgejo_settings(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_FORGE", "fake")
    for name in ("UNICON_FORGE_ADMIN_TOKEN", "UNICON_WOODPECKER_URL", "UNICON_WOODPECKER_TOKEN"):
        monkeypatch.delenv(name)

    assert load_settings().forge == "fake"


def test_a_secret_never_prints_itself() -> None:
    settings = Settings.for_tests(
        token_encryption_key="dW5pY29uIHRlc3QgdG9rZW4gZW5jcnlwdGlvbiBrZXk"
    )
    assert "dG9rZW4" not in repr(settings)
    assert "dG9rZW4" not in str(settings.token_encryption_key)


def test_a_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValidationError, match="expected 32"):
        Settings.for_tests(token_encryption_key="c2hvcnQ")
