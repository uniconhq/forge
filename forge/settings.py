"""Every `UNICON_*` setting the package reads, loaded once at start. A missing
or malformed variable stops the process with the variable named, so a bad
deployment fails at start rather than on the first request that needs the
value. Secrets are `SecretStr` and never reach a log.
"""

import base64
import binascii
import logging
import sys
from datetime import timedelta
from typing import Any, Literal, Self

from pydantic import HttpUrl, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

KEY_BYTES = 32

ForgeKind = Literal["forgejo", "fake"]

FORGEJO_FIELDS = (
    "forge_public_url",
    "forge_admin_token",
    "forge_oauth_client_id",
    "forge_oauth_client_secret",
    "woodpecker_url",
    "woodpecker_token",
)


class Settings(BaseSettings):
    """The package's configuration. `forge` picks the implementation behind
    the port; the settings of the Forgejo implementation are required only
    when it is chosen.
    """

    model_config = SettingsConfigDict(env_prefix="UNICON_", extra="ignore")

    public_url: HttpUrl
    database_url: PostgresDsn
    token_encryption_key: SecretStr

    forge: ForgeKind = "forgejo"
    forge_public_url: HttpUrl | None = None
    forge_internal_url: HttpUrl | None = None
    forge_admin_token: SecretStr | None = None
    forge_oauth_client_id: str | None = None
    forge_oauth_client_secret: SecretStr | None = None
    forge_registration_open: bool = False
    forge_cache: bool = False
    woodpecker_url: HttpUrl | None = None
    woodpecker_token: SecretStr | None = None

    s3_endpoint: HttpUrl
    s3_region: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr

    org_creation_open: bool = True
    log_level: str = "INFO"

    session_hard_ttl: timedelta = timedelta(days=30)
    session_idle_ttl: timedelta = timedelta(days=14)
    sign_in_ttl: timedelta = timedelta(minutes=10)
    fresh_sign_in_window: timedelta = timedelta(minutes=5)

    @field_validator("log_level")
    @classmethod
    def _a_logging_level(cls, value: str) -> str:
        name = value.strip().upper()
        if name not in logging.getLevelNamesMapping():
            raise ValueError(f"is not a logging level: {value}")
        return name

    @model_validator(mode="after")
    def _idle_within_hard(self) -> Self:
        if self.session_idle_ttl > self.session_hard_ttl:
            raise ValueError("UNICON_SESSION_IDLE_TTL is longer than UNICON_SESSION_HARD_TTL")
        return self

    @model_validator(mode="after")
    def _forgejo_needs_its_settings(self) -> Self:
        if self.forge != "forgejo":
            return self
        missing = [name for name in FORGEJO_FIELDS if getattr(self, name) is None]
        if missing:
            names = ", ".join(f"UNICON_{name.upper()}" for name in missing)
            raise ValueError(f"UNICON_FORGE=forgejo needs {names}")
        if self.forge_internal_url is None:
            self.forge_internal_url = self.forge_public_url
        return self

    @field_validator("*")
    @classmethod
    def _no_blank_values(cls, value: Any) -> Any:
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(text, str) and not text.strip():
            raise ValueError("is empty")
        return value

    @field_validator(
        "session_hard_ttl", "session_idle_ttl", "sign_in_ttl", "fresh_sign_in_window", mode="before"
    )
    @classmethod
    def _seconds(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return value

    @field_validator("token_encryption_key")
    @classmethod
    def _thirty_two_bytes_base64url(cls, value: SecretStr) -> SecretStr:
        return require_key(value)

    @property
    def token_encryption_key_bytes(self) -> bytes:
        return decode_key(self.token_encryption_key)

    @classmethod
    def for_tests(cls, **overrides: Any) -> Self:
        """A complete configuration against the in-memory forge that reads
        nothing from the environment.
        """
        values: dict[str, Any] = {**TEST_VALUES}
        values.update(overrides)
        return cls(**values)


class DatabaseSettings(BaseSettings):
    """The one setting a migration needs."""

    model_config = SettingsConfigDict(env_prefix="UNICON_", extra="ignore")

    database_url: PostgresDsn


TEST_KEY = base64.urlsafe_b64encode(b"\x00" * KEY_BYTES).decode().rstrip("=")

TEST_VALUES: dict[str, Any] = {
    "public_url": "http://localhost:8080",
    "database_url": "postgresql+psycopg://unicon:unicon@localhost:5432/unicon",
    "token_encryption_key": TEST_KEY,
    "forge": "fake",
    "forge_public_url": "http://localhost:3300",
    "s3_endpoint": "http://garage:3900",
    "s3_region": "garage",
    "s3_access_key": "test-access-key",
    "s3_secret_key": "test-secret-key",
}


def require_key(value: SecretStr) -> SecretStr:
    """A 32-byte key, base64url without padding."""
    raw = value.get_secret_value()
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64url") from exc
    if len(decoded) != KEY_BYTES:
        raise ValueError(f"decodes to {len(decoded)} bytes, expected {KEY_BYTES}")
    return value


def decode_key(secret: SecretStr) -> bytes:
    raw = secret.get_secret_value()
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def load_settings() -> Settings:
    """Read the environment, or exit naming every variable that is wrong."""
    return load(Settings)


def load_database_settings() -> DatabaseSettings:
    return load(DatabaseSettings)


def load[T: BaseSettings](settings_class: type[T]) -> T:
    """Read `settings_class` from the environment, or exit with every problem
    listed and no traceback.
    """
    try:
        return settings_class()
    except ValueError as exc:
        for line in _problems(exc):
            print(line, file=sys.stderr)
        raise SystemExit(2) from None


def _problems(exc: ValueError) -> list[str]:
    errors = getattr(exc, "errors", None)
    if errors is None:
        return [f"configuration error: {exc}"]
    lines = ["configuration error:"]
    for error in errors():
        if not error["loc"]:
            lines.append(f"  {error['msg'].removeprefix('Value error, ')}")
            continue
        location = str(error["loc"][0])
        name = location if location.startswith("UNICON_") else f"UNICON_{location.upper()}"
        lines.append(f"  {name}: {error['msg']}")
    return lines
