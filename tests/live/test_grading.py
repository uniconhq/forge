"""Grading against the real Woodpecker: the CI's signing key is read with the
administrator's token and a request it did not sign is refused.
"""

import base64
import hashlib
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from forge.adapters.ci.woodpecker import WoodpeckerCi
from forge.domain.errors import Forbidden
from forge.domain.grading import InboundRequest, PipelineRun, PipelineSpec
from forge.domain.ids import OrgId
from tests.live.conftest import LIVE, needs_ci

pytestmark = [*LIVE, needs_ci]


async def _never_looked_up(grading: str | None, org: OrgId) -> tuple[PipelineRun, PipelineSpec]:
    raise AssertionError("a request that does not verify is never looked up")


async def test_the_cis_key_is_read_and_a_request_it_did_not_sign_is_refused(
    woodpecker: WoodpeckerCi,
) -> None:
    body = b'{"repo": {"owner": "acme", "name": "unicon-dispatch"}, "pipeline": {}}'
    digest = "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode() + ":"
    now = datetime.now(UTC)
    parameters = (
        f'("@request-target" "content-digest");created={int(now.timestamp())};alg="ed25519"'
    )
    base = (
        f'"@request-target": /api/v1/ci/config\n"content-digest": {digest}\n'
        f'"@signature-params": {parameters}'
    )
    signature = base64.b64encode(Ed25519PrivateKey.generate().sign(base.encode())).decode()
    request = InboundRequest(
        method="POST",
        target="/api/v1/ci/config",
        headers={
            "Content-Digest": digest,
            "Signature-Input": f"woodpecker-ci-extensions={parameters}",
            "Signature": f"woodpecker-ci-extensions=:{signature}:",
        },
        body=body,
    )

    with pytest.raises(Forbidden):
        await woodpecker.grading.answer(request, _never_looked_up, now=now)

    assert isinstance(woodpecker.grading._key, Ed25519PublicKey)
