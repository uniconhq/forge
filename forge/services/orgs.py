"""Making an org at the forge: the org itself, its roles, and the labels its
threads are marked with, as three recorded provisioning steps. A rerun after
a failure picks up where the record says it stopped, and an org the first
try made before its record caught up is taken as made rather than refused.
"""

from forge.domain.errors import Conflict
from forge.domain.ids import OrgName
from forge.domain.names import validate_name
from forge.runtime.actions import action
from forge.runtime.context import Context
from forge.services import provisioning
from forge.services.provisioning import Attempt, Record, Step

KIND = "org"


@action
async def provision(ctx: Context, name: OrgName, *, description: str) -> Record:
    """Make the org with everything an org needs. `Conflict` when the name is
    taken by something this record did not make.
    """
    validate_name(name)

    async def make_org(attempt: Attempt) -> None:
        try:
            await ctx.forge.orgs.create_org(name, description=description)
        except Conflict:
            if not attempt.is_rerun:
                raise

    async def make_roles(attempt: Attempt) -> None:
        await ctx.forge.orgs.create_roles(name)

    async def make_labels(attempt: Attempt) -> None:
        await ctx.forge.orgs.create_thread_labels(name)

    return await provisioning.run(
        ctx,
        KIND,
        name,
        [Step("org", make_org), Step("roles", make_roles), Step("labels", make_labels)],
    )
