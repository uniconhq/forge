"""The fakes joined into the one `Forge` tests use: the git host in memory,
the CI in memory paired with it through its `CiHost`, and the object store
and the mail server in memory beside them, all in one world, so one call log
holds every call in order. A test reads and turns the CI's records as
`ci`, and the git host's as `state`.
"""

from datetime import timedelta

from forge.adapters.ci.fake import FakeCi
from forge.adapters.fake_world import FakeWorld
from forge.adapters.git.fake import FakeGitHost
from forge.adapters.mail.fake import FakeMail
from forge.adapters.objects.fake import FakeObjects
from forge.domain.clock import Clock


class FakeForge(FakeGitHost):
    def __init__(
        self,
        *,
        public_url: str = "http://forge.test",
        sign_in_redirect_uri: str = "http://app.test/api/v1/auth/callback",
        clock: Clock | None = None,
        ci_login_lifetime: timedelta = timedelta(days=30),
        ci_asks: bool = True,
    ) -> None:
        world = FakeWorld(clock)
        super().__init__(
            public_url=public_url, sign_in_redirect_uri=sign_in_redirect_uri, world=world
        )
        self.ci = FakeCi(world, self.ci_host, login_lifetime=ci_login_lifetime, asks=ci_asks)
        self.grading = self.ci.grading
        self.computes = self.ci.computes
        self.objects = FakeObjects(world.clock)
        self.mail = FakeMail()
