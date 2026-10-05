"""The mail area over SMTP, and the one a deployment without a mail server
has. Python's own `smtplib` speaks SMTP; it blocks, so each message is sent
on a thread of its own, and a server that does not answer within
`SMTP_TIMEOUT_SECONDS` is `Unavailable`. Plain `smtp` is for a server on the
deployment's own network, such as Mailpit on a development stack; `smtps`
encrypts from the first byte and `smtp+starttls` upgrades the connection
before the credentials cross it, the two ways the forge itself is set up.
"""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Literal

from forge.domain.errors import Misconfigured, Rejected, Unavailable
from forge.port.mail import Mail

SMTP_TIMEOUT_SECONDS = 15.0

Protocol = Literal["smtp", "smtps", "smtp+starttls"]


@dataclass(frozen=True, slots=True)
class MailConfig:
    host: str
    port: int
    protocol: Protocol
    sender: str
    user: str | None = None
    password: str | None = None


class SmtpMail:
    def __init__(self, config: MailConfig) -> None:
        self._config = config

    @property
    def configured(self) -> bool:
        return True

    async def send(self, mail: Mail) -> None:
        message = EmailMessage()
        message["From"] = self._config.sender
        message["To"] = mail.to
        message["Subject"] = mail.subject
        message["Date"] = format_datetime(datetime.now(UTC))
        message["Message-ID"] = make_msgid(domain=_domain(self._config.sender))
        message.set_content(mail.text)
        await asyncio.to_thread(self._send, message)

    def _send(self, message: EmailMessage) -> None:
        config = self._config
        try:
            if config.protocol == "smtps":
                client: smtplib.SMTP = smtplib.SMTP_SSL(
                    config.host,
                    config.port,
                    timeout=SMTP_TIMEOUT_SECONDS,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(config.host, config.port, timeout=SMTP_TIMEOUT_SECONDS)
            with client:
                if config.protocol == "smtp+starttls":
                    client.starttls(context=ssl.create_default_context())
                if config.user:
                    client.login(config.user, config.password or "")
                client.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = sorted({code for code, _ in exc.recipients.values()})
            if all(400 <= code < 500 for code in codes):
                raise Unavailable(f"the mail server is busy: {codes}") from None
            raise Rejected(f"the mail server refused the address: {codes}") from None
        except smtplib.SMTPAuthenticationError as exc:
            raise Rejected(f"the mail server refused the login: {exc.smtp_code}") from None
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                raise Unavailable(f"the mail server is busy: {exc.smtp_code}") from None
            raise Rejected(f"the mail server refused: {exc.smtp_code}") from None
        except (OSError, smtplib.SMTPException) as exc:
            raise Unavailable(f"the mail server did not answer: {type(exc).__name__}") from None


class NoMail:
    """Where the deployment names no mail server: nothing is ever sent."""

    @property
    def configured(self) -> bool:
        return False

    async def send(self, mail: Mail) -> None:
        raise Misconfigured("no mail server is configured")


def _domain(sender: str) -> str:
    address = sender.rsplit("<", 1)[-1].rstrip(">").strip()
    return address.rpartition("@")[2] or "unicon"
