"""Every `UNICON_*` setting the package reads, loaded once at start. A missing
or malformed variable stops the process with the variable named, so a bad
deployment fails at start rather than on the first request that needs the
value. Secrets are `SecretStr` and never reach a log.
"""

import base64
import binascii
import logging
import os
import sys
from datetime import timedelta
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    HttpUrl,
    NonNegativeInt,
    PositiveInt,
    PostgresDsn,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from forge.domain.grading import CLONE_IMAGE
from forge.domain.plans import HARNESS_IMAGE
from forge.domain.primitives import IMAGE

KEY_BYTES = 32

ForgeKind = Literal["forgejo", "fake"]

FORGE_PUBLIC_URL = "UNICON_FORGE_PUBLIC_URL"

FORGEJO_VARIABLES = {
    "internal_url": "UNICON_FORGE_INTERNAL_URL",
    "admin_token": "UNICON_FORGE_ADMIN_TOKEN",
    "oauth_client_id": "UNICON_FORGE_OAUTH_CLIENT_ID",
    "oauth_client_secret": "UNICON_FORGE_OAUTH_CLIENT_SECRET",
    "registration_open": "UNICON_FORGE_REGISTRATION_OPEN",
    "platform_account": "UNICON_FORGE_PLATFORM_ACCOUNT",
    "woodpecker_url": "UNICON_WOODPECKER_URL",
    "woodpecker_public_url": "UNICON_WOODPECKER_PUBLIC_URL",
    "woodpecker_token": "UNICON_WOODPECKER_TOKEN",
}

DEFAULTED_FORGEJO_FIELDS = ("internal_url", "woodpecker_public_url")

S3_VARIABLES = {
    "endpoint": "UNICON_S3_ENDPOINT",
    "region": "UNICON_S3_REGION",
    "access_key": "UNICON_S3_ACCESS_KEY",
    "secret_key": "UNICON_S3_SECRET_KEY",
    "results_bucket": "UNICON_S3_RESULTS_BUCKET",
}

MAIL_VARIABLES = {
    "smtp_addr": "UNICON_MAIL_SMTP_ADDR",
    "smtp_port": "UNICON_MAIL_SMTP_PORT",
    "protocol": "UNICON_MAIL_PROTOCOL",
    "user": "UNICON_MAIL_SMTP_USER",
    "password": "UNICON_MAIL_SMTP_PASSWORD",
    "sender": "UNICON_MAIL_FROM",
}

MailProtocol = Literal["smtp", "smtps", "smtp+starttls"]


class ForgejoSettings(BaseModel):
    """The settings of the Forgejo implementation, each read from the variable
    `FORGEJO_VARIABLES` names. `internal_url` is where the package reaches
    Forgejo, the public URL unless given. `platform_account` is the Forgejo
    account the admin token belongs to, the one account protected versions
    are reserved for. `woodpecker_public_url` is the URL the CI knows itself
    by, `woodpecker_url` unless given.
    """

    model_config = ConfigDict(frozen=True)

    internal_url: HttpUrl
    admin_token: SecretStr
    oauth_client_id: str
    oauth_client_secret: SecretStr
    registration_open: bool = False
    platform_account: str
    woodpecker_url: HttpUrl
    woodpecker_public_url: HttpUrl
    woodpecker_token: SecretStr

    @model_validator(mode="before")
    @classmethod
    def _ci_public_url_follows_the_ci_url(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("woodpecker_public_url") is None:
            return {**data, "woodpecker_public_url": data.get("woodpecker_url")}
        return data

    @field_validator("*")
    @classmethod
    def _no_blank_values(cls, value: Any) -> Any:
        return not_blank(value)


class S3Settings(BaseModel):
    """Where the grading run log store is and the key the platform signs in
    to it with, each read from the variable `S3_VARIABLES` names. `endpoint`
    is the store's internal address; the region is `garage` and the bucket
    `unicon-results` unless given. What people upload goes into the forge's
    own store and is none of this (`port/uploads.py`).
    """

    model_config = ConfigDict(frozen=True)

    endpoint: HttpUrl
    region: str = "garage"
    access_key: str
    secret_key: SecretStr
    results_bucket: str = "unicon-results"

    @field_validator("*")
    @classmethod
    def _no_blank_values(cls, value: Any) -> Any:
        return not_blank(value)


class MailSettings(BaseModel):
    """The mail server invite mail goes through, the one the forge sends its
    own mail through, each read from the variable `MAIL_VARIABLES` names.
    The whole of it is absent while `UNICON_MAIL_SMTP_ADDR` is empty, and the
    platform then sends no mail. `protocol` is `smtp+starttls` unless given,
    `smtps` for a server that encrypts from the first byte, and plain `smtp`
    only for a server on the deployment's own network.
    """

    model_config = ConfigDict(frozen=True)

    smtp_addr: str
    smtp_port: PositiveInt = 587
    protocol: MailProtocol = "smtp+starttls"
    user: str | None = None
    password: SecretStr | None = None
    sender: str

    @model_validator(mode="before")
    @classmethod
    def _blank_credentials_are_none(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        found = dict(data)
        for name in ("user", "password"):
            value = found.get(name)
            if isinstance(value, str) and not value.strip():
                found[name] = None
        return found

    @field_validator("smtp_addr", "sender")
    @classmethod
    def _no_blank_values(cls, value: Any) -> Any:
        return not_blank(value)


class NestedVariables(PydanticBaseSettingsSource):
    """Reads the variables a mapping names into one nested setting, so a
    group of settings travels together and every variable keeps its name:
    the `UNICON_FORGE_*` and `UNICON_WOODPECKER_*` ones into `forgejo`, the
    `UNICON_S3_*` ones into `s3`, and the `UNICON_MAIL_*` ones into `mail`.
    """

    def __init__(
        self, settings_cls: type[BaseSettings], name: str, variables: dict[str, str]
    ) -> None:
        super().__init__(settings_cls)
        self._name = name
        self._variables = variables

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        environment = {name.upper(): value for name, value in os.environ.items()}
        found = {
            field: environment[variable]
            for field, variable in self._variables.items()
            if variable in environment
        }
        return {self._name: found} if found else {}


class Settings(BaseSettings):
    """The package's configuration. `forge` picks the implementation behind
    the port. `forgejo`, the settings of the Forgejo implementation, is
    required when it is chosen and none otherwise. `forge_public_url` is
    where browsers reach the forge, for either implementation. `internal_url`
    is where the forge reaches the platform inside the deployment, the public
    URL unless given: the org event push points there. `machine_url` is
    where grading machines reach the platform, for the envelope, the callback
    and the log they write, the public URL unless given. `org_creation_open`
    says whether any signed-in user may create an org, or only the operator.
    `places_ahead` says whether a contestant's place to submit each task is
    made once they are approved and once the task is first published, so the
    first uploads at a contest's start find them made; off, each is made at
    the contestant's first upload to the task, and nobody who never submits
    costs the forge a repository. `s3`, the object store's settings, is
    required with `forgejo`; the fake keeps its store in memory.
    `mail`, the mail server's settings, is optional, and without it nothing
    is mailed. `harness_image` is the harness every plan names, by digest, and
    `clone_image` the image the CI checks a task and a submission out with,
    by digest, each the one of the runner release the package pins unless
    given.

    `database_pool_size` connections to the database stay open in each
    process, `database_pool_overflow` more are opened in a rush, and a
    request that finds them all taken waits `database_pool_wait` and fails.
    A unit of work holds one connection and lets go of it before a slow call
    where it can, so a request keeps one for milliseconds and 30 serve a
    contest; ten seconds is long enough to ride out a rush and short enough
    that a stuck pool shows as an error rather than a page that hangs. The
    database server must have room for every process's pool at once: the
    deployment runs one backend process, Forgejo and Woodpecker share the
    server, and Postgres allows 100 connections with three kept for its
    superuser, so 30 leaves the forge, the CI, the readiness probe and an
    operator's command more than half. A process that serves live updates
    holds one connection more, outside the pool, while anyone is watching
    (`runtime/broker.py`). A deployment that runs more backend processes
    keeps their pools' sum under that.
    """

    model_config = SettingsConfigDict(env_prefix="UNICON_", extra="ignore")

    public_url: HttpUrl
    internal_url: HttpUrl
    machine_url: HttpUrl
    database_url: PostgresDsn
    database_pool_size: PositiveInt = 20
    database_pool_overflow: NonNegativeInt = 10
    database_pool_wait: timedelta = timedelta(seconds=10)
    token_encryption_key: SecretStr
    session_signing_key: SecretStr
    cookie_secure: bool | None = None
    org_creation_open: bool = True
    places_ahead: bool = True

    forge: ForgeKind = "forgejo"
    forge_public_url: HttpUrl | None = None
    forge_cache: bool = False
    forgejo: ForgejoSettings | None = None
    s3: S3Settings | None = None
    mail: MailSettings | None = None
    harness_image: str = HARNESS_IMAGE
    clone_image: str = CLONE_IMAGE

    session_hard_ttl: timedelta = timedelta(days=30)
    session_idle_ttl: timedelta = timedelta(days=14)
    sign_in_ttl: timedelta = timedelta(minutes=10)
    fresh_sign_in_window: timedelta = timedelta(minutes=5)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            NestedVariables(settings_cls, "forgejo", FORGEJO_VARIABLES),
            NestedVariables(settings_cls, "s3", S3_VARIABLES),
            NestedVariables(settings_cls, "mail", MAIL_VARIABLES),
            dotenv_settings,
            file_secret_settings,
        )

    @model_validator(mode="before")
    @classmethod
    def _internal_urls_follow_the_public_url(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "public_url" not in data:
            return data
        found = dict(data)
        for name in ("internal_url", "machine_url"):
            if found.get(name) is None:
                found[name] = data["public_url"]
        return found

    @model_validator(mode="before")
    @classmethod
    def _mail_needs_its_server(cls, data: Any) -> Any:
        if not isinstance(data, dict) or not isinstance(data.get("mail"), dict):
            return data
        server = data["mail"].get("smtp_addr")
        if server is None or (isinstance(server, str) and not server.strip()):
            return {**data, "mail": None}
        return data

    @model_validator(mode="before")
    @classmethod
    def _forgejo_needs_its_settings(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("forge", "forgejo") != "forgejo":
            return {**data, "forgejo": None}
        given = data.get("forgejo")
        if isinstance(given, ForgejoSettings):
            return data
        values = dict(given or {})
        missing = [] if data.get("forge_public_url") is not None else [FORGE_PUBLIC_URL]
        missing += [
            FORGEJO_VARIABLES[name]
            for name, field in ForgejoSettings.model_fields.items()
            if field.is_required()
            and name not in DEFAULTED_FORGEJO_FIELDS
            and values.get(name) is None
        ]
        storage = data.get("s3")
        if not isinstance(storage, S3Settings):
            given_s3 = dict(storage or {})
            missing += [
                S3_VARIABLES[name]
                for name, field in S3Settings.model_fields.items()
                if field.is_required() and given_s3.get(name) is None
            ]
        if missing:
            raise ValueError(f"UNICON_FORGE=forgejo needs {', '.join(missing)}")
        if values.get("internal_url") is None:
            values["internal_url"] = data["forge_public_url"]
        return {**data, "forgejo": values}

    @field_validator("harness_image", "clone_image")
    @classmethod
    def _an_image_by_digest(cls, value: str) -> str:
        if not IMAGE.match(value):
            raise ValueError(
                "is not an image by digest, such as ghcr.io/uniconhq/harness@sha256:..."
            )
        return value

    @model_validator(mode="after")
    def _idle_within_hard(self) -> Self:
        if self.session_idle_ttl > self.session_hard_ttl:
            raise ValueError("UNICON_SESSION_IDLE_TTL is longer than UNICON_SESSION_HARD_TTL")
        return self

    @field_validator("*")
    @classmethod
    def _no_blank_values(cls, value: Any) -> Any:
        return not_blank(value)

    @field_validator(
        "session_hard_ttl",
        "session_idle_ttl",
        "sign_in_ttl",
        "fresh_sign_in_window",
        "database_pool_wait",
        mode="before",
    )
    @classmethod
    def _seconds(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
        return value

    @model_validator(mode="after")
    def _secure_cookies_over_https(self) -> Self:
        if self.cookie_secure is False and self.public_url.scheme == "https":
            raise ValueError("UNICON_COOKIE_SECURE is off but UNICON_PUBLIC_URL is https")
        return self

    @field_validator("token_encryption_key", "session_signing_key")
    @classmethod
    def _thirty_two_bytes_base64url(cls, value: SecretStr) -> SecretStr:
        return require_key(value)

    @property
    def token_encryption_key_bytes(self) -> bytes:
        return decode_key(self.token_encryption_key)

    @property
    def session_signing_key_bytes(self) -> bytes:
        return decode_key(self.session_signing_key)

    @property
    def secure_cookies(self) -> bool:
        """Whether the cookies the hosting process sets are marked `Secure`:
        `UNICON_COOKIE_SECURE` when given, and otherwise on exactly when the
        public URL is https.
        """
        if self.cookie_secure is None:
            return self.public_url.scheme == "https"
        return self.cookie_secure

    @classmethod
    def for_tests(cls, **overrides: Any) -> Self:
        """A complete configuration against the in-memory forge that reads
        nothing from the environment. Places are not made ahead, since that
        work runs beside the test and would race what it checks; a test of it
        turns it on.
        """
        values: dict[str, Any] = {**TEST_VALUES}
        values.update(overrides)
        return cls(**values)


class DatabaseSettings(BaseSettings):
    """The one setting a migration needs."""

    model_config = SettingsConfigDict(env_prefix="UNICON_", extra="ignore")

    database_url: PostgresDsn


class LogSettings(BaseSettings):
    """The one setting the logger needs, read before anything else is."""

    model_config = SettingsConfigDict(env_prefix="UNICON_", extra="ignore")

    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _a_logging_level(cls, value: str) -> str:
        name = value.strip().upper()
        if name not in logging.getLevelNamesMapping():
            raise ValueError(f"is not a logging level: {value}")
        return name


TEST_KEY = base64.urlsafe_b64encode(b"\x00" * KEY_BYTES).decode().rstrip("=")

TEST_VALUES: dict[str, Any] = {
    "public_url": "http://localhost:8080",
    "database_url": "postgresql+psycopg://unicon:unicon@localhost:5432/unicon",
    "token_encryption_key": TEST_KEY,
    "session_signing_key": TEST_KEY,
    "forge": "fake",
    "forge_public_url": "http://localhost:3300",
    "places_ahead": False,
}


def not_blank(value: Any) -> Any:
    """Refuse a string, or a secret, that is empty or only whitespace."""
    text = value.get_secret_value() if isinstance(value, SecretStr) else value
    if isinstance(text, str) and not text.strip():
        raise ValueError("is empty")
    return value


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


def load_log_settings() -> LogSettings:
    return load(LogSettings)


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
        lines.append(f"  {_variable(error['loc'])}: {error['msg']}")
    return lines


def _variable(location: tuple[int | str, ...]) -> str:
    """The variable a validation error is about, from where pydantic says it
    is: a nested Forgejo setting is named by its own variable.
    """
    head = str(location[0])
    if head == "forgejo" and len(location) > 1:
        return FORGEJO_VARIABLES.get(str(location[1]), "UNICON_FORGEJO")
    if head == "s3" and len(location) > 1:
        return S3_VARIABLES.get(str(location[1]), "UNICON_S3")
    if head == "mail" and len(location) > 1:
        return MAIL_VARIABLES.get(str(location[1]), "UNICON_MAIL")
    return head if head.startswith("UNICON_") else f"UNICON_{head.upper()}"
