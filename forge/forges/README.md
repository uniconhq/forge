# What a git host must provide

The port in `forge/port/` is written in the platform's words. Any host that
sits behind it has to provide the capabilities below. The list is also what
fixes the database boundary: anything not on it is platform state, held in the
package's own tables, and stays there when the host changes.

| Requirement | Forgejo | GitLab |
|---|---|---|
| An identity provider the platform signs users in through, returning a credential to act as them | OpenID Connect with PKCE; OAuth2 access and refresh tokens | OpenID Connect with PKCE; OAuth2 access and refresh tokens |
| Scoped roles that inherit downward: a role at an org reaches every contest and task in it, and every role one person holds read in one call, by them or by the platform | Teams, one per role per scope; an org-level team covers every repository, new ones included; a contest's teams are attached to its repository and its tasks', a task's to its own; no organiser's team is a repository admin, since an admin may delete a tag's protection; a person's teams listed by them, or by the platform's account for them with `sudo` | Group members with inherited access; a subgroup per contest, a project per task; a user's memberships from the admin API |
| Per-workspace write access for one contestant or one team's members | Collaborators on the workspace repositories | Project members with Developer access |
| Protected versions only the platform's account may create, even against an account with admin rights over the place, each able to carry a short note | Protected tags on `published/*` and `submission/*`, allowed for the platform's account, `UNICON_FORGE_PLATFORM_ACCOUNT`, only; a publication is an annotated tag whose message is its note | Protected tags by role, emulated: the platform's account is the only Maintainer, so it alone may create them; the tag's message is the note |
| File writes with a conflict check on the previous version, several files as one change | Contents API with the blob SHA each file was read at, the multi-file endpoint for a save | Commits API with `last_commit_id` |
| History nobody can rewrite, even with admin rights over the place | A protected `main`, which refuses every force-push | A protected branch with force push off |
| A content token that is the same for the same content, so two versions compare without reading them | The blob SHA | The blob SHA |
| History and rollback | Git log; a rollback is a new commit | Commits list; a rollback is a new commit |
| Threads with labels and comments for announcements and clarifications | Issues with labels and comments | Issues with labels and notes |
| Big files | Git LFS with the object store behind it | Git LFS with the object store behind it |
| A searchable mark on a workflow, with stars | Topics `unicon-workflow` and `unicon-primitive`; stars | Project topics; stars |
| A copy of a workflow from a version, without a fork relationship | A new repository created from the source tree at that version | A new project created from the source tree at that version |
| Event push to the platform | One org-level webhook, signed, allowed only to the backend's hostname | Group webhooks are a paid feature, emulated with a per-project webhook created with each project |
| A CI that asks the platform for each run's steps, runs pinned images with variables, lends the activating account's credential to the checkout only, scopes its machines to an org on the server side, and pins a run to one machine by label | Woodpecker with the configuration extension set exclusive, org agents, agent labels, trusted clone image | GitLab CI cannot ask a service for a run's steps; the platform would drive Woodpecker against GitLab instead |
| Accounts that can be deactivated reversibly and deleted with what they own | Admin API: `active` flag; delete with purge | Admin API: block and unblock; delete with hard delete |

The Forgejo implementation is `forgejo/`. The in-memory implementation for
tests is `fake/`. Both name repositories and build and read ids with the one
grammar in `ids.py`. `cached.py` wraps either in the small per-read cache, and
`build` in `__init__.py` picks one from the settings.
