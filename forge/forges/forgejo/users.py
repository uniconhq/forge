"""Users at Forgejo: the one place a user id becomes a login, and the
administration of accounts as the platform.
"""

from typing import Any

from forge.domain.errors import NotFound
from forge.domain.identity import PLATFORM, User
from forge.forges.forgejo.http import Http, json_of


class Users:
    def __init__(self, http: Http) -> None:
        self._http = http

    async def find(self, user_id: int) -> User:
        return user_from(await self._record(user_id))

    async def username_of(self, user_id: int) -> str:
        return str((await self._record(user_id))["login"])

    async def deactivate(self, user_id: int) -> None:
        person = await self._record(user_id)
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

    async def delete(self, user_id: int) -> None:
        """Remove the user and what they own, and keep what other people still
        read. Forgejo's purge takes the user's issues and comments with the
        account (measured on 15.0.8), and a plain delete refuses while the user
        still owns a repository. So what they own, at most their private
        workflows, is removed first, and then the account is deleted without
        purge: their questions and answers stay, under the ghost user.
        """
        person = await self._record(user_id)
        login = str(person["login"])
        for repo in await self._http.get_all(PLATFORM, f"/api/v1/users/{login}/repos"):
            await self._http.call(PLATFORM, "DELETE", f"/api/v1/repos/{login}/{repo['name']}")
        await self._http.call(PLATFORM, "DELETE", f"/api/v1/admin/users/{login}")

    async def _record(self, user_id: int) -> dict[str, Any]:
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
