# What a git host must provide

The port in `forge/port/` is written in the platform's words. Any host that
sits behind it has to provide the capabilities below. The list is also what
fixes the database boundary: anything not on it is platform state, held in the
package's own tables, and stays there when the host changes.

| Requirement | Forgejo | GitLab |
|---|---|---|
| An identity provider the platform signs users in through, returning a credential to act as them | OpenID Connect with PKCE; OAuth2 access and refresh tokens | OpenID Connect with PKCE; OAuth2 access and refresh tokens |
| Scoped roles that inherit downward: a role at an org reaches every contest and task in it | Teams, one per role per scope; an org-level team covers every repository, new ones included | Group members with inherited access; a subgroup per contest, a project per task |
| Per-workspace write access for one contestant or one team's members | Collaborators on the workspace repositories | Project members with Developer access |
| Protected versions only the platform's account may create | Protected tags on `published/*` and `submission/*`, allowed for `unicon-backend` only | Protected tags by role, emulated: the platform's account is the only Maintainer, so it alone may create them |
| File writes with a conflict check on the previous version | Contents API with the blob SHA the write started from | Commits API with `last_commit_id` |
| History and rollback | Git log; a rollback is a new commit | Commits list; a rollback is a new commit |
| Threads with labels and comments for announcements and clarifications | Issues with labels and comments | Issues with labels and notes |
| Big files | Git LFS with the object store behind it | Git LFS with the object store behind it |
| A searchable mark on a workflow, with stars | Topics `unicon-workflow` and `unicon-primitive`; stars | Project topics; stars |
| A copy of a workflow from a version, without a fork relationship | A new repository created from the source tree at that version | A new project created from the source tree at that version |
| Event push to the platform | One org-level webhook, signed, allowed only to the backend's hostname | Group webhooks are a paid feature, emulated with a per-project webhook created with each project |
| A CI that asks the platform for each run's steps, runs pinned images with variables, lends the registering account's credential to the checkout only, scopes its machines to an org on the server side, and pins a run to one machine by label | Woodpecker with the configuration extension set exclusive, org agents, agent labels, trusted clone image | GitLab CI cannot ask a service for a run's steps; the platform would drive Woodpecker against GitLab instead |
| Accounts that can be deactivated reversibly and deleted with what they own | Admin API: `active` flag; delete with purge | Admin API: block and unblock; delete with hard delete |

The Forgejo implementation is `forgejo/`. The in-memory implementation for
tests is `fake/`. `cached.py` wraps either in the small per-read cache.
