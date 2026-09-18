"""In-memory views of the harness database for scoring: the item catalogue and one
UserContext per (fold, user), which is all a scorer is allowed to see."""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .engagement import seed_weight

Key = tuple[int, str]  # (tmdb_id, media_type)


@dataclass
class Item:
    key: Key
    title: str
    year: int | None
    release_date: str | None
    genres: list[int]
    keywords: list[int]
    cast: list[int]
    crew: list[int]
    language: str | None
    certification: str | None
    collection_id: int | None
    runtime: int | None
    seasons: int | None
    vote_average: float
    vote_count: int
    popularity: float
    recommendations: list[int]
    similar: list[int]
    in_catalogue: bool

    @property
    def media_type(self) -> str:
        return self.key[1]


@dataclass
class Seed:
    key: Key
    weight: float          # engagement × rewatch × recency at the cutoff
    first_at: int
    last_at: int
    engagement: float
    tv_share: float = 0.0


@dataclass
class UserContext:
    user_id: int
    cutoff: int
    seeds: list[Seed]                                  # positives before the cutoff
    negatives: set[Key]                                # weak negatives before the cutoff
    candidates: set[Key]
    household_seeds: dict[int, list[Seed]] = field(default_factory=dict)  # other users
    household_requests: dict[int, set[Key]] = field(default_factory=dict)  # before cutoff


def load_items(con) -> dict[Key, Item]:
    items = {}
    for r in con.execute("SELECT * FROM items"):
        key = (r["tmdb_id"], r["media_type"])
        items[key] = Item(
            key, r["title"], r["year"], r["release_date"],
            json.loads(r["genres"]), json.loads(r["keywords"]), json.loads(r["cast"]),
            json.loads(r["crew"]), r["language"], r["certification"], r["collection_id"],
            r["runtime"], r["seasons"], r["vote_average"] or 0.0, r["vote_count"] or 0, r["popularity"] or 0.0,
            json.loads(r["recommendations"]), json.loads(r["similar"]), bool(r["in_catalogue"]),
        )
    return items


def _seeds(con, user_id: int, cutoff: int, exclude: set[Key] = frozenset()) -> list[Seed]:
    out = []
    for r in con.execute(
        "SELECT * FROM engagements WHERE user_id = ? AND label = 'positive' AND first_at <= ?",
        (user_id, cutoff),
    ):
        key = (r["tmdb_id"], r["media_type"])
        if key not in exclude:
            out.append(Seed(key, seed_weight(r, cutoff), r["first_at"], r["last_at"], r["engagement"], r["tv_share"]))
    return out


def _negatives(con, user_id: int, cutoff: int) -> set[Key]:
    return {
        (r["tmdb_id"], r["media_type"])
        for r in con.execute(
            "SELECT tmdb_id, media_type FROM engagements WHERE user_id = ? "
            "AND label = 'weak_negative' AND last_at <= ?", (user_id, cutoff))
    }


def fold_contexts(con, items: dict[Key, Item], fold_id: int) -> dict[int, UserContext]:
    """Contexts for the arrival-time fold: candidates are catalogue titles absent from the
    library at the cutoff, not yet requested by anyone (picks parks those, so nobody would be
    shown them), and released by the end of the window."""
    fold = con.execute("SELECT * FROM folds WHERE fold_id = ?", (fold_id,)).fetchone()
    cutoff, window_end = fold["cutoff"], fold["window_end"]
    unavailable = {
        (r["tmdb_id"], r["media_type"])
        for r in con.execute("SELECT tmdb_id, media_type FROM library WHERE added_at IS NULL OR added_at <= ?", (cutoff,))
    } | {
        (r["tmdb_id"], r["media_type"])
        for r in con.execute("SELECT tmdb_id, media_type FROM seerr_requests WHERE created_at <= ?", (cutoff,))
    }
    end_date = datetime.fromtimestamp(window_end, timezone.utc).strftime("%Y-%m-%d")
    universe = {
        k for k, it in items.items()
        if it.in_catalogue and k not in unavailable and it.release_date and it.release_date <= end_date
    }
    users = [r["user_id"] for r in con.execute("SELECT user_id FROM fold_users WHERE fold_id = ?", (fold_id,))]
    return _contexts(con, users, cutoff, universe)


def holdout_contexts(con, items: dict[Key, Item], now: int) -> dict[int, UserContext]:
    """Contexts for the secondary protocol: whole catalogue, library included; the user's
    held-out positives are removed from their seeds."""
    universe = {k for k, it in items.items() if it.in_catalogue}
    users = [r["user_id"] for r in con.execute("SELECT user_id FROM users WHERE evaluated")]
    held = {}
    for r in con.execute("SELECT * FROM holdout"):
        held.setdefault(r["user_id"], set()).add((r["tmdb_id"], r["media_type"]))
    return _contexts(con, users, now, universe, held)


def _contexts(con, users, cutoff, universe, held=None) -> dict[int, UserContext]:
    held = held or {}
    seeds = {u: _seeds(con, u, cutoff, held.get(u, set())) for u in users}
    requests = {}
    for r in con.execute(
        "SELECT user_id, tmdb_id, media_type FROM seerr_requests WHERE created_at <= ?", (cutoff,)):
        requests.setdefault(r["user_id"], set()).add((r["tmdb_id"], r["media_type"]))
    contexts = {}
    for u in users:
        seen = {s.key for s in seeds[u]}
        contexts[u] = UserContext(
            user_id=u,
            cutoff=cutoff,
            seeds=seeds[u],
            negatives=_negatives(con, u, cutoff),
            candidates=universe - seen,
            household_seeds={v: s for v, s in seeds.items() if v != u},
            household_requests={v: s for v, s in requests.items() if v != u},
        )
    return contexts
