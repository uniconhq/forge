"""A Forgejo implementation over a recording transport, so a test asserts the
requests each area makes: method, path and body.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from forge.adapters.git.forgejo import ForgejoConfig, ForgejoForge
from forge.adapters.git.forgejo.http import ForgejoAuth, Http, WoodpeckerAuth

CONFIG = ForgejoConfig(
    public_url="http://forge.test",
    internal_url="http://forge.internal",
    admin_token="admin",
    platform_account="platform-account",
    oauth_client_id="client",
    oauth_client_secret="secret",
    sign_in_redirect_uri="http://app.test/api/v1/auth/callback",
    sign_ups_open=True,
    ci_url="http://ci.internal",
    ci_public_url="http://ci.test",
    ci_admin_token="ci-admin",
)


@dataclass
class Recorder:
    """Answers each request from what `on` scripted for its method and path,
    an empty list otherwise, and keeps every request it saw.
    """

    answers: dict[tuple[str, str], list[httpx.Response]] = field(default_factory=dict)
    seen: list[httpx.Request] = field(default_factory=list)

    def on(self, method: str, path: str, *responses: httpx.Response) -> None:
        self.answers.setdefault((method, path), []).extend(responses)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        queue = self.answers.get((request.method, request.url.path))
        if not queue:
            return httpx.Response(200, json=[])
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sent(self, method: str, path: str) -> list[dict[str, Any]]:
        return [
            json.loads(request.content) if request.content else {}
            for request in self.seen
            if request.method == method and request.url.path == path
        ]

    def calls(self) -> list[str]:
        return [f"{request.method} {request.url.path}" for request in self.seen]

    def headers(self, method: str, path: str) -> list[str]:
        return [
            request.headers.get("Authorization", "")
            for request in self.seen
            if request.method == method and request.url.path == path
        ]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def forgejo(recorder: Recorder) -> ForgejoForge:
    transport = httpx.MockTransport(recorder.handle)
    forge_http = Http(
        httpx.AsyncClient(base_url=CONFIG.internal_url, transport=transport),
        ForgejoAuth(CONFIG.admin_token),
        backoff_seconds=0,
    )
    ci_http = Http(
        httpx.AsyncClient(base_url=CONFIG.ci_url, transport=transport),
        WoodpeckerAuth(CONFIG.ci_admin_token),
        backoff_seconds=0,
    )
    return ForgejoForge(CONFIG, clients=(forge_http, ci_http))


def ok(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)
