"""The mail area: handing a message to the mail server the forge already
sends its own mail through (password resets, address confirmations), so one
server and one set of credentials serve both. It travels with the forge for
the reason the object store does: the deployment that runs the forge names
the server beside it. A deployment without a mail server has an area that
says so and sends nothing.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Mail:
    """One plain-text message to one address."""

    to: str
    subject: str
    text: str


class MailPort(Protocol):
    @property
    def configured(self) -> bool:
        """Whether the deployment names a mail server at all."""
        ...

    async def send(self, mail: Mail) -> None:
        """Hand the message to the mail server. `Unavailable` when it does not
        answer in time, `Rejected` when it refuses the message or the
        platform's credentials, `Misconfigured` where there is no server.
        """
        ...
