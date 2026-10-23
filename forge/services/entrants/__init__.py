"""Entrants: the one kind of entry a contest has, one or more people who enter
it together (`forge.domain.entrants` and `forge.domain.registration` hold the
rules). An entrant is an `entrants` row, with a row in `entrant_members` for
each time someone was in it; nothing about it is a forge role. Its
registration, the organisers' decision on it, its extension, its workspace
and its row on a leaderboard are the entrant's, never a member's.

A contestant's actions each need a session, and are refused once the contest
has ended or is archived, since an entrant stands as it is from then on:

1. `register` makes a new entrant with the signed-in person as its first
   member. In this order, each with its own code: the contest is one they
   see, or no such contest; the window is open, and, where entrants need
   more than one person, the roster lock has not passed for a new entrant
   (`registration_closed`), since one that could never grow is no use;
   they hold no role at the contest, its tasks or its org (`is_staff`),
   read under the org's lock on role changes, so a grant and a registration
   of one person never pass each other; they are in no entrant of the
   contest, whatever became of it (`already_registered`); they qualify by
   one of the contest's ways (`invite_required`, `wrong_invite_code`,
   `domain_not_allowed`, or `registration_closed` for a contest with none),
   where invited means holding an organiser's invite they accepted; a place
   is free (`contest_full`), the entrants pending or approved counted under
   an advisory lock on the contest, a lock Postgres holds against a name, so
   the last place goes once; and the name, needed when the contest's
   entrants may be more than one person (`entrant_name_required`), refused
   when they are one each (`entrants_alone`), checked
   (`invalid_entrant_name`) and no other entrant's, ignoring case
   (`entrant_name_taken`).
2. `invite` asks someone into the person's entrant, by username or address,
   as an invite with `grants = member` mailed as an organiser's is, by a
   member of a pending or approved entrant; refused once the roster has
   locked (`roster_locked`), when the members and the invites waiting reach
   the contest's `entrants.most` (`entrant_full`), when entrants are one
   person each (`entrants_alone`), or once the entrant's members have sent
   `MEMBER_DAILY_MAX` in a day, apart from the org's own limit
   (`invite_limit`). Any member withdraws its waiting
   invites (`withdraw_invite`).
3. Accepting an invite (`invites.accept`, which calls `take_place`) puts
   the person in the entrant it names, or, for an organiser's invite to the
   contest, registers them as `register` would, by the contest's invite way,
   without a name when entrants are one person each and named after their
   username otherwise; someone in an entrant already accepts that one
   without anything else changing. Accepting is refused, closing nothing,
   unless the contest is open (`published`) and has not ended or been
   archived (`registration_closed`). Joining is refused when the roster has
   locked or the window is closed, unless an organiser sent the invite;
   when they hold a role (`is_staff`); when the entrant is full
   (`entrant_full`); or when they do not qualify: an organiser's invite
   into an entrant puts them in as an organiser's change, as `move_member`
   does, so it never sends an approved entrant back to pending; one to the
   contest registers them by the invite way (`invite_required` once the
   contest has none); and a member's does neither, so they qualify as
   themselves and may need a code. Someone in another entrant of the contest moves out of it
   only while its roster is open, it has no submissions and it has not been
   turned away (`in_entrant`).
4. `leave` takes the person out before the roster locks (`roster_locked`);
   it is also how someone changes entrant.
5. `rename` gives the entrant a new name, by any member, before the roster
   locks.

Who may reach an entrant's workspace changes in the request that changes
the membership, before the membership is written: someone who joins is given
access to every part already made, and someone who leaves, is taken out or
is moved loses it. A call to the forge that fails fails the request, and
whatever access it gave or took before then is put back to the members as
they stand (`_shared_again`), so a move whose second call fails leaves the
person reaching the entrant they are still in; asking again finishes it,
since giving or taking access twice changes nothing. A part made later is
made with whoever is a member then (`settle`), so a place to submit is
finished only once every member can write it. An entrant left with nobody
and no submissions is deleted, its member rows kept `left`.

An entrant is approved straight away once it is complete and every member
came in by a way that approves straight away, checked whenever someone
joins it or leaves it pending; one who joins an approved entrant by a way an organiser approves
sends it back to `pending`, which can happen only before the roster locks,
so nothing it submitted is held back.

Organisers act on the entrant, whatever its roster's lock and the window:
anyone observing the contest lists them (`list_entrants`), and a manager decides
them: `approve`, refused while the entrant is short of `entrants.fewest`
(`entrant_incomplete`); `reject` with a reason its members read; `reopen` a
rejection, refused like a registration when a member holds a role by now or
every place is taken; `remove`, which takes every member's access away and
keeps what the workspace holds; `extend`, which every deadline check adds;
`organise_rename`; `remove_member`; `move_member` to another entrant, never
leaving behind an entrant whose results would have nobody
(`entrant_has_submissions`); and `delete` one that has no submissions.

A change takes, in this order, the org's lock on role changes when it reads
roles, the invite it acts on, an advisory lock on each person it moves in
the order of their ids, the invites still waiting into an entrant it may
leave empty, which ending it withdraws, the contest's places, then the
entrant rows it touches in the order of their ids, so two changes of one
person or one entrant happen one after another, a removal never passes a
join, and no change holds an entrant while it waits for an invite; a
submit holds its entrant's row for reading, so no change of who is in it
passes the submit.
"""

from forge.services.entrants.contestant_actions import (
    invite,
    leave,
    mine,
    register,
    rename,
    withdraw_invite,
)
from forge.services.entrants.entrant_rows import (
    _add_member,
    _after_join,
    _approve,
    _approve_if_due,
    _close,
    _decide,
    _end,
    _flush_once,
    _free_name,
    _join,
    _make,
    _new_name,
    _refuse_taken,
    _rename,
    _shared_again,
    _take_out,
)
from forge.services.entrants.guards import (
    _decided,
    _emails,
    _emails_or_none,
    _held_own,
    _hold_entrants,
    _hold_invites_into,
    _hold_people,
    _hold_places,
    _managed,
    _own,
    _refuse_fixed,
    _refuse_full,
    _refuse_shut,
    _refuse_staff,
    _refuse_too_many_sent,
    _refuse_window,
    _teams_fixed,
    _waiting,
)
from forge.services.entrants.joining import (
    _by_invite_way,
    _refuse_moving,
    _register_invited,
    forget_person,
    take_place,
)
from forge.services.entrants.organiser_actions import (
    approve,
    delete,
    extend,
    list_entrants,
    move_member,
    organise_rename,
    reject,
    remove,
    remove_member,
    reopen,
)
from forge.services.entrants.queries import (
    approved_contests,
    display_names,
    entrant_ids,
    everyone_in,
    hold_for_reading,
    membership,
    ref,
    standing,
    statuses,
    submitted,
    username_of,
    workspace_of,
    workspaces_of,
)
from forge.services.entrants.records import (
    ADDRESSES_UNREAD,
    ENDED,
    MEMBER_DAILY_MAX,
    NO_SUCH_ENTRANT,
    NOT_IN_ONE,
    PEOPLE_LOCK,
    PLACES_LOCK,
    Entered,
    Entrant,
    Invitation,
    Invited,
    Member,
    Mine,
    Refusal,
    Standing,
)
from forge.services.entrants.views import _invitations, _user, _view, _views, panel
from forge.services.member_lookup import member_ids, of_person, settle

__all__ = [
    "ADDRESSES_UNREAD",
    "ENDED",
    "MEMBER_DAILY_MAX",
    "NOT_IN_ONE",
    "NO_SUCH_ENTRANT",
    "PEOPLE_LOCK",
    "PLACES_LOCK",
    "Entered",
    "Entrant",
    "Invitation",
    "Invited",
    "Member",
    "Mine",
    "Refusal",
    "Standing",
    "_add_member",
    "_after_join",
    "_approve",
    "_approve_if_due",
    "_by_invite_way",
    "_close",
    "_decide",
    "_decided",
    "_emails",
    "_emails_or_none",
    "_end",
    "_flush_once",
    "_free_name",
    "_held_own",
    "_hold_entrants",
    "_hold_invites_into",
    "_hold_people",
    "_hold_places",
    "_invitations",
    "_join",
    "_make",
    "_managed",
    "_new_name",
    "_own",
    "_refuse_fixed",
    "_refuse_full",
    "_refuse_moving",
    "_refuse_shut",
    "_refuse_staff",
    "_refuse_taken",
    "_refuse_too_many_sent",
    "_refuse_window",
    "_register_invited",
    "_rename",
    "_shared_again",
    "_take_out",
    "_teams_fixed",
    "_user",
    "_view",
    "_views",
    "_waiting",
    "approve",
    "approved_contests",
    "delete",
    "display_names",
    "entrant_ids",
    "everyone_in",
    "extend",
    "forget_person",
    "hold_for_reading",
    "invite",
    "leave",
    "list_entrants",
    "member_ids",
    "membership",
    "mine",
    "move_member",
    "of_person",
    "organise_rename",
    "panel",
    "ref",
    "register",
    "reject",
    "remove",
    "remove_member",
    "rename",
    "reopen",
    "settle",
    "standing",
    "statuses",
    "submitted",
    "take_place",
    "username_of",
    "withdraw_invite",
    "workspace_of",
    "workspaces_of",
]
