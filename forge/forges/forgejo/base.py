"""What every part of the Forgejo implementation shares: the HTTP client and
the platform account's name.
"""

from typing import Any

from forge.forges.forgejo.http import ForgejoHttp

PLATFORM_ACCOUNT = "unicon-backend"
CI_ADMIN_ACCOUNT = "unicon-ci"


class ForgejoBase:
    _http: ForgejoHttp

    def __init__(self, http: ForgejoHttp) -> None:
        self._http = http


def json_of(response: Any) -> dict[str, Any]:
    payload: dict[str, Any] = response.json()
    return payload


def list_of(response: Any) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = response.json()
    return payload
