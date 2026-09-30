"""The port in memory, for tests. It implements every area with no network,
records every call with the identity it was made under, and refuses the three
things a real forge refuses: a write whose conflict check is stale, a
protected version created by anything but the platform, and a read the user
has no access to. Its object store is in memory too, and refuses what a
real store refuses.
"""

from typing import Any

from forge.domain.clock import Clock
from forge.domain.identity import Credential, User
from forge.forges.fake.content import FakeContent
from forge.forges.fake.grading import FakeComputes, FakeGrading
from forge.forges.fake.identity import FakeIdentity
from forge.forges.fake.objects import FakeObjects
from forge.forges.fake.orgs import FakeOrgs
from forge.forges.fake.state import Call, State
from forge.forges.fake.threads import FakeThreads
from forge.forges.fake.workflows import FakePrimitives, FakeWorkflows
from forge.forges.fake.workspaces import FakeWorkspaces

__all__ = ["Call", "FakeForge", "State"]


class FakeForge:
    def __init__(
        self,
        *,
        public_url: str = "http://forge.test",
        sign_in_redirect_uri: str = "http://app.test/api/v1/auth/callback",
        clock: Clock | None = None,
    ) -> None:
        self.state = State(clock)
        self.identity = FakeIdentity(
            self.state, public_url=public_url, redirect_uri=sign_in_redirect_uri
        )
        self.orgs = FakeOrgs(self.state)
        self.content = FakeContent(self.state)
        self.grading = FakeGrading(self.state)
        self.workspaces = FakeWorkspaces(self.state)
        self.threads = FakeThreads(self.state)
        self.workflows = FakeWorkflows(self.state)
        self.primitives = FakePrimitives(self.state)
        self.computes = FakeComputes(self.state)
        self.objects = FakeObjects(self.state.clock)

    @property
    def name(self) -> str:
        return "fake"

    async def aclose(self) -> None:
        return None

    def add_user(self, user_id: int, username: str, **fields: Any) -> User:
        """A person at the fake. An id already taken, by a test's user or by
        an account the package made, is refused, so a test never replaces
        one without knowing.
        """
        if user_id in self.state.users:
            raise ValueError(f"the fake already has a user with id {user_id}")
        user = User(id=user_id, username=username, **fields)
        self.state.users[user_id] = user
        return user

    def mint(self, user_id: int) -> Credential:
        """A credential for the user, as a completed sign-in would yield."""
        return self.state.mint(user_id)

    def consent_redirect(self, sign_in_url: str) -> str:
        return self.identity.consent_redirect(sign_in_url)

    @property
    def calls(self) -> list[Call]:
        return self.state.calls

    def calls_to(self, operation: str) -> list[Call]:
        return [call for call in self.state.calls if call.operation == operation]

    def reset_calls(self) -> None:
        self.state.calls.clear()

    @property
    def users(self) -> dict[int, User]:
        return self.state.users

    @property
    def signed_in_user_id(self) -> int:
        return self.state.signed_in_user_id

    @signed_in_user_id.setter
    def signed_in_user_id(self, user_id: int) -> None:
        self.state.signed_in_user_id = user_id

    @property
    def unavailable(self) -> bool:
        return self.state.unavailable

    @unavailable.setter
    def unavailable(self, value: bool) -> None:
        self.state.unavailable = value

    @property
    def refuse_refresh(self) -> bool:
        return self.state.refuse_refresh

    @refuse_refresh.setter
    def refuse_refresh(self, value: bool) -> None:
        self.state.refuse_refresh = value

    @property
    def refreshes(self) -> int:
        return self.state.refreshes

    @property
    def racing_submissions(self) -> int:
        """How many submissions made elsewhere take the next number first,
        so the next record of a submission collides that many times.
        """
        return self.state.racing_submissions

    @racing_submissions.setter
    def racing_submissions(self, count: int) -> None:
        self.state.racing_submissions = count

    @property
    def lose_submission_answer(self) -> bool:
        """Whether the next record of a submission names it and then fails as
        if its answer were lost on the way back.
        """
        return self.state.lose_submission_answer

    @lose_submission_answer.setter
    def lose_submission_answer(self, value: bool) -> None:
        self.state.lose_submission_answer = value

    @property
    def ci_dead(self) -> set[str]:
        """The usernames whose CI sign-in has gone: the CI no longer answers
        their token until the sign-in dance is run for them again.
        """
        return self.state.ci_dead
