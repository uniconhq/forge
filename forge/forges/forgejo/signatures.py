"""HTTP message signatures as Woodpecker signs the calls it makes to an
extension: RFC 9421 with the server's ed25519 key, over the request target
and the body's digest, and RFC 9530's `Content-Digest` for the body.

    Content-Digest:  sha-256=:<base64>:
    Signature-Input: woodpecker-ci-extensions=("@request-target" "content-digest")
                     ;created=<unix>;alg="ed25519"   (one line as sent)
    Signature:       woodpecker-ci-extensions=:<base64>:

A request verifies when one signature it carries covers the request target
and the body's digest, was made within `FRESHNESS` of now and, when it says
when it expires, has not, and checks against the key, and the digest is the
body's own. The signature base is
built as RFC 9421 lays it out: one line per covered component,
`"<name>": <value>`, and last `"@signature-params": ` with the parameters
exactly as the request sent them.
"""

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

FRESHNESS = timedelta(minutes=5)
REQUIRED = frozenset({"content-digest"})
TARGETS = frozenset({"@request-target", "@path"})

_MEMBER = re.compile(r'\s*([a-z*][a-z0-9_.*-]*)=(\((?:[^()"]|"(?:[^"\\]|\\.)*")*\)(?:;[^,]*)?)')
_COMPONENT = re.compile(r'"([^"]+)"')
_PARAMETER = re.compile(r';\s*([a-z*][a-z0-9_.*-]*)(?:=("(?:[^"\\]|\\.)*"|[^;]*))?')
_SIGNATURE = re.compile(r"\s*([a-z*][a-z0-9_.*-]*)=:([A-Za-z0-9+/=]*):")
_DIGEST = re.compile(r"\s*(sha-256|sha-512)=:([A-Za-z0-9+/=]*):")


class Unverified(Exception):
    """A request that does not verify, with why, for the log."""


@dataclass(frozen=True, slots=True)
class Signed:
    """What one signature covers: its components in order, its parameters
    as sent, when it says it was made, and when it expires, if it says.
    """

    label: str
    components: tuple[str, ...]
    parameters: str
    created: int | None
    expires: int | None = None


def header(headers: Mapping[str, str], name: str) -> str | None:
    """A header's value, whatever the case of its name."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def verify(
    key: Ed25519PublicKey,
    *,
    method: str,
    target: str,
    headers: Mapping[str, str],
    body: bytes,
    now: datetime,
) -> None:
    """Raise `Unverified` unless the request carries a signature by `key`
    over its target and its body's digest, made within `FRESHNESS` of `now`,
    and its digest is the body's.
    """
    check_digest(header(headers, "content-digest"), body)
    signatures = _signature_values(header(headers, "signature"))
    inputs = _inputs(header(headers, "signature-input"))
    if not inputs:
        raise Unverified("no signature input")
    reasons = []
    for signed in inputs:
        value = signatures.get(signed.label)
        if value is None:
            reasons.append(f"{signed.label}: no signature")
            continue
        try:
            _check_one(key, signed, value, method=method, target=target, headers=headers, now=now)
        except Unverified as exc:
            reasons.append(f"{signed.label}: {exc}")
            continue
        return
    raise Unverified("; ".join(reasons))


def check_digest(value: str | None, body: bytes) -> None:
    """Raise `Unverified` unless `value` holds a SHA-256 or SHA-512 digest
    and every one it holds is the body's.
    """
    if not value:
        raise Unverified("no content digest")
    found = dict(_DIGEST.findall(value))
    if not found:
        raise Unverified("no digest the platform reads")
    for name, digest in found.items():
        expected = (hashlib.sha256 if name == "sha-256" else hashlib.sha512)(body).digest()
        if not hmac.compare_digest(_decoded(digest), expected):
            raise Unverified("the body is not the one the digest names")


def _check_one(
    key: Ed25519PublicKey,
    signed: Signed,
    value: bytes,
    *,
    method: str,
    target: str,
    headers: Mapping[str, str],
    now: datetime,
) -> None:
    covered = set(signed.components)
    if not covered >= REQUIRED or not covered & TARGETS:
        raise Unverified("the signature does not cover the target and the digest")
    if signed.created is None:
        raise Unverified("the signature says nothing of when it was made")
    age = abs(now.timestamp() - signed.created)
    if age > FRESHNESS.total_seconds():
        raise Unverified("the signature is stale")
    if signed.expires is not None and now.timestamp() >= signed.expires:
        raise Unverified("the signature has expired")
    lines = []
    for component in signed.components:
        lines.append(f'"{component}": {_component(component, method, target, headers)}')
    lines.append(f'"@signature-params": {signed.parameters}')
    try:
        key.verify(value, "\n".join(lines).encode())
    except InvalidSignature:
        raise Unverified("the signature does not check against the key") from None


def _component(name: str, method: str, target: str, headers: Mapping[str, str]) -> str:
    match name:
        case "@request-target":
            return target
        case "@path":
            return target.split("?", 1)[0]
        case "@method":
            return method.upper()
        case "@query":
            return "?" + target.split("?", 1)[1] if "?" in target else "?"
    if name.startswith("@"):
        raise Unverified(f"the signature covers {name}, which the platform does not read")
    value = header(headers, name)
    if value is None:
        raise Unverified(f"the signature covers {name}, which the request lacks")
    return value.strip()


def _inputs(value: str | None) -> list[Signed]:
    found = []
    for label, member in _MEMBER.findall(value or ""):
        inner, _, rest = member.partition(")")
        parameters = member
        components = tuple(_COMPONENT.findall(inner + ")"))
        created = expires = None
        algorithm = None
        for name, raw in _PARAMETER.findall(rest):
            text = raw.strip('"')
            if name == "created" and text.isdigit():
                created = int(text)
            elif name == "expires" and text.isdigit():
                expires = int(text)
            elif name == "alg":
                algorithm = text
        if algorithm not in (None, "ed25519"):
            continue
        found.append(Signed(label, components, parameters, created, expires))
    return found


def _signature_values(value: str | None) -> dict[str, bytes]:
    return {label: _decoded(encoded) for label, encoded in _SIGNATURE.findall(value or "")}


def _decoded(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error, ValueError:
        return b""
