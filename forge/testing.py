"""What a test needs to run the package: a Postgres of its own, migrated, the
in-memory forge with two users, a clock a test can move, and a setup and a
context over them. Load it as a pytest plugin, with `-p forge.testing` in the
pytest configuration or `pytest_plugins = ["forge.testing"]` in a conftest.
`UNICON_TEST_DATABASE_URL` names a server the tests may create databases on;
without it every test that needs one is skipped.

A test calls an action with `ctx` to run it inside the test's unit of work,
or with `setup` to have it open and commit one of its own. The setup files
every org, contest and task a test names under its name, so the ids a test
writes down read as the names it made things with, `acme/spring/sum`; a
deployment files them under random keys. A test of the difference asks for
`setup_with_random_keys`, a setup over the same forge and database that
makes random keys as a deployment does, or `held_setup_with_random_keys`
for a dependant's. A dependant that
calls actions with neither, as the backend does, asks for `held_setup`, which
makes `setup` the one forge holds for the test and lets go of it afterwards.

A dependant imports `forge.api` and this module and nothing else of the
package, so this module also re-exports what a dependant's test needs to
arrange the fake: `FakeForge`, `Settings` for a settings override, and the
domain types a test builds, `OrgId`, `AsUser` and `Visibility`, and `Setup`
to type the setup a fixture hands over. `logged` reads back the records a
test caused, in the shape they are written. `register_contestant` writes
the row that makes someone a contestant, for a test of what registration
decides; `name_places` gives the org,
contest and task ids a test made straight at the fake the names a route
finds them by, each its own last part; and `seed_classic` puts the
built-in workflow `unicon/classic@v2` at the fake the way deploy's bootstrap
puts it at a real forge, from `CLASSIC`, a copy of that file,
`deploy/workflows/classic/v2/workflow.yaml`. The package's own tests check the
two agree when the deploy repo is checked out beside this one.
"""

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from forge.adapters.fakes import FakeForge
from forge.db.migrations import upgrade_to_head
from forge.db.tables import Contestant, Name, metadata
from forge.domain.clock import FakeClock
from forge.domain.identity import PLATFORM, AsUser
from forge.domain.ids import ContestId, OrgId
from forge.domain.keys import key_from_name, random_key
from forge.domain.workflows import Visibility
from forge.log import JsonFormatter
from forge.runtime.context import Context
from forge.runtime.held import hold, release
from forge.runtime.setup import Setup
from forge.settings import Settings

__all__ = [
    "APP_URL",
    "CALLBACK",
    "CALLBACK_PATH",
    "CLASSIC",
    "FORGE_URL",
    "PLACEHOLDER_DIGEST",
    "PRIMITIVES",
    "AsUser",
    "FakeClock",
    "FakeForge",
    "OrgId",
    "Settings",
    "Setup",
    "Visibility",
    "logged",
    "name_places",
    "register_contestant",
    "seed_classic",
    "seed_primitives",
]

SERVER_URL_VARIABLE = "UNICON_TEST_DATABASE_URL"
APP_URL = "http://app.test"
FORGE_URL = "http://forge.test"
CALLBACK_PATH = "/api/v1/auth/callback"
CALLBACK = f"{APP_URL}{CALLBACK_PATH}"

_FORMATTER = JsonFormatter()

CLASSIC = b"""\
# unicon/classic@v2, the built-in workflow for a task judged by comparing
# output with an answer: compile the submission once, run the program on
# every test under the task's limits, and compare each run's output with
# that test's answer. Seeded by `uv run bootstrap` into the repository
# unicon/classic.workflow at the forge, at the tag v2. The format is
# TASK-FORMAT.md section 1.3.
inputs:
  submission: {type: file, contestant: true}
  language: {type: enum, options: [c, cpp, java, python], contestant: true}
  time_limit: number
  memory_limit: number
test:
  input: file
  answer: file
steps:
  - id: compile
    use: unicon/compile@v2
    with:
      source: ${{ inputs.submission }}
      language: ${{ inputs.language }}
  - id: run
    use: unicon/sandbox-run@v2
    per_test: true
    with:
      binary: ${{ steps.compile.binary }}
      input: ${{ test.input }}
      time_limit: ${{ inputs.time_limit }}
      memory_limit: ${{ inputs.memory_limit }}
  - id: check
    use: unicon/diff-check@v2
    per_test: true
    with:
      actual: ${{ steps.run.output }}
      expected: ${{ test.answer }}
report:
  time_ms: {from: "${{ steps.run.time_ms }}", fold: max, better: lower, at_least: 0}
  memory_kb: {from: "${{ steps.run.memory_kb }}", fold: max, better: lower, at_least: 0}
  log: ${{ steps.compile.compile_log }}
"""

PLACEHOLDER_DIGEST = "sha256:" + "0" * 64
"""The digest the fake's primitives name. Bootstrap writes each primitive's
real digest from the release manifest; nothing in a test runs an image."""


def _declared(name: str, rest: str) -> bytes:
    """A primitive's declaration under the platform's org, its image named by
    the placeholder digest on the first line, where bootstrap writes it.
    """
    return (f"image: ghcr.io/uniconhq/primitive-{name}@{PLACEHOLDER_DIGEST}\n" + rest).encode()


PRIMITIVES: dict[str, bytes] = {
    "compile": _declared(
        "compile",
        """\
batch: false
network: false
limits: {time_ms: 60000, cpu_ms: 60000, memory_mb: 1024, pids: 128, output_mb: 64, gpus: 0}
inputs:
  source: {type: folder, runs: true}
  language: {type: enum, options: [c, cpp, java, python]}
  entry: {type: text, optional: true}
outputs:
  binary: {type: file}
  compile_log: {type: text}
  outcome: {type: outcome}
""",
    ),
    "sandbox-run": _declared(
        "sandbox-run",
        """\
batch: true
network: false
limits: {time_ms: 2000, cpu_ms: 2000, memory_mb: 256, pids: 128, output_mb: 64, gpus: 0}
limits_from:
  time_ms: {input: time_limit, scale: 2000, add: 3000}
  cpu_ms: {input: time_limit, scale: 2000, add: 3000}
  memory_mb: {input: memory_limit, add: 256}
inputs:
  binary: {type: file, runs: true}
  input: {type: file, runs: false}
  args: {type: text, optional: true}
  time_limit: {type: number}
  memory_limit: {type: number}
outputs:
  output: {type: file}
  time_ms: {type: number}
  memory_kb: {type: number}
  outcome: {type: outcome}
""",
    ),
    "diff-check": _declared(
        "diff-check",
        """\
batch: true
network: false
limits: {time_ms: 2000, cpu_ms: 2000, memory_mb: 256, pids: 32, output_mb: 1, gpus: 0}
inputs:
  actual: {type: file, runs: false}
  expected: {type: file, runs: false}
outputs:
  outcome: {type: outcome}
""",
    ),
}
"""The declarations of the three primitives the built-in workflow uses, at
`v2`: each primitive repo's own `primitive.yaml` as it is, with the `image`
line deploy's bootstrap writes at its top, carrying a placeholder digest in
place of the image's own. The package's own tests check each against its
repo's when that is checked out beside this one."""


async def seed_primitives(fake: FakeForge) -> None:
    """`unicon/compile`, `unicon/sandbox-run` and `unicon/diff-check` at `v2`
    at the fake, from `PRIMITIVES`, as bootstrap mirrors them into a real
    forge.
    """
    for name, declaration in PRIMITIVES.items():
        fake.primitives.add(name, {"v2": declaration})


async def seed_classic(fake: FakeForge) -> None:
    """`unicon/classic@v2` public at the fake, and the three primitives it
    uses, as bootstrap makes them at a real forge, so a task's first save
    finds a workflow its organiser can read and every step's image.
    """
    await seed_primitives(fake)
    workflow = await fake.workflows.create_workflow(
        PLATFORM, "unicon", "classic", {"workflow.yaml": CLASSIC}, Visibility.PUBLIC
    )
    await fake.workflows.create_workflow_version(PLATFORM, workflow, "v2")


def logged(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, Any]]:
    """Every record named `event` that `caplog` holds, as the JSON object the
    package writes it as.
    """
    return [
        json.loads(_FORMATTER.format(record))
        for record in caplog.records
        if record.getMessage() == event
    ]


async def register_contestant(
    setup: Setup,
    contest: ContestId,
    user_id: int,
    *,
    status: str = "approved",
    time_extension: timedelta = timedelta(0),
) -> None:
    """Make the user a contestant of the contest, with `status` and their own
    time extension, committed at once.
    """
    async with setup.unit_of_work() as ctx:
        ctx.db.add(
            Contestant(
                contest_id=contest,
                user_id=user_id,
                status=status,
                registered_at=ctx.now,
                time_extension_seconds=int(time_extension.total_seconds()),
            )
        )


async def name_places(setup: Setup, *ids: str) -> None:
    """Name each org, contest or task id after its last part, committed at
    once, as a create would have when it filed the thing under its name.
    """
    kinds = {1: "org", 2: "contest", 3: "task"}
    async with setup.unit_of_work() as ctx:
        for place in ids:
            parent, _, name = place.rpartition("/")
            ctx.db.add(Name(id=place, kind=kinds[place.count("/") + 1], parent=parent, name=name))


def _server_url() -> str:
    url = os.environ.get(SERVER_URL_VARIABLE)
    if not url:
        pytest.skip(f"{SERVER_URL_VARIABLE} is not set")
    return url


def _connect(url: str) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True)


@pytest.fixture
def database_url() -> Iterator[str]:
    """An empty database of its own, dropped when the test ends."""
    server_url = _server_url()
    name = _create(server_url)
    try:
        yield _database(server_url, name)
    finally:
        _drop(server_url, name)


@pytest.fixture(scope="session")
def migrated_template() -> str:
    """A database at the latest migration that this process's databases are
    copied from, since copying one takes a fraction of the time migrating
    one does. It is kept between runs under a name carrying a hash of the
    migrations, so it is built again only once a migration changes, and
    the ones a changed migration left behind are dropped. Each process of a
    parallel run has its own, so none waits on another's.
    """
    server_url = _server_url()
    prefix = f"unicon_template_{_worker()}_"
    name = prefix + _schema_mark()
    with _connect(server_url) as connection:
        # Another run on the same server, with the same process name, may be
        # building it: the second waits, then finds it built.
        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (prefix,))
        _drop_others(connection, prefix, keep=name)
        if _exists(connection, name):
            return name
        # Built under a name of its own and renamed once migrated, so a run
        # stopped halfway leaves nothing that looks finished.
        building = name + "_building"
        connection.execute(f'DROP DATABASE IF EXISTS "{building}" WITH (FORCE)')
        connection.execute(f'CREATE DATABASE "{building}"')
        upgrade_to_head(_database(server_url, building))
        # A database is renamed or copied only while nothing is connected.
        _disconnect(connection, building)
        connection.execute(f'ALTER DATABASE "{building}" RENAME TO "{name}"')
        connection.execute(f'ALTER DATABASE "{name}" WITH IS_TEMPLATE true')
    return name


@pytest.fixture(scope="session")
def process_database(migrated_template: str) -> Iterator[ProcessDatabase]:
    """This process's database at the latest migration, which every test
    that needs one shares, emptied before the first and after each, and the
    one connection that empties it, open for the run, since opening one
    costs more than the emptying does. Like the template it is kept between
    runs, since dropping a database on Windows waits for a checkpoint.
    """
    server_url = _server_url()
    prefix = f"unicon_shared_{_worker()}_"
    name = prefix + _schema_mark()
    with _connect(server_url) as connection:
        # Held for the run, so another run on the same server never empties
        # it under this one's tests; that run makes one of its own instead.
        taken = connection.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (prefix,))
        if taken.fetchone() != (True,):
            name = _create(server_url, template=migrated_template)
            try:
                with _connect(_database(server_url, name)) as cleaner:
                    yield ProcessDatabase(_database(server_url, name), cleaner)
            finally:
                _drop(server_url, name)
            return
        _drop_others(connection, prefix, keep=name)
        if not _exists(connection, name):
            connection.execute(f'CREATE DATABASE "{name}" TEMPLATE "{migrated_template}"')
        with _connect(_database(server_url, name)) as cleaner:
            shared = ProcessDatabase(_database(server_url, name), cleaner)
            shared.empty()
            yield shared


@dataclass(frozen=True)
class ProcessDatabase:
    url: str
    cleaner: psycopg.Connection[tuple[object, ...]]

    def empty(self) -> None:
        """Let go of whatever a test left connected, then delete every row
        of every table the package owns, those that point at others first.
        Deleting the few rows a test leaves touches no file, where a
        truncate makes every table's files again.
        """
        self.cleaner.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
        with self.cleaner.transaction():
            for table in reversed(metadata.sorted_tables):
                self.cleaner.execute(f'DELETE FROM "{table.name}"')


@pytest.fixture
def migrated_database_url(process_database: ProcessDatabase) -> Iterator[str]:
    """A database at the latest migration with nothing in it, the process's
    own, emptied again when the test ends. A test that moves the schema
    takes `private_migrated_database_url` instead.
    """
    yield process_database.url
    process_database.empty()


@pytest.fixture
def private_migrated_database_url(migrated_template: str) -> Iterator[str]:
    """A database of its own at the latest migration, copied from the
    template and dropped when the test ends, for a test that moves the
    schema, which the shared one must never be.
    """
    server_url = _server_url()
    name = _create(server_url, template=migrated_template)
    try:
        yield _database(server_url, name)
    finally:
        _drop(server_url, name)


def _create(server_url: str, *, template: str | None = None) -> str:
    name = f"unicon_test_{_worker()}_{uuid.uuid4().hex[:8]}"
    copied = f' TEMPLATE "{template}"' if template is not None else ""
    with _connect(server_url) as connection:
        connection.execute(f'CREATE DATABASE "{name}"{copied}')
    return name


def _drop(server_url: str, name: str) -> None:
    with _connect(server_url) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _disconnect(connection: psycopg.Connection[tuple[object, ...]], name: str) -> None:
    connection.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
        " WHERE datname = %s AND pid <> pg_backend_pid()",
        (name,),
    )


def _worker() -> str:
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


def _schema_mark() -> str:
    """A short hash of every migration, which names the databases kept
    between runs.
    """
    alembic = Path(upgrade_to_head.__code__.co_filename).parent / "alembic"
    digest = hashlib.sha256()
    for path in sorted(alembic.rglob("*.py")):
        digest.update(path.relative_to(alembic).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _exists(connection: psycopg.Connection[tuple[object, ...]], name: str) -> bool:
    found = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    return found.fetchone() is not None


def _drop_others(
    connection: psycopg.Connection[tuple[object, ...]], prefix: str, *, keep: str
) -> None:
    """Drop the databases under `prefix` but `keep`: ones a changed migration
    left behind, or a run stopped while building one.
    """
    found = connection.execute(
        "SELECT datname FROM pg_database WHERE starts_with(datname, %s)", (prefix,)
    )
    for (name,) in found.fetchall():
        if name != keep:
            connection.execute(f'ALTER DATABASE "{name}" WITH IS_TEMPLATE false')
            connection.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _database(server_url: str, name: str) -> str:
    return server_url.rsplit("/", 1)[0] + "/" + name


@pytest.fixture
def settings(migrated_database_url: str) -> Settings:
    return Settings.for_tests(
        database_url=migrated_database_url, public_url=APP_URL, forge_public_url=FORGE_URL
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(clock: FakeClock) -> FakeForge:
    """The in-memory forge with two users, `ada` (7) signed in and `bob` (8)."""
    forge = FakeForge(public_url=FORGE_URL, sign_in_redirect_uri=CALLBACK, clock=clock)
    forge.add_user(7, "ada", name="Ada Lovelace", email="ada@example.test")
    forge.add_user(8, "bob")
    forge.signed_in_user_id = 7
    return forge


@pytest.fixture
async def setup(settings: Settings, fake: FakeForge, clock: FakeClock) -> AsyncIterator[Setup]:
    built = Setup.build(
        settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock, keys=key_from_name
    )
    try:
        yield built
    finally:
        await built.stop()


@pytest.fixture
async def setup_with_random_keys(
    settings: Settings, fake: FakeForge, clock: FakeClock
) -> AsyncIterator[Setup]:
    """A setup that files what it names under random keys, as a deployment
    does, so a test can tell a name from the key it is filed under.
    """
    built = Setup.build(
        settings, callback_path=CALLBACK_PATH, forge=fake, clock=clock, keys=random_key
    )
    try:
        yield built
    finally:
        await built.stop()


@pytest.fixture
def held_setup(setup: Setup) -> Iterator[Setup]:
    """`setup`, held as the process's own for the test, so an action called
    with no setup uses it. Let go when the test ends, passed or failed.
    """
    hold(setup)
    try:
        yield setup
    finally:
        release()


@pytest.fixture
def held_setup_with_random_keys(setup_with_random_keys: Setup) -> Iterator[Setup]:
    """`setup_with_random_keys`, held as the process's own for the test, as
    `held_setup` holds `setup`.
    """
    hold(setup_with_random_keys)
    try:
        yield setup_with_random_keys
    finally:
        release()


@pytest.fixture
async def ctx(setup: Setup) -> AsyncIterator[Context]:
    """One unit of work, committed when the test ends."""
    async with setup.unit_of_work() as context:
        yield context
