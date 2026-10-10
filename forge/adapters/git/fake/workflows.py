"""The workflow and primitive areas in memory."""

from collections.abc import Mapping

from forge.adapters.git.fake.state import Repo, State, token_of
from forge.adapters.ids import (
    PLATFORM_ORG,
    PRIMITIVE,
    WORKFLOW,
    WorkflowRef,
    parse_workflow,
    primitive_repo,
)
from forge.domain.content import ConflictToken, File, Files
from forge.domain.errors import Conflict, Forbidden, NotFound
from forge.domain.identity import PLATFORM, Identity, User
from forge.domain.ids import PrimitiveId, VersionId, WorkflowId
from forge.domain.roles import Role, Scope
from forge.domain.workflows import Primitive, Visibility, Workflow

DECLARATION = "primitive.yaml"


class FakeWorkflows:
    def __init__(self, state: State) -> None:
        self._state = state

    async def create_workflow(
        self, as_: Identity, owner: str, name: str, files: Files, visibility: Visibility
    ) -> WorkflowId:
        """Made as the platform in the owner's name, as Forgejo does: a
        person's own they own, and an org's the org's roles reach. The files
        are then written as `as_`, who must be able to write there.
        """
        self._state.record("create_workflow", as_, owner=owner, name=name, visibility=visibility)
        ref = WorkflowRef(owner, name)
        scope = Scope(owner) if owner in self._state.orgs else None
        try:
            repo = self._state.create_repo(
                PLATFORM, owner, ref.repo, {}, scope=scope, marked=WORKFLOW
            )
        except Conflict:
            # One of the name without the mark is a create that stopped before
            # the end, which Forgejo's create finishes.
            existing = self._state.repo(owner, ref.repo)
            if existing.marked is None:
                existing.rewrites_refused = True
                existing.marked = WORKFLOW
            raise
        repo.private = visibility is not Visibility.PUBLIC
        repo.rewrites_refused = True
        if files:
            self._state.require_write(as_, repo)
            self._state.commit(repo, files, "Create", self._state.author(as_))
        return ref.id

    async def set_workflow_visibility(
        self, as_: Identity, workflow: WorkflowId, visibility: Visibility
    ) -> None:
        self._state.record("set_workflow_visibility", as_, workflow=workflow, visibility=visibility)
        repo = self._repo(workflow)
        self._state.require_write(as_, repo)
        repo.private = visibility is not Visibility.PUBLIC

    async def share_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        self._state.record("share_workflow", as_, workflow=workflow, user_id=user_id)
        repo = self._repo(workflow)
        self._state.require_write(as_, repo)
        repo.readers.add(user_id)

    async def unshare_workflow(self, as_: Identity, workflow: WorkflowId, user_id: int) -> None:
        self._state.record("unshare_workflow", as_, workflow=workflow, user_id=user_id)
        repo = self._repo(workflow)
        self._state.require_write(as_, repo)
        repo.readers.discard(user_id)

    async def workflow_readers(self, as_: Identity, workflow: WorkflowId) -> tuple[User, ...]:
        self._state.record("workflow_readers", as_, workflow=workflow)
        repo = self._repo(workflow)
        self._state.require_write(as_, repo)
        return tuple(self._state.user(user_id) for user_id in sorted(repo.readers))

    async def create_workflow_version(
        self, as_: Identity, workflow: WorkflowId, version: str, at: VersionId | None = None
    ) -> None:
        self._state.record(
            "create_workflow_version", as_, workflow=workflow, version=version, at=at
        )
        repo = self._repo(workflow)
        self._state.require_write(as_, repo)
        if at is not None and at not in repo.snapshots:
            raise NotFound(f"{workflow} has no commit {at}")
        self._state.create_version(as_, repo, version, at=at)

    async def read_workflow_draft(
        self, as_: Identity, workflow: WorkflowId, path: str
    ) -> tuple[VersionId, File]:
        self._state.record("read_workflow_draft", as_, workflow=workflow, path=path)
        self._state.check_up()
        repo = self._repo(workflow)
        self._state.require_read(as_, repo)
        if path not in repo.files:
            raise NotFound(f"{path} is not in {workflow}")
        content = repo.files[path]
        return VersionId(repo.head), File(path=path, content=content, token=repo.tokens[path])

    async def write_workflow_file(
        self,
        as_: Identity,
        workflow: WorkflowId,
        path: str,
        content: bytes,
        *,
        expected: ConflictToken | None,
        message: str,
    ) -> File:
        self._state.record("write_workflow_file", as_, workflow=workflow, path=path)
        self._state.check_up()
        repo = self._repo(workflow)
        self._state.require_write(as_, repo)
        if repo.tokens.get(path) != expected:
            raise Conflict(f"{path} in {workflow} has changed since it was read")
        self._state.commit(repo, {path: content}, message, self._state.author(as_))
        return File(path=path, content=content, token=repo.tokens[path])

    async def read_workflow_file(
        self, as_: Identity, workflow: WorkflowId, version: str, path: str
    ) -> File:
        self._state.record("read_workflow_file", as_, workflow=workflow, version=version, path=path)
        repo = self._repo(workflow)
        self._state.require_read(as_, repo)
        files = self._state.version_files(repo, version) if version in repo.versions else {}
        if path not in files:
            raise NotFound(f"{path} at {version} is not in {workflow}")
        return File(path=path, content=files[path], token=token_of(files[path]))

    async def search_public_workflows(self, query: str) -> tuple[Workflow, ...]:
        self._state.record("search_public_workflows", PLATFORM, query=query)
        return tuple(
            _workflow(repo)
            for repo in self._state.repos.values()
            if repo.marked == WORKFLOW and not repo.private and query in repo.name
        )

    async def star_workflow(self, as_: Identity, workflow: WorkflowId) -> None:
        self._state.record("star_workflow", as_, workflow=workflow)
        repo = self._repo(workflow)
        self._state.require_read(as_, repo)
        author = self._state.author(as_)
        if author is not None:
            repo.stars.add(author)

    async def copy_workflow(
        self, as_: Identity, source: WorkflowId, version: str, owner: str, name: str
    ) -> WorkflowId:
        self._state.record(
            "copy_workflow", as_, source=source, version=version, owner=owner, name=name
        )
        origin = self._repo(source)
        self._state.require_read(as_, origin)
        files = dict(self._state.version_files(origin, version))
        return await self.create_workflow(as_, owner, name, files, Visibility.PRIVATE)

    async def workflows_owned_by(self, user_id: int) -> tuple[Workflow, ...]:
        self._state.record("workflows_owned_by", PLATFORM, user_id=user_id)
        username = self._state.username(user_id)
        return tuple(
            _workflow(repo)
            for repo in self._state.repos.values()
            if repo.marked == WORKFLOW and repo.owner == username
        )

    async def describe_workflow(self, as_: Identity, workflow: WorkflowId) -> Workflow:
        self._state.record("describe_workflow", as_, workflow=workflow)
        self._state.check_up()
        repo = self._repo(workflow)
        try:
            self._state.require_read(as_, repo)
        except Forbidden:
            raise NotFound(f"no workflow {workflow}") from None
        return _workflow(repo)

    async def workflows_readable_by(self, as_: Identity) -> tuple[Workflow, ...]:
        self._state.record("workflows_readable_by", as_)
        self._state.check_up()
        user_id = self._state.author(as_)

        def reached(repo: Repo) -> bool:
            if repo.owner == PLATFORM_ORG:
                return not repo.private
            if user_id is None:
                return True
            if not repo.private:
                # A public one is reached when it is the person's own or an
                # org's they belong to; anyone else's is the marketplace's.
                return repo.owner.lower() == self._state.username(
                    user_id
                ).lower() or self._state.holds(user_id, repo, Role.OBSERVER)
            return (
                user_id in repo.readers
                or self._state.may_write(user_id, repo)
                or self._state.holds(user_id, repo, Role.OBSERVER)
            )

        return tuple(
            _workflow(repo)
            for repo in self._state.repos.values()
            if repo.marked == WORKFLOW and reached(repo)
        )

    async def workflow_key(self, workflow: WorkflowId) -> str:
        self._state.record("workflow_key", PLATFORM, workflow=workflow)
        self._state.check_up()
        return str(self._repo(workflow).id)

    def _repo(self, workflow: WorkflowId) -> Repo:
        """The workflow's repository, which carries the workflow mark;
        `NotFound` for one that does not.
        """
        ref = parse_workflow(workflow)
        repo = self._state.repo(ref.owner, ref.repo)
        if repo.marked != WORKFLOW:
            raise NotFound(f"{workflow} is not a workflow")
        return repo


class FakePrimitives:
    def __init__(self, state: State) -> None:
        self._state = state

    def add(self, name: str, versions: Mapping[str, bytes]) -> None:
        """Seed a primitive the way bootstrap does, one declaration per
        version.
        """
        repo = self._state.create_repo(
            PLATFORM, PLATFORM_ORG, primitive_repo(PrimitiveId(name)), {}, marked=PRIMITIVE
        )
        repo.private = False
        for version, declaration in versions.items():
            self._state.commit(repo, {DECLARATION: declaration}, version, None)
            self._state.create_version(PLATFORM, repo, version)

    async def list_primitives(self) -> tuple[Primitive, ...]:
        self._state.record("list_primitives", PLATFORM)
        return tuple(
            Primitive(
                id=PrimitiveId(repo.name.removesuffix(f".{PRIMITIVE}")),
                name=repo.name.removesuffix(f".{PRIMITIVE}"),
                versions=tuple(repo.versions),
            )
            for repo in self._state.repos.values()
            if repo.marked == PRIMITIVE
        )

    async def read_declaration(self, as_: Identity, primitive: PrimitiveId, version: str) -> bytes:
        self._state.record("read_declaration", as_, primitive=primitive, version=version)
        self._state.check_up()
        repo = self._state.repo(PLATFORM_ORG, primitive_repo(primitive))
        self._state.require_read(as_, repo)
        files = self._state.version_files(repo, version) if version in repo.versions else {}
        if DECLARATION not in files:
            raise NotFound(f"{primitive} has no declaration at {version}")
        return files[DECLARATION]


def _workflow(repo: Repo) -> Workflow:
    if not repo.private:
        visibility = Visibility.PUBLIC
    elif repo.readers:
        visibility = Visibility.SHARED
    else:
        visibility = Visibility.PRIVATE
    name = repo.name.removesuffix(f".{WORKFLOW}")
    return Workflow(
        id=WorkflowRef(repo.owner, name).id,
        owner=repo.owner,
        name=name,
        visibility=visibility,
        stars=len(repo.stars),
        versions=tuple(repo.versions),
    )
