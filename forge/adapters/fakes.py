"""The fakes joined into the one `Forge` tests use: the git host in memory,
with the object store and the mail server in memory beside it.
"""

from datetime import timedelta

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
        super().__init__(
            public_url=public_url,
            sign_in_redirect_uri=sign_in_redirect_uri,
            clock=clock,
            ci_login_lifetime=ci_login_lifetime,
            ci_asks=ci_asks,
        )
        self.objects = FakeObjects(self.state.clock)
        self.mail = FakeMail()
