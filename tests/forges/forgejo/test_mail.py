"""The mail area over SMTP, against a server in this process that speaks just
enough SMTP to take a message, or refuse it: a message arrives with the
sender, the address and the text it was given; a server that refuses the
address is `Rejected`, one that is busy or not there is `Unavailable`, and
a deployment without a server sends nothing.
"""

import socketserver
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from forge.adapters.git.forgejo.mail import MailConfig, NoMail, SmtpMail
from forge.domain.errors import Misconfigured, Rejected, Unavailable
from forge.port.mail import Mail


@dataclass
class Server:
    port: int
    answer_to_rcpt: bytes = b"250 ok"
    received: list[bytes] = field(default_factory=list)


@pytest.fixture
def server() -> Iterator[Server]:
    state = Server(port=0)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.wfile.write(b"220 test ready\r\n")
            while line := self.rfile.readline():
                verb = line.strip().split(b" ", 1)[0].upper()
                if verb in (b"EHLO", b"HELO"):
                    self.wfile.write(b"250 test\r\n")
                elif verb == b"RCPT":
                    self.wfile.write(state.answer_to_rcpt + b"\r\n")
                elif verb == b"DATA":
                    self.wfile.write(b"354 go\r\n")
                    body = b""
                    while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                        body += chunk
                    state.received.append(body)
                    self.wfile.write(b"250 queued\r\n")
                elif verb == b"QUIT":
                    self.wfile.write(b"221 bye\r\n")
                    return
                else:
                    self.wfile.write(b"250 ok\r\n")

    listener = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    listener.daemon_threads = True
    state.port = listener.server_address[1]
    thread = threading.Thread(target=listener.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        listener.shutdown()
        listener.server_close()


def _mail(port: int) -> SmtpMail:
    return SmtpMail(
        MailConfig(host="127.0.0.1", port=port, protocol="smtp", sender="Unicon <u@example.test>")
    )


MESSAGE = Mail(to="carol@example.test", subject="You are invited", text="Open the link.\n")


async def test_a_message_reaches_the_server_with_its_sender_address_and_text(
    server: Server,
) -> None:
    await _mail(server.port).send(MESSAGE)

    [body] = server.received
    assert b"From: Unicon <u@example.test>" in body
    assert b"To: carol@example.test" in body
    assert b"Subject: You are invited" in body
    assert b"Open the link." in body


async def test_a_refused_address_is_rejected_and_a_busy_server_is_unavailable(
    server: Server,
) -> None:
    server.answer_to_rcpt = b"550 no such mailbox"
    with pytest.raises(Rejected):
        await _mail(server.port).send(MESSAGE)
    server.answer_to_rcpt = b"451 try later"
    with pytest.raises(Unavailable):
        await _mail(server.port).send(MESSAGE)
    assert server.received == []


async def test_a_server_that_is_not_there_is_unavailable(server: Server) -> None:
    closed = server.port
    with socketserver.TCPServer(("127.0.0.1", 0), socketserver.BaseRequestHandler) as spare:
        closed = spare.server_address[1]
    with pytest.raises(Unavailable):
        await _mail(closed).send(MESSAGE)


async def test_without_a_server_nothing_is_sent() -> None:
    assert not NoMail().configured
    with pytest.raises(Misconfigured):
        await NoMail().send(MESSAGE)
