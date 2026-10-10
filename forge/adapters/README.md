# The adapters behind the port

The port in `forge/port/` is written in the platform's words. Behind it sit
four services outside the platform, each filled by an adapter of its own,
grouped by what the service is:

```
git/       the git host: forgejo/, and fake/ for tests
ci/        the CI: woodpecker/, and fake.py for tests; host.py is what a CI
           needs from the git host
objects/   the store run logs are kept in: s3.py, fake.py
mail/      the mail server invite mail goes through: smtp.py, fake.py
```

`build` in `__init__.py` picks one of each from the settings, `UNICON_FORGE`
for the git host and `UNICON_CI` for the CI, hands the CI the git host's
`CiHost`, and joins them into the one `Forge` the services see
(`JoinedForge`). `fakes.py` joins the fakes the same way, in one shared world
(`fake_world.py`): one clock, one log of every call, one outage switch.
`cached.py` puts a small per-read cache in front. Every git host names
repositories and builds and reads ids with the one grammar in `ids.py`, since
the platform names every repository itself; `http.py` is the retrying client
the adapters that speak HTTP share, and `browser.py` the browser a CI's
sign-in walks with.

The groups never import each other, and none imports `services` or `db`; the
import-linter contracts in `pyproject.toml` fail the build otherwise. A CI
reaches the git host only through `CiHost`, so any git host can be paired
with any CI that needs no more than it.

The lists below are also what fix the database boundary: anything not on
them is platform state, held in the package's own tables, and stays there
when a service changes.

## What a git host must provide

| Requirement | Forgejo | GitLab |
|---|---|---|
| An identity provider the platform signs users in through, returning a credential to act as them | OpenID Connect with PKCE; OAuth2 access and refresh tokens | OpenID Connect with PKCE; OAuth2 access and refresh tokens |
| Scoped roles that inherit downward: a role at an org reaches every contest and task in it, and every role one person holds read in one call, by them or by the platform | Teams, one per role per scope; an org-level team covers every repository, new ones included; a contest's teams are attached to its repository and its tasks', a task's to its own; no organiser's team is a repository admin, since an admin may delete a tag's protection; a person's teams listed by them, or by the platform's account for them with `sudo` | Group members with inherited access; a subgroup per contest, a project per task; a user's memberships from the admin API |
| Places only the platform's account creates, in an org or in a person's own name, which that person then owns | `MAX_CREATION_LIMIT = 0` and no team may create; the site administrator creates in an org, or under a person with `POST /admin/users/{username}/repos` | Projects limit 0 for users; the admin creates a project in a group, or under a user with `POST /projects/user/:user_id` |
| Per-workspace write access for one contestant or one team's members | Collaborators on the workspace repositories | Project members with Developer access |
| Protected versions only the platform's account may create, even against an account with admin rights over the place, each able to carry a short note | Protected tags on `published/*` and `submission/*`, allowed for the platform's account, `UNICON_FORGE_PLATFORM_ACCOUNT`, only; a publication is an annotated tag whose message is its note | Protected tags by role, emulated: the platform's account is the only Maintainer, so it alone may create them; the tag's message is the note |
| File writes with a conflict check on the previous version, several files as one change | Contents API with the blob SHA each file was read at, the multi-file endpoint for a save | Commits API with `last_commit_id` |
| History nobody can rewrite, even with admin rights over the place | A protected `main`, which refuses every force-push | A protected branch with force push off |
| A content token that is the same for the same content, so two versions compare without reading them | The blob SHA | The blob SHA |
| History and rollback | Git log; a rollback is a new commit | Commits list; a rollback is a new commit |
| Threads with labels and comments for announcements and clarifications | Issues with labels and comments | Issues with labels and notes |
| Big files | Git LFS with the object store behind it | Git LFS with the object store behind it |
| A searchable mark on a workflow, with stars, and who reads a workflow changed by the platform's account for a person the host says may write it | Topics `unicon-workflow` and `unicon-primitive`, set by the platform; stars; visibility and read collaborators set by the platform after the person's own read of the repository shows `push` | Project topics; stars; visibility and Reporter members set by the admin |
| A copy of a workflow from a version, without a fork relationship | A new repository created from the source tree at that version | A new project created from the source tree at that version |
| Event push to the platform | One org-level webhook, signed, allowed only to the backend's hostname | Group webhooks are a paid feature, emulated with a per-project webhook created with each project |
| Accounts that can be deactivated reversibly and deleted with what they own | Admin API: `active` flag; delete with purge | Admin API: block and unblock; delete with hard delete |

## What a CI must provide

| Requirement | Woodpecker | GitLab CI |
|---|---|---|
| A CI the grading port sits in front of. What a run runs comes from the platform's records, never from the request or the repository: a CI that asks for a run's steps signs the question and is answered only for a grading of that task being started, and only when every variable of the run is exactly one the platform started it with; a CI that is pushed to is handed the steps at the start. It meets the runner's machine contract (the runner's README, "The machine contract"): the org's clone credential only in the checkouts and reading only that org's repositories, a run only on a machine carrying its label, the harness container fixed, and both checkouts in one volume. It says where a run is (waiting, taken, finished or lost) and cancels one. What an org's account holds there is one value its implementation owns and says when to refresh, which the platform keeps encrypted and refreshes one caller at a time | Woodpecker, `UNICON_CI=woodpecker`: the configuration extension set exclusive and signed (RFC 9421, ed25519, `ci/woodpecker/signatures.py`), answered for a queued grading of the task and refused unless the variables match exactly; the trusted clone image; org agents and agent labels; the queue for where a run is, since a run Woodpecker dropped still reads as pending; the account's CI user and a token from an unattended sign-in through the git host, signed in again at two thirds of the session's hard lifetime | GitLab CI cannot ask a service for a run's steps; it would be pushed to, with the pipeline file where only the platform may write it, or the platform would drive Woodpecker against GitLab instead |

## What a CI needs from the git host

`ci/host.py` defines `CiHost`, the one way into the git host a CI has, and
the git host's adapter answers it:

| `CiHost` | What for | Forgejo's answer |
|---|---|---|
| `account(id)` | finding the org's service account by the id it was made with, never by its name | the user by id |
| `set_password(id, password)` | a fresh sign-in for that account | the admin API |
| `repo_id(owner, repo)` | activating a task's repository at the CI | the repository's id |
| `remove_hooks(owner, repo, url_prefix)` | deleting the webhooks the CI left on a repository | the hooks API |
| `default_branch` | the branch a run starts on | `main` |
| `web_sign_in` (`web_address`, `sign_in`, `approve_consent`) | a CI that admits only people who signed in through the git host | the sign-in form and the consent form |

The sign-in part differs for every git host, since each has its own pages:
a GitLab adapter would write its own once, and every CI that signs people in
through it would use that. A CI that keeps its own accounts uses none of it.

## The object store and the mail server

The store holds each grading run's log, written by the machine through a
presigned URL and read back bounded (`objects/s3.py`, any S3 store; Garage
in the deployment). The mail server sends invite mail over SMTP
(`mail/smtp.py`); without one, nothing is mailed.
