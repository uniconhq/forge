"""Signing a user in through Forgejo's OpenID Connect provider and keeping
their credential alive. The browser is sent to the public URL; every other
call goes over the internal one.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from forge.domain.errors import Forbidden, Misconfigured, Rejected
from forge.domain.identity import Credential, User
from forge.forges.forgejo.http import ForgejoHttp, message_of
from forge.port import SignedIn

AUTHORIZE_PATH = "/login/oauth/authorize"
TOKEN_PATH = "/login/oauth/access_token"
USERINFO_PATH = "/login/oauth/userinfo"
SCOPES = "openid profile email"

SPENT_GRANT_ERRORS = frozenset({"invalid_grant", "unauthorized_client"})
INVALID_CLIENT = 400


class OAuth:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        public_url: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> None:
        self._client = client
        self._public_url = public_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "response_type": "code",
                "scope": SCOPES,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._public_url}{AUTHORIZE_PATH}?{query}"

    async def complete_sign_in(self, http: ForgejoHttp, *, code: str, verifier: str) -> SignedIn:
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": verifier,
            }
        )
        credential = _credential(payload)
        user = await self.user_of(credential)
        return SignedIn(user=user, credential=credential, nonce=_nonce_of(payload))

    async def refresh(self, credential: Credential) -> Credential:
        payload = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": credential.refresh}
        )
        return _credential(payload)

    async def user_of(self, credential: Credential) -> User:
        response = await self._client.get(
            USERINFO_PATH, headers={"Authorization": f"Bearer {credential.access}"}
        )
        if response.is_error:
            raise Forbidden(message_of(response) or "the forge does not accept this credential")
        return _user(response.json())

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        requested_at = datetime.now(UTC)
        response = await self._client.post(
            TOKEN_PATH,
            data=form | {"client_id": self._client_id, "client_secret": self._client_secret},
            headers={"Accept": "application/json"},
        )
        if response.is_error:
            raise _grant_refusal(response)
        payload: dict[str, Any] = response.json()
        payload["_requested_at"] = requested_at
        return payload


def _grant_refusal(response: httpx.Response) -> Exception:
    error = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            error = str(body.get("error", ""))
    except ValueError:
        body = None
    if error in SPENT_GRANT_ERRORS:
        return Forbidden(f"the forge will not renew this credential: {error}")
    if response.status_code == INVALID_CLIENT and error == "invalid_client":
        return Misconfigured("the forge does not recognise this platform's registration")
    return Rejected(message_of(response) or f"the forge answered {response.status_code}")


def _credential(payload: dict[str, Any]) -> Credential:
    requested_at: datetime = payload["_requested_at"]
    return Credential(
        access=str(payload["access_token"]),
        refresh=str(payload["refresh_token"]),
        expires_at=requested_at + timedelta(seconds=int(payload["expires_in"])),
    )


def _nonce_of(payload: dict[str, Any]) -> str | None:
    """The nonce claim of the identity token. The token arrived over the
    back channel with the client secret, so its claims are read without a
    signature check.
    """
    token = payload.get("id_token")
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    body = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    nonce = claims.get("nonce") if isinstance(claims, dict) else None
    return str(nonce) if nonce else None


def _user(claims: dict[str, Any]) -> User:
    return User(
        id=int(claims["sub"]),
        username=str(claims["preferred_username"]),
        name=_text(claims.get("name")),
        email=_text(claims.get("email")),
        avatar_url=_text(claims.get("picture")),
    )


def _text(value: object) -> str | None:
    return str(value) if value else None
