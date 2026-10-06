"""Configuration is read once at start, a mistake names the variable, the
Forgejo, object store and image settings are required only when Forgejo is
chosen, and cookies are secure whenever the platform is served over https.
"""

import pytest
from pydantic import ValidationError

from forge.settings import FAKE_IMAGES, Settings, load_log_settings, load_settings

COMPLETE = {
    "UNICON_PUBLIC_URL": "http://localhost:8080",
    "UNICON_DATABASE_URL": "postgresql+psycopg://unicon:pw@postgres:5432/unicon",
    "UNICON_TOKEN_ENCRYPTION_KEY": "dW5pY29uIHRlc3QgdG9rZW4gZW5jcnlwdGlvbiBrZXk",
    "UNICON_SESSION_SIGNING_KEY": "dW5pY29uIHRlc3Qgc2Vzc2lvbiBzaWduaW5nIGtleSE",
    "UNICON_FORGE_PUBLIC_URL": "http://localhost:3300",
    "UNICON_FORGE_ADMIN_TOKEN": "admin-token",
    "UNICON_FORGE_OAUTH_CLIENT_ID": "client-id",
    "UNICON_FORGE_OAUTH_CLIENT_SECRET": "client-secret",
    "UNICON_FORGE_PLATFORM_ACCOUNT": "unicon-backend",
    "UNICON_WOODPECKER_URL": "http://woodpecker-server:8000",
    "UNICON_WOODPECKER_TOKEN": "woodpecker-token",
    "UNICON_S3_ENDPOINT": "http://garage:3900",
    "UNICON_S3_ACCESS_KEY": "GK-test",
    "UNICON_S3_SECRET_KEY": "s3-secret",
    "UNICON_HARNESS_IMAGE": "ghcr.io/uniconhq/harness@sha256:" + "1" * 64,
    "UNICON_CLONE_IMAGE": "ghcr.io/uniconhq/clone@sha256:" + "2" * 64,
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [
        *COMPLETE,
        "UNICON_FORGE_INTERNAL_URL",
        "UNICON_FORGE_REGISTRATION_OPEN",
        "UNICON_WOODPECKER_PUBLIC_URL",
        "UNICON_FORGE",
        "UNICON_LOG_LEVEL",
        "UNICON_COOKIE_SECURE",
        "UNICON_INTERNAL_URL",
        "UNICON_ORG_CREATION_OPEN",
        "UNICON_MACHINE_URL",
        "UNICON_S3_REGION",
        "UNICON_S3_RESULTS_BUCKET",
    ]:
        monkeypatch.delenv(name, raising=False)
    for name, value in COMPLETE.items():
        monkeypatch.setenv(name, value)


def test_a_complete_environment_loads(environment: None) -> None:
    settings = load_settings()
    assert settings.forge == "forgejo"
    assert settings.forgejo is not None
    assert str(settings.forgejo.internal_url) == "http://localhost:3300/"
    assert settings.forgejo.admin_token.get_secret_value() == "admin-token"
    assert settings.forgejo.platform_account == "unicon-backend"
    assert settings.forgejo.registration_open is False


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


def test_forgejo_needs_the_platform_account(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("UNICON_FORGE_PLATFORM_ACCOUNT")

    with pytest.raises(SystemExit):
        load_settings()

    assert "UNICON_FORGE=forgejo needs UNICON_FORGE_PLATFORM_ACCOUNT" in capsys.readouterr().err


def test_a_malformed_forgejo_setting_is_named_by_its_variable(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UNICON_WOODPECKER_URL", "not a url")

    with pytest.raises(SystemExit):
        load_settings()

    assert "UNICON_WOODPECKER_URL: " in capsys.readouterr().err


def test_the_fake_needs_no_forgejo_settings(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_FORGE", "fake")
    for name in ("UNICON_FORGE_ADMIN_TOKEN", "UNICON_WOODPECKER_URL", "UNICON_WOODPECKER_TOKEN"):
        monkeypatch.delenv(name)

    settings = load_settings()
    assert settings.forge == "fake"
    assert settings.forgejo is None


def test_a_secret_never_prints_itself() -> None:
    settings = Settings.for_tests(
        token_encryption_key="dW5pY29uIHRlc3QgdG9rZW4gZW5jcnlwdGlvbiBrZXk"
    )
    assert "dG9rZW4" not in repr(settings)
    assert "dG9rZW4" not in str(settings.token_encryption_key)


def test_a_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValidationError, match="expected 32"):
        Settings.for_tests(token_encryption_key="c2hvcnQ")


def test_the_ci_public_url_follows_the_ci_url_unless_given(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    forgejo = load_settings().forgejo
    assert forgejo is not None
    assert str(forgejo.woodpecker_public_url) == "http://woodpecker-server:8000/"

    monkeypatch.setenv("UNICON_WOODPECKER_PUBLIC_URL", "http://ci.example.test")
    forgejo = load_settings().forgejo
    assert forgejo is not None
    assert str(forgejo.woodpecker_public_url) == "http://ci.example.test/"


def test_cookies_are_secure_by_default_exactly_when_the_public_url_is_https() -> None:
    assert Settings.for_tests(public_url="http://localhost:8080").secure_cookies is False
    assert Settings.for_tests(public_url="https://unicon.example.test").secure_cookies is True
    assert Settings.for_tests(cookie_secure=True).secure_cookies is True


def test_insecure_cookies_behind_https_stop_the_process_naming_the_variable(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UNICON_PUBLIC_URL", "https://unicon.example.test")
    monkeypatch.setenv("UNICON_COOKIE_SECURE", "false")

    with pytest.raises(SystemExit):
        load_settings()

    assert "UNICON_COOKIE_SECURE is off but UNICON_PUBLIC_URL is https" in capsys.readouterr().err


def test_a_signing_key_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValidationError, match="expected 32"):
        Settings.for_tests(session_signing_key="c2hvcnQ")


def test_the_log_level_is_read_on_its_own_and_a_wrong_one_is_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UNICON_LOG_LEVEL", "debug")
    assert load_log_settings().log_level == "DEBUG"

    monkeypatch.setenv("UNICON_LOG_LEVEL", "chatty")
    with pytest.raises(SystemExit):
        load_log_settings()

    assert "UNICON_LOG_LEVEL: Value error, is not a logging level: chatty" in (
        capsys.readouterr().err
    )


def test_org_creation_is_open_unless_the_variable_closes_it(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_settings().org_creation_open is True
    assert Settings.for_tests().org_creation_open is True

    monkeypatch.setenv("UNICON_ORG_CREATION_OPEN", "false")
    assert load_settings().org_creation_open is False


def test_the_internal_url_follows_the_public_url_unless_given(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert str(load_settings().internal_url) == "http://localhost:8080/"
    assert str(Settings.for_tests().internal_url) == str(Settings.for_tests().public_url)

    monkeypatch.setenv("UNICON_INTERNAL_URL", "http://backend:8000")
    assert str(load_settings().internal_url) == "http://backend:8000/"
    assert str(Settings.for_tests(internal_url="http://backend:8000").internal_url) == (
        "http://backend:8000/"
    )


def test_the_object_store_travels_with_forgejo_and_defaults_its_region_and_buckets(
    environment: None,
) -> None:
    settings = load_settings()
    assert settings.s3 is not None
    assert str(settings.s3.endpoint) == "http://garage:3900/"
    assert settings.s3.secret_key.get_secret_value() == "s3-secret"
    assert (settings.s3.region, settings.s3.results_bucket) == ("garage", "unicon-results")


def test_forgejo_without_the_object_store_names_its_variables(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("UNICON_S3_ENDPOINT")
    monkeypatch.delenv("UNICON_S3_SECRET_KEY")

    with pytest.raises(SystemExit):
        load_settings()

    lines = capsys.readouterr().err
    assert "UNICON_FORGE=forgejo needs UNICON_S3_ENDPOINT, UNICON_S3_SECRET_KEY" in lines


def test_the_fake_needs_no_object_store(environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("UNICON_S3_ENDPOINT", "UNICON_S3_ACCESS_KEY", "UNICON_S3_SECRET_KEY"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("UNICON_FORGE", "fake")
    assert load_settings().forgejo is None


def test_the_machine_url_follows_the_public_url_unless_given(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert str(load_settings().machine_url) == "http://localhost:8080/"
    monkeypatch.setenv("UNICON_MACHINE_URL", "http://proxy")
    assert str(load_settings().machine_url) == "http://proxy/"


def test_forgejo_needs_both_images(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("UNICON_HARNESS_IMAGE")
    monkeypatch.delenv("UNICON_CLONE_IMAGE")

    with pytest.raises(SystemExit):
        load_settings()

    lines = capsys.readouterr().err
    assert "UNICON_FORGE=forgejo needs UNICON_HARNESS_IMAGE, UNICON_CLONE_IMAGE" in lines


@pytest.mark.parametrize(
    ("variable", "field", "tagged"),
    [
        ("UNICON_HARNESS_IMAGE", "harness_image", "ghcr.io/uniconhq/harness:latest"),
        ("UNICON_CLONE_IMAGE", "clone_image", "ghcr.io/uniconhq/clone:v1"),
    ],
)
def test_an_image_is_the_one_given_and_always_by_digest(
    environment: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
    field: str,
    tagged: str,
) -> None:
    local = "localhost:5000/uniconhq/image@sha256:" + "a" * 64
    monkeypatch.setenv(variable, local)
    assert getattr(load_settings(), field) == local

    monkeypatch.setenv(variable, tagged)
    with pytest.raises(SystemExit):
        load_settings()
    assert f"{variable}: Value error, is not an image by digest" in capsys.readouterr().err


def test_the_fake_names_placeholder_images_unless_given(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_FORGE", "fake")
    monkeypatch.delenv("UNICON_HARNESS_IMAGE")
    monkeypatch.delenv("UNICON_CLONE_IMAGE")

    settings = load_settings()
    assert (settings.harness_image, settings.clone_image) == (
        FAKE_IMAGES["harness_image"],
        FAKE_IMAGES["clone_image"],
    )
    assert Settings.for_tests().harness_image == FAKE_IMAGES["harness_image"]
    monkeypatch.setenv("UNICON_HARNESS_IMAGE", COMPLETE["UNICON_HARNESS_IMAGE"])
    assert load_settings().harness_image == COMPLETE["UNICON_HARNESS_IMAGE"]


def test_there_is_no_mail_server_while_its_address_is_empty(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert load_settings().mail is None
    # Compose passes every key, empty where .env leaves it so.
    monkeypatch.setenv("UNICON_MAIL_SMTP_ADDR", "")
    monkeypatch.setenv("UNICON_MAIL_SMTP_PORT", "587")
    monkeypatch.setenv("UNICON_MAIL_FROM", "")
    assert load_settings().mail is None


def test_the_mail_server_encrypts_unless_told_and_blank_credentials_are_none(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNICON_MAIL_SMTP_ADDR", "smtp.example.test")
    monkeypatch.setenv("UNICON_MAIL_FROM", "Unicon <unicon@example.test>")
    monkeypatch.setenv("UNICON_MAIL_SMTP_USER", "")
    monkeypatch.setenv("UNICON_MAIL_SMTP_PASSWORD", " ")

    mail = load_settings().mail

    assert mail is not None
    assert (mail.smtp_addr, mail.smtp_port, mail.protocol) == (
        "smtp.example.test",
        587,
        "smtp+starttls",
    )
    assert (mail.user, mail.password) == (None, None)


def test_a_mail_server_without_a_sender_is_named_by_its_variable(
    environment: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("UNICON_MAIL_SMTP_ADDR", "smtp.example.test")
    monkeypatch.setenv("UNICON_MAIL_PROTOCOL", "smtp+starttls")

    with pytest.raises(SystemExit):
        load_settings()

    assert "UNICON_MAIL_FROM: Field required" in capsys.readouterr().err
