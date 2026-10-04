"""Making a workflow. Anyone signed in may make one under their own name, or
under an org where they hold the manager role or above; an observer of the
org may not, and neither may anyone for another person. The forge lets only
the platform create a place, so the port makes it as the platform in the
owner's name, and the person writes its first commit, a `workflow.yaml`
named `<owner>/<name>` that is valid as written
(`forge.domain.workflow_definition.starter_workflow`). A new workflow is
private.

The owner is given by the name people call it. An org's name is looked up
first, the way a `workflow.yaml` reference reads an owner
(`names.workflow_id`), so `acme/tuned` names the same workflow in both. An
owner that is neither the caller nor an org where they are a manager is
refused with `Forbidden`, the same words whether there is such an org or
not, so nobody learns which orgs exist. A person's own workflows sit under
their username, which has to follow the name rules once in lower case for
the workflow to be named by it.

A workflow's name is not reserved here: the forge holds one place per owner
and name, and a name taken there is `Conflict`.
"""

from dataclasses import dataclass

from forge.domain.errors import Conflict, Forbidden, InvalidName, PortError
from forge.domain.identity import AsUser, Identity
from forge.domain.ids import WorkflowId
from forge.domain.names import validate_name
from forge.domain.roles import Role, Scope
from forge.domain.sessions import Session
from forge.domain.workflow_definition import starter_workflow
from forge.domain.workflows import Visibility
from forge.log import get_logger
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import access, making, names, sessions

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class NewWorkflow:
    """A workflow just made: its id at the forge, and the owner and name a
    person calls it by, `<owner>/<name>`. An org's id is built from its key,
    so the id is for the package and the owner and the name are for showing.
    """

    id: WorkflowId
    owner: str
    name: str


@action
async def create(ctx: Context, session: Session, owner: str, name: str) -> NewWorkflow:
    """Make the workflow `name` under `owner`, the caller's own username or
    an org's name, private, with its first commit written as the caller.
    `Forbidden` unless the owner is the caller or an org where they hold the
    manager role or above; `InvalidName` for a name that breaks the rules,
    or a username that cannot name a workflow; `Conflict` when the owner
    has a workflow of that name.
    """
    org = await names.org_id(ctx, owner)
    as_: Identity
    at_forge: str
    if org is not None:
        organiser = await access.organiser(ctx, session, Scope(org, label=owner), Role.MANAGER)
        label, as_, at_forge = owner, organiser.identity, org
    else:
        username = await _username_now(ctx, session)
        if owner.lower() != username.lower():
            log.info(
                "access.refused", user_id=session.user_id, owner=owner, role=Role.MANAGER.value
            )
            raise Forbidden(f"This needs the {Role.MANAGER.value} role at {owner}.")
        label = _own_label(username)
        as_ = AsUser(session.user_id, await sessions.credential_for(ctx, session.id))
        at_forge = username
    validate_name(name)
    try:
        workflow = await ctx.forge.workflows.create_workflow(
            as_, at_forge, name, starter_workflow(label, name), Visibility.PRIVATE
        )
    except Conflict:
        raise Conflict(f"There is a workflow {label}/{name} already.") from None
    except PortError as exc:
        raise making.failure(exc, "workflows.step_failed", owner=at_forge, name=name) from None
    log.info("workflows.created", workflow=workflow, user_id=session.user_id)
    return NewWorkflow(workflow, label, name)


async def _username_now(ctx: Context, session: Session) -> str:
    """The caller's username as the forge has it now. The session keeps the
    one they signed in with, which a rename on the account page leaves
    behind, and someone else may since have taken it.
    """
    try:
        return (await ctx.forge.identity.find_user(session.user_id)).username
    except PortError as exc:
        raise making.failure(exc, "workflows.step_failed", user_id=str(session.user_id)) from None


def _own_label(username: str) -> str:
    """The owner a person's own workflows are named by: their username in
    lower case. `InvalidName` when that breaks the name rules, as a username
    with a dot does, since a workflow named by it could not be referred to.
    """
    label = username.lower()
    try:
        return validate_name(label)
    except InvalidName:
        raise InvalidName(
            f"Your username {username!r} cannot name a workflow: an owner is lower case "
            "letters, digits, hyphens and underscores. Make it under an org instead."
        ) from None
