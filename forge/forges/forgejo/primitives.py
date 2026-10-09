"""The primitive area over Forgejo: public repositories in the platform org
carrying the primitive topic, with a declaration at every version.
"""

from forge.domain.identity import PLATFORM, Identity
from forge.domain.ids import PrimitiveId
from forge.domain.workflows import Primitive
from forge.forges.forgejo.repos import Repos
from forge.forges.ids import PLATFORM_ORG, PRIMITIVE, primitive_repo

DECLARATION = "primitive.yaml"
PRIMITIVE_TOPIC = "unicon-primitive"


class ForgejoPrimitives:
    def __init__(self, repos: Repos) -> None:
        self._repos = repos

    async def list_primitives(self) -> tuple[Primitive, ...]:
        found = []
        for repo in await self._repos.marked(PRIMITIVE_TOPIC):
            name = str(repo["name"])
            owner = str(repo["owner"]["login"])
            if owner != PLATFORM_ORG or not name.endswith(f".{PRIMITIVE}"):
                continue
            versions = await self._repos.versions(PLATFORM, PLATFORM_ORG, name)
            short = name.removesuffix(f".{PRIMITIVE}")
            found.append(Primitive(id=PrimitiveId(short), name=short, versions=tuple(versions)))
        return tuple(found)

    async def read_declaration(self, as_: Identity, primitive: PrimitiveId, version: str) -> bytes:
        await self._repos.require_version(as_, PLATFORM_ORG, primitive_repo(primitive), version)
        found = await self._repos.read_file(
            as_, PLATFORM_ORG, primitive_repo(primitive), DECLARATION, at=version
        )
        return found.content
