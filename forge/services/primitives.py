"""The platform's primitives as a workflow reads them: the declaration a
step's `use:` names, read at its version as the person whose save or check
needs it, and every primitive the platform holds at each of its versions,
for the editor's palette.
"""

from dataclasses import dataclass

from forge.domain.errors import Forbidden, NotFound
from forge.domain.identity import AsUser, Identity
from forge.domain.plans import natural
from forge.domain.primitives import PrimitiveDeclaration, parse_primitive
from forge.domain.roles import PRIMITIVE_OWNER, primitive_id_of
from forge.domain.sessions import Session
from forge.domain.workflow_definition import WORKFLOW_FILE, WorkflowRef
from forge.domain.yaml_models import InvalidDefinition
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import names, sessions


async def declaration(
    ctx: Context, as_: Identity, use: WorkflowRef
) -> tuple[PrimitiveDeclaration | None, str | None]:
    """The declaration a `use:` names, or what is wrong with it. A `use:` that
    names a workflow the reader can read is refused as not a primitive, and
    one they cannot read is refused naming it, the same whether it is not
    there or not shared with them.
    """
    primitive = primitive_id_of(use)
    if primitive is not None:
        try:
            text = await ctx.forge.primitives.read_declaration(as_, primitive, use.version)
        except NotFound, Forbidden:
            pass
        else:
            try:
                return parse_primitive(text), None
            except InvalidDefinition as invalid:
                return None, (
                    f"The primitive {use} is not in the current format; use a later version of "
                    f"it: {invalid.detail}"
                )
    try:
        await ctx.forge.workflows.read_workflow_file(
            as_, await names.workflow_id(ctx, use), use.version, WORKFLOW_FILE
        )
    except NotFound, Forbidden:
        return None, (
            f"{use} cannot be read: there is no such primitive or workflow at that version, "
            "or it is not shared with you."
        )
    return None, f"{use} is not a primitive: a step uses a primitive, never a workflow."


@dataclass(frozen=True, slots=True)
class PrimitiveVersion:
    """One version of a primitive as the palette shows it: its reference,
    `unicon/<name>@<version>`, and its declaration, or none with the reason
    when the version is in a format the platform no longer reads, so the
    palette leaves it out of what a step may use.
    """

    name: str
    version: str
    declaration: PrimitiveDeclaration | None
    problem: str | None = None

    @property
    def ref(self) -> str:
        return f"{PRIMITIVE_OWNER}/{self.name}@{self.version}"


@action
async def listing(ctx: Context, session: Session) -> tuple[PrimitiveVersion, ...]:
    """Every primitive the platform holds at every version, by name and then
    version in natural order, each with its declaration, read as the person
    signed in. Every primitive is public, so a session is all it needs.
    """
    as_ = AsUser(session.user_id, await sessions.credential_for(ctx, session.id))
    found: list[PrimitiveVersion] = []
    for primitive in sorted(await ctx.forge.primitives.list_primitives(), key=lambda p: p.name):
        for version in sorted(primitive.versions, key=natural):
            try:
                text = await ctx.forge.primitives.read_declaration(as_, primitive.id, version)
            except NotFound, Forbidden:
                problem = (
                    f"{PRIMITIVE_OWNER}/{primitive.name}@{version} has no declaration to read."
                )
                found.append(PrimitiveVersion(primitive.name, version, None, problem))
                continue
            try:
                found.append(PrimitiveVersion(primitive.name, version, parse_primitive(text)))
            except InvalidDefinition as invalid:
                found.append(PrimitiveVersion(primitive.name, version, None, invalid.detail))
    return tuple(found)
