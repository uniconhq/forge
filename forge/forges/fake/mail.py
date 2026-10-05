"""Mail kept in memory: every message handed over is in `sent`, in order.
`down` makes the server fail to answer and `refusing` makes it refuse, each
until it is set back; `configured` off is a deployment with no server.
"""

from forge.domain.errors import Misconfigured, Rejected, Unavailable
from forge.port.mail import Mail


class FakeMail:
    def __init__(self) -> None:
        self.sent: list[Mail] = []
        self.down = False
        self.refusing = False
        self._configured = True

    @property
    def configured(self) -> bool:
        return self._configured

    @configured.setter
    def configured(self, value: bool) -> None:
        self._configured = value

    async def send(self, mail: Mail) -> None:
        if not self._configured:
            raise Misconfigured("no mail server is configured")
        if self.down:
            raise Unavailable("the mail server did not answer")
        if self.refusing:
            raise Rejected("the mail server refused")
        self.sent.append(mail)
