from enum import StrEnum
from typing import Annotated

from pydantic import Field, PlainValidator, model_validator

from forge.domain.definitions.basics import Approval, Count, Domain, _pattern
from forge.domain.yaml_models import AwareTime, Line, Model, Problems, already_listed, fail


class Locks(StrEnum):
    """When an entrant's roster locks: at the contest's start, or at the
    entrant's first submission.
    """

    START = "start"
    FIRST_SUBMISSION = "first_submission"


class InviteWay(Model):
    """`invite`: the people an organiser invited to the contest, who type
    nothing. Approved straight away unless `approval` says an organiser
    approves them, since an organiser who invited someone chose them.
    """

    approval: Approval = Approval.AUTO


class SignupWay(Model):
    """`signup`: people who register themselves, under every condition it
    gives: the `code`, and one confirmed address that is at one of the
    `domains` and matches `email_pattern`. One with no condition admits
    anyone with an account. `name` is shown to organisers beside each person
    who came in by it. An organiser approves each one unless `approval` says
    otherwise.
    """

    name: Line | None = None
    code: Line | None = None
    domains: Annotated[tuple[Domain, ...], Field(min_length=1)] | None = None
    email_pattern: Annotated[str, PlainValidator(_pattern)] | None = None
    approval: Approval = Approval.MANUAL

    @model_validator(mode="after")
    def _check(self) -> SignupWay:
        problems = Problems()
        domains = list(self.domains or ())
        for index, domain in enumerate(domains):
            if domain in domains[:index]:
                problems.add(("domains", index), already_listed(domain))
        problems.raise_any()
        return self

    @property
    def asks_address(self) -> bool:
        """Whether it takes only some addresses."""
        return self.domains is not None or self.email_pattern is not None


class Way(Model):
    """One way into the contest: exactly one of `invite` and `signup`."""

    invite: InviteWay | None = None
    signup: SignupWay | None = None

    @model_validator(mode="before")
    @classmethod
    def _bare(cls, value: object) -> object:
        """A way written with nothing under its key, `- invite:`, is that
        way with every default.
        """
        if isinstance(value, dict):
            return {key: {} if found is None else found for key, found in value.items()}
        return value

    @model_validator(mode="after")
    def _one(self) -> Way:
        if (self.invite is None) == (self.signup is None):
            fail([((), "A way in is either an invite or a sign-up. Choose one of the two.")])
        return self

    @property
    def approval(self) -> Approval:
        if self.invite is not None:
            return self.invite.approval
        assert self.signup is not None
        return self.signup.approval

    @property
    def name(self) -> str | None:
        return self.signup.name if self.signup is not None else None


DEFAULT_WAYS = (Way(signup=SignupWay()),)
"""The ways of a contest that lists none: anyone with an account signs up,
and an organiser approves each one."""


class Registration(Model):
    """Who may enter and how: when the window is open, from `opens` until
    `closes`, always without them; at most `capacity` entrants pending or
    approved, no cap without it; and the `ways` someone qualifies by, any one
    enough, by default one sign-up that admits anyone with an account and
    waits for an organiser's approval. With no ways at all nobody may
    register.
    """

    opens: AwareTime | None = None
    closes: AwareTime | None = None
    capacity: Count | None = None
    ways: tuple[Way, ...] = DEFAULT_WAYS

    @model_validator(mode="after")
    def _check(self) -> Registration:
        problems = Problems()
        invites = [index for index, way in enumerate(self.ways) if way.invite is not None]
        for index in invites[1:]:
            problems.add(
                ("ways", index, "invite"),
                "A contest has only one way in by invite. Remove this one.",
            )
        seen: set[str] = set()
        for index, way in enumerate(self.ways):
            if way.name is None:
                continue
            if way.name in seen:
                problems.add(
                    ("ways", index, "signup", "name"),
                    f"Another sign-up is already called {way.name!r}. Give this one another name.",
                )
            seen.add(way.name)
        problems.raise_any()
        return self

    @property
    def invite_way(self) -> int | None:
        """The index of the contest's invite way, or none."""
        return next((index for index, way in enumerate(self.ways) if way.invite is not None), None)


class Entrants(Model):
    """Who an entrant is: from `fewest` to `most` people, one and one by
    default, so everyone enters alone; and when its roster `locks`, at the
    contest's start by default.
    """

    most: Count = 1
    fewest: Count = 1
    locks: Locks = Locks.START

    @model_validator(mode="after")
    def _check(self) -> Entrants:
        if self.fewest > self.most:
            fail(
                [
                    (
                        ("fewest",),
                        f"This must be at most {self.most}, the most people in a team.",
                    )
                ]
            )
        return self
