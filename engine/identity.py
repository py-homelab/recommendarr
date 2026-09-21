"""Several Plex accounts that are one household, ranked as one.

A household that moves off one shared account onto Plex Home profiles (an adults' profile, a
children's one) still has ONE taste history: the years on the old account plus whatever each profile
watches from now on. `ENGINE_IDENTITY_GROUPS` names those accounts and the engine pools their plays
under the first of them, the canonical id:

    ENGINE_IDENTITY_GROUPS="895220:856697834,856698746"        canonical:member,member
    ENGINE_IDENTITY_GROUPS="1:2,3;10:11"                        several groups, ';' between them

Applied where engagements are built — raw plays keep the account that made them, so the map can be
changed or removed and the next build simply regroups — and again at lookup, where a member's id is
answered with the canonical's lists. What differs between the profiles (children's titles only, or
none) is not decided here: every `/v1/recommend` answer for an account in a group carries the same
household counts plus `group`, and it is the CALLER's per-person household setting that has to tell
the profiles apart — the counts cannot.

Unset or empty is a strict no-op. A malformed value stops the engine at start rather than being half
applied: an ignored map leaves the profiles ungrouped with nothing to show for it.
"""

import os

ENV = "ENGINE_IDENTITY_GROUPS"


def parse(raw: str | None) -> dict[int, int]:
    """member id -> canonical id. The canonical itself is not a key. Raises ValueError with a
    message naming the offending part."""
    members: dict[int, int] = {}
    canonicals: set[int] = set()
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part:
            continue
        head, sep, tail = part.partition(":")
        if not sep:
            raise ValueError(f"{ENV}: {part!r} has no ':' — write canonical:member,member")
        canonical = _account_id(head, part)
        if not tail.strip():
            raise ValueError(f"{ENV}: group {canonical} names no members")
        ids = [_account_id(m, part) for m in tail.split(",")]
        if canonical in canonicals:
            raise ValueError(f"{ENV}: {canonical} is the canonical id of two groups")
        canonicals.add(canonical)
        for member in ids:
            if member == canonical:
                raise ValueError(f"{ENV}: {canonical} is listed as its own member")
            if member in members:
                raise ValueError(f"{ENV}: {member} appears twice")
            members[member] = canonical
    both = canonicals & members.keys()
    if both:
        raise ValueError(f"{ENV}: {sorted(both)[0]} is the canonical id of one group and a member of another")
    return members


def _account_id(token: str, part: str) -> int:
    """A Plex account id: plain ASCII digits, above zero. `int()` alone is far too generous for a
    value someone pastes into a compose file — it reads "1_0" as 10, "+5" as 5 and "٣" as 3, and a
    mistyped id is not an error anywhere downstream: it is a household with no lists."""
    token = token.strip()
    if not (token.isascii() and token.isdigit()) or int(token) <= 0:
        raise ValueError(f"{ENV}: {part!r} — {token!r} is not a Plex account id (digits only, above zero)")
    return int(token)


def members() -> dict[int, int]:
    return parse(os.environ.get(ENV))


def canonical(plex_id: int) -> int:
    return members().get(plex_id, plex_id)


def group_of(plex_id: int) -> int | None:
    """The canonical id of the group this account is in (members AND the canonical), else None."""
    table = members()
    if plex_id in table:
        return table[plex_id]
    return plex_id if plex_id in table.values() else None


def normalised(table: dict[int, int] | None = None) -> str:
    """One spelling per map, so "did it change since the last build?" is a string comparison."""
    table = members() if table is None else table
    groups: dict[int, list[int]] = {}
    for member, head in table.items():
        groups.setdefault(head, []).append(member)
    return ";".join(f"{head}:{','.join(str(m) for m in sorted(ids))}" for head, ids in sorted(groups.items()))


def unknown_ids(con) -> tuple[list[int], list[int]]:
    """`(canonicals, members)` of the map that Tautulli has never reported a user for."""
    table = members()
    known = {r[0] for r in con.execute("SELECT user_id FROM users")}
    return (
        sorted(set(table.values()) - known),
        sorted(set(table) - known),
    )


def check(con=None) -> tuple[str, list[str]]:
    """The resolved map as text, and warnings about ids the engine has never seen (needs a database)."""
    table = members()
    if not table:
        return f"{ENV} is unset or empty: no accounts are grouped.", []
    lines = [f"group {head}: {', '.join(str(m) for m in sorted(m for m, h in table.items() if h == head))}"
             for head in sorted(set(table.values()))]
    warnings = []
    if con is not None:
        canonicals, pooled = unknown_ids(con)
        warnings += [
            f"{plex_id} (canonical) is not in the users table — the build REFUSES a map whose canonical "
            "account Tautulli has never reported, since that household would get no lists at all"
            for plex_id in canonicals
        ]
        warnings += [
            f"{plex_id} (member) is not in the users table — Tautulli has not reported that account yet, "
            "which is normal for a profile nobody has watched on"
            for plex_id in pooled
        ]
    return "\n".join(lines), warnings
