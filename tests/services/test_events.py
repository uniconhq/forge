"""An event the forge pushes passes when its signature is the org's secret
over the raw body, and is refused, the same way each time, when the signature
or the body differs, the signature is not plain text, or the org has no
account.
"""

import hashlib
import hmac
import logging

import pytest

from forge.domain.errors import Forbidden
from forge.domain.ids import OrgName
from forge.runtime.context import Context
from forge.services import events, org_accounts, orgs
from forge.settings import Settings
from forge.testing import logged

BODY = b'{"action": "push", "repository": {"name": "spring.contest"}}'


async def test_a_signed_event_passes_and_a_bad_or_unknown_one_is_refused(
    ctx: Context, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await org_accounts.ensure_row(ctx, OrgName("acme"))
    secret = await org_accounts.event_secret_of(ctx, OrgName("acme"))
    good = hmac.new(secret, BODY, hashlib.sha256).hexdigest()

    await events.check(ctx, OrgName("acme"), BODY, good)
    await events.check(ctx, OrgName("acme"), BODY, f" {good.upper()}\n")

    with pytest.raises(Forbidden):
        await events.check(ctx, OrgName("acme"), BODY, "0" * 64)
    with pytest.raises(Forbidden):
        await events.check(ctx, OrgName("acme"), BODY + b" ", good)
    with pytest.raises(Forbidden):
        await events.check(ctx, OrgName("acme"), BODY, "")
    with pytest.raises(Forbidden):
        await events.check(ctx, OrgName("nowhere"), BODY, good)
    assert [record["org"] for record in logged(caplog, "events.refused")] == ["acme"] * 3 + [
        "nowhere"
    ]


async def test_a_signature_that_is_not_plain_text_is_refused_like_any_wrong_one(
    ctx: Context,
) -> None:
    await org_accounts.ensure_row(ctx, OrgName("acme"))

    with pytest.raises(Forbidden):
        await events.check(ctx, OrgName("acme"), BODY, "é" * 64)


def test_the_door_is_where_every_org_pushes_to() -> None:
    settings = Settings.for_tests(internal_url="http://backend:8000")

    assert events.EVENTS_PATH == "/api/v1/events/forge"
    assert events.SIGNATURE_HEADERS[0] == "X-Forgejo-Signature"
    assert orgs.event_push_url(settings, OrgName("acme")) == (
        "http://backend:8000/api/v1/events/forge/acme"
    )
