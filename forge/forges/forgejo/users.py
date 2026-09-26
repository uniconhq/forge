"""The identity operations against Forgejo: sign in, refresh a credential,
look a user up, deactivate and delete. Delete uses purge, since Forgejo
refuses to delete a user who is in a team or owns a repository.
"""

from typing import Any

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, Credential, User
from forge.forges.forgejo.base import ForgejoBase, json_of
from forge.forges.forgejo.oauth import OAuth
from forge.port import SignedIn


class IdentityOps(ForgejoBase):
    _oauth: OAuth

    def sign_in_url(self, *, state: str, code_challenge: str, nonce: str) -> str:
        return self._oauth.sign_in_url(state=state, code_challenge=code_challenge, nonce=nonce)

    async def complete_sign_in(self, *, code: str, verifier: str) -> SignedIn:
        return await self._oauth.complete_sign_in(self._http, code=code, verifier=verifier)

    async def refresh_credential(self, credential: Credential) -> Credential:
        return await self._oauth.refresh(credential)

    async def user_of(self, credential: Credential) -> User:
        return await self._oauth.user_of(credential)

    async def find_user(self, user_id: int) -> User:
        return user_from(await self._user_json(user_id))

    async def deactivate_user(self, user_id: int) -> None:
        person = await self._user_json(user_id)
        await self._http.call(
            PLATFORM,
            "PATCH",
            f"/api/v1/admin/users/{person['login']}",
            json={
                "active": False,
                "login_name": person.get("login_name") or person["login"],
                "source_id": person.get("source_id", 0),
            },
        )

    async def delete_user(self, user_id: int) -> None:
        person = await self._user_json(user_id)
        await self._http.call(
            PLATFORM, "DELETE", f"/api/v1/admin/users/{person['login']}", params={"purge": "true"}
        )

    async def username_of(self, user_id: int) -> str:
        return str((await self._user_json(user_id))["login"])

    async def _user_json(self, user_id: int) -> dict[str, Any]:
        found = json_of(
            await self._http.call(PLATFORM, "GET", "/api/v1/users/search", params={"uid": user_id})
        )
        people = found.get("data") or []
        if not people:
            raise NotFound(f"no user with id {user_id}")
        person: dict[str, Any] = people[0]
        return person


def user_from(person: dict[str, Any]) -> User:
    return User(
        id=int(person["id"]),
        username=str(person["login"]),
        name=str(person["full_name"]) if person.get("full_name") else None,
        email=str(person["email"]) if person.get("email") else None,
        avatar_url=str(person["avatar_url"]) if person.get("avatar_url") else None,
        active=bool(person.get("active", True)),
    )
