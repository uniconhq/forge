"""Configuration is read once at start, and a mistake names the variable."""

import pytest

from forge.settings import Settings, load_settings

COMPLETE = {
    "UNICON_PUBLIC_URL": "http://localhost:8080",
    "UNICON_DATABASE_URL": "postgresql+psycopg://unicon:pw@postgres:5432/unicon",
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
    "UNICON_SESSION_SIGNING_KEY": "dW5pY29uIHRlc3Qgc2Vzc2lvbiBzaWduaW5nIGtleS4",
    "UNICON_TOKEN_ENCRYPTION_KEY": "dW5pY29uIHRlc3QgdG9rZW4gZW5jcnlwdGlvbiBrZXk",
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
    assert settings.forge_admin_token.get_secret_value() == "admin-token"


def test_a_missing_variable_stops_the_process_naming_it(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("UNICON_FORGE_PUBLIC_URL")

    with pytest.raises(SystemExit) as stopped:
        load_settings()

    assert stopped.value.code == 2
    lines = capsys.readouterr().err
    assert "UNICON_FORGE_PUBLIC_URL: Field required" in lines
    assert "UNICON_FORGE_ADMIN_TOKEN" not in lines


def test_a_secret_never_prints_itself() -> None:
    settings = Settings.for_tests(forge_admin_token="gho_live")
    assert "gho_live" not in repr(settings)
    assert "gho_live" not in str(settings.forge_admin_token)


def test_a_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValueError, match="expected 32"):
        Settings.for_tests(session_signing_key="c2hvcnQ")
