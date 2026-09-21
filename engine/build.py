"""Nightly build: refresh history, library, requests and catalogue, then rank two surfaces for
every user with the adopted blend — the titles the library does not hold (`suggestions`, what
picks shows) and the ones it does hold and they have not watched (`library_suggestions`, what
Shortlist's rows draw from through the engine protocol). Everything here is read-only against
live services; the only writes are local."""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np

from harness import blend, catalogue, content, data, engagement, graph, llm, movielens, pull, resolve, signals, tmdb, tune
from harness.data import Key, Seed, UserContext

from . import identity

MIN_SEEDS = 3                 # below this a user gets the household/popularity fallback
LIST_SIZE = 300               # the missing surface; the library surface ranks the whole library
NEIGHBOURS = 100              # per library title, for rows focused on one or a few watches
MOVIE_OBTAINABLE_LAG_DAYS = 45   # theatrical → digital, a heuristic until release types are used
RECENT_YEARS = 2              # catalogue refresh re-discovers only these years nightly
WHY_SEEDS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    plex_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    title TEXT, year INTEGER, rating REAL, votes INTEGER, poster_path TEXT, overview TEXT,
    kids INTEGER NOT NULL,
    why TEXT,                             -- JSON: [{"seed": title, "seed_tmdb_id": id, "media_type": mt}]
    score REAL,
    built_at INTEGER NOT NULL,
    PRIMARY KEY (plex_id, tmdb_id, media_type)
);
CREATE INDEX IF NOT EXISTS suggestions_user ON suggestions (plex_id, rank);
CREATE TABLE IF NOT EXISTS library_suggestions (
    plex_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    title TEXT, year INTEGER, rating REAL, votes INTEGER, poster_path TEXT, overview TEXT,
    kids INTEGER NOT NULL,
    why TEXT,
    score REAL,
    built_at INTEGER NOT NULL,
    PRIMARY KEY (plex_id, tmdb_id, media_type)
);
CREATE INDEX IF NOT EXISTS library_suggestions_user ON library_suggestions (plex_id, rank);
CREATE TABLE IF NOT EXISTS builds (built_at INTEGER PRIMARY KEY, users INTEGER, seconds REAL, network_calls INTEGER);
"""

SURFACES = {"missing": "suggestions", "library": "library_suggestions"}
META_SCHEMA = "CREATE TABLE IF NOT EXISTS engine_meta (key TEXT PRIMARY KEY, value TEXT);"
# The library surface is ranked in full: Shortlist's rows narrow it (a season, one person's libraries,
# unstarted shows, draw-without-replacement across rows), and a cap there left a seasonal row with
# whatever of its season happened to make the head. ~1,900 titles per person.
SURFACE_LIMITS = {"missing": LIST_SIZE, "library": None}

# Each library title's nearest library titles by content (TF-IDF over keywords, genres, cast, crew …;
# averaged with gemini-embedding-2 where both titles have one, as the shows blend weights it). What a
# row focused on a few watches ("Because you watched X") ranks by — see service.recommend.
NEIGHBOURS_SCHEMA = """
CREATE TABLE IF NOT EXISTS item_neighbours (
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    n_tmdb_id INTEGER NOT NULL,
    n_media_type TEXT NOT NULL,
    sim REAL NOT NULL,
    built_at INTEGER NOT NULL,
    PRIMARY KEY (tmdb_id, media_type, n_tmdb_id, n_media_type)
);
"""

# Household label per person, from their own last 12 months of positives (children's titles by
# `signals.is_kids`). Measured 2026-09-19 on 12 accounts: two mixed family accounts at 35% and 49%,
# one child's own account at 100%, a borderline 15%, everyone else at 0-8%.
# The engine's own label is a suggestion: Shortlist re-derives it from the counts with thresholds from
# its own settings (and a per-person override). These env vars set the engine's defaults.
HOUSEHOLD_WINDOW_DAYS = int(os.environ.get("ENGINE_HOUSEHOLD_WINDOW_DAYS", "365"))
HOUSEHOLD_MIN_TITLES = int(os.environ.get("ENGINE_HOUSEHOLD_MIN_TITLES", "10"))  # fewer: too little to judge -> adult
FAMILY_MIN_SHARE = float(os.environ.get("ENGINE_FAMILY_MIN_SHARE", "0.15"))  # at least this share of kids titles ...
FAMILY_MIN_KIDS_TITLES = int(os.environ.get("ENGINE_FAMILY_MIN_KIDS_TITLES", "4"))  # ... from this many -> family
KIDS_ACCOUNT_MIN_SHARE = float(os.environ.get("ENGINE_KIDS_ACCOUNT_MIN_SHARE", "0.80"))  # above: a child's own account

HOUSEHOLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    plex_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,                  -- adult | family | kids
    kids_share REAL NOT NULL,
    kids_titles INTEGER NOT NULL,
    window_titles INTEGER NOT NULL,
    built_at INTEGER NOT NULL
);
"""


def household_label(window_titles: int, kids_titles: int) -> str:
    """adult | family | kids — see the constants above."""
    if window_titles < HOUSEHOLD_MIN_TITLES:
        return "adult"
    share = kids_titles / window_titles
    if share > KIDS_ACCOUNT_MIN_SHARE:
        return "kids"
    if share >= FAMILY_MIN_SHARE and kids_titles >= FAMILY_MIN_KIDS_TITLES:
        return "family"
    return "adult"


def write_households(con, items, built_at: int) -> dict[int, str]:
    since = built_at - HOUSEHOLD_WINDOW_DAYS * 86400
    counts: dict[int, list[int]] = {}
    for r in con.execute(
        "SELECT user_id, tmdb_id, media_type FROM engagements WHERE label = 'positive' AND last_at >= ?", (since,)
    ):
        it = items.get((r["tmdb_id"], r["media_type"]))
        if it is None:
            continue
        c = counts.setdefault(r["user_id"], [0, 0])
        c[0] += 1
        c[1] += int(signals.is_kids(it))
    con.execute("DELETE FROM households")
    labels = {}
    pooled = identity.members()
    for (u,) in con.execute("SELECT user_id FROM users"):
        if u in pooled:
            continue  # answered with the canonical's row — see `identity`
        total, kids = counts.get(u, [0, 0])
        labels[u] = household_label(total, kids)
        con.execute(
            "INSERT INTO households VALUES (?,?,?,?,?,?)",
            (u, labels[u], kids / total if total else 0.0, kids, total, built_at),
        )
    return labels


def refresh_catalogue(con) -> None:
    """New releases and any library/history/request title not yet stored; the full crawl
    (`harness catalogue`) is only needed on first install or after the cache TTL."""
    con.executescript(catalogue.SCHEMA)
    have = {(r[0], r[1]) for r in con.execute("SELECT tmdb_id, media_type FROM items")}
    targets = {}
    year = date.today().year
    for media_type in ("movie", "show"):
        for y in range(year - RECENT_YEARS, year + 2):
            for tmdb_id in catalogue.discover_year(media_type, y):
                targets[(tmdb_id, media_type)] = True
    for sql in ("SELECT tmdb_id, media_type FROM library", "SELECT tmdb_id, media_type FROM titles WHERE tmdb_id IS NOT NULL",
                "SELECT tmdb_id, media_type FROM seerr_requests"):
        for r in con.execute(sql):
            targets.setdefault((r["tmdb_id"], r["media_type"]), False)
    todo = [k for k in targets if k not in have]
    for key in todo:
        row = catalogue.fetch_item(key[1], key[0])
        if row is not None:
            con.execute("INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row[0], row[1], int(targets[key]), *row[2:]))
    con.commit()
    print(f"catalogue: {len(todo)} new items")


def contexts_now(con, items, surface="missing") -> dict[int, UserContext]:
    """Every user's context over one surface: `missing` is the obtainable catalogue minus the
    library and minus anything already requested; `library` is exactly the library (every title
    Plex holds, catalogue or not). Both minus the person's own positives, in `_contexts`."""
    now = int(time.time())
    library = {(r[0], r[1]) for r in con.execute("SELECT tmdb_id, media_type FROM library")}
    if surface == "library":
        universe = {k for k in items if k in library}
    else:
        today = date.today()
        movie_limit = (today - timedelta(days=MOVIE_OBTAINABLE_LAG_DAYS)).isoformat()
        show_limit = today.isoformat()
        unavailable = library | {(r[0], r[1]) for r in con.execute("SELECT tmdb_id, media_type FROM seerr_requests")}
        universe = {
            k for k, it in items.items()
            if it.in_catalogue and k not in unavailable and it.release_date
            and it.release_date <= (movie_limit if k[1] == "movie" else show_limit)
        }
    # A pooled member has no list of its own: its plays are the canonical's, and its lookups are
    # answered with the canonical's lists (`identity`). Ranking it separately would write a
    # popularity list for an account with "no history" and count one household as several people.
    pooled = identity.members()
    users = [r["user_id"] for r in con.execute("SELECT user_id FROM users") if r["user_id"] not in pooled]
    return data._contexts(con, users, now, universe)


def write_neighbours(con, items, space: content.ItemSpace, embeddings, built_at: int) -> int:
    """Top-NEIGHBOURS library titles per library title (both media), written whole each build."""
    library = sorted({(r[0], r[1]) for r in con.execute("SELECT tmdb_id, media_type FROM library")} & space.index.keys())
    con.executescript(NEIGHBOURS_SCHEMA)
    con.execute("DELETE FROM item_neighbours")
    if len(library) < 2:
        return 0
    X = space.X[[space.index[k] for k in library]]
    sims = (X @ X.T).toarray().astype(np.float32)
    if embeddings is not None and len(getattr(embeddings, "keys", [])):
        has = np.array([k in embeddings.index for k in library])
        rows = np.flatnonzero(has)
        if len(rows) > 1:
            E = embeddings.X[[embeddings.index[library[i]] for i in rows]]
            E = E.toarray() if hasattr(E, "toarray") else np.asarray(E)
            both = np.ix_(rows, rows)
            sims[both] = 0.5 * sims[both] + 0.5 * np.maximum(E @ E.T, 0)
    np.fill_diagonal(sims, 0)
    k = min(NEIGHBOURS, len(library) - 1)
    out = []
    for i, key in enumerate(library):
        top = np.argpartition(-sims[i], k - 1)[:k]
        out.extend((key[0], key[1], library[j][0], library[j][1], float(sims[i, j]), built_at) for j in top if sims[i, j] > 0)
    con.executemany("INSERT INTO item_neighbours VALUES (?,?,?,?,?,?)", out)
    return len(out)


def popularity_fallback(ctx: UserContext, items, years=3, limit: int | None = LIST_SIZE) -> list[Key]:
    """Too few seeds to rank from: recent titles by votes first, then everything else by votes
    (on the library surface the recent slice alone can be a handful)."""
    floor = str(date.today().year - years)

    def key(k: Key) -> tuple:
        it = items[k]
        return (not (it.release_date and it.release_date >= floor), -it.vote_count)

    return sorted(ctx.candidates, key=key)[:limit]


def explain(space: content.ItemSpace, ctx: UserContext, key: Key, items, continuations) -> list[dict]:
    if key in continuations:
        seed = items[continuations[key]]
        return [{"seed": seed.title, "seed_tmdb_id": seed.key[0], "media_type": seed.key[1], "kind": "next_in_series"}]
    seeds = [s for s in ctx.seeds if s.key in space.index]
    if not seeds or key not in space.index:
        return []
    sims = (space.X[[space.index[key]]] @ space.X[[space.index[s.key] for s in seeds]].T).toarray()[0]
    top = np.argsort(-sims)[:WHY_SEEDS]
    return [
        {"seed": items[seeds[i].key].title, "seed_tmdb_id": seeds[i].key[0], "media_type": seeds[i].key[1], "kind": "similar"}
        for i in top if sims[i] > 0
    ]


def check_identity(con) -> None:
    """Refuse to build on a map whose canonical account Tautulli does not know; say so for a member.

    A canonical that is not in `users` is never ranked, and its members are skipped BECAUSE they are
    pooled — so one mistyped digit leaves a whole household with no lists on either surface and no
    household label, and nothing anywhere says why: the callers just fall back. Refusing keeps last
    night's lists in place and puts the reason in the build log and `/healthz`. An unknown MEMBER is
    ordinary (a new profile nobody has watched on yet) and only worth a line.
    """
    canonicals, members = identity.unknown_ids(con)
    for plex_id in members:
        print(f"identity: member {plex_id} has no plays yet — pooled, nothing to add")
    if canonicals:
        raise ValueError(
            f"{identity.ENV}: canonical account {canonicals[0]} is not a user Tautulli knows — check the id. "
            "Building with it would leave that household with no lists at all."
        )


def record_meta(con, version: str, pooled: dict[int, int]) -> None:
    """What made this build, so a start under something else rebuilds: the engine version, and the
    identity map it pooled accounts by (the service compares both — `service.nightly`)."""
    con.executescript(META_SCHEMA)
    con.execute("INSERT OR REPLACE INTO engine_meta VALUES ('built_by', ?)", (version,))
    con.execute("INSERT OR REPLACE INTO engine_meta VALUES ('identity_groups', ?)", (identity.normalised(pooled),))


def pooled_requests(requests: dict, pooled: dict[int, int]) -> dict:
    """A member's own Seerr requests seed the canonical's ranking, like its plays do."""
    merged: dict[int, dict] = {}
    for user_id, rows in requests.items():
        mine = merged.setdefault(pooled.get(user_id, user_id), {})
        for key, created_at in rows:
            # Once per title, at its EARLIEST request: two profiles asking for the same show (two
            # seasons of it, say) is one statement of intent, not a seed of twice the weight.
            mine[key] = min(created_at, mine.get(key, created_at))
    return {user_id: list(rows.items()) for user_id, rows in merged.items()}


def build(con, version: str = "") -> None:
    started = time.time()
    calls0 = tmdb.network_calls
    con.executescript(SCHEMA)
    con.executescript(HOUSEHOLD_SCHEMA)
    pooled = identity.members()  # raises on a malformed map, before anything is rebuilt
    pull.run(con)
    check_identity(con)
    resolve.run(con)
    engagement.run(con, pooled)
    refresh_catalogue(con)

    items = data.load_items(con)
    embeddings = llm.EmbeddingSpace(con, items)
    embeddings = embeddings if len(embeddings.keys) else None
    scorer = signals.IntentSeeds(tune.final_blend(embeddings), pooled_requests(signals.requests_by_user(con), pooled))
    # `prepare` builds the cross-user state (the household, the EASE fold-in) from the contexts'
    # seeds, which are the same on both surfaces — only the candidate universe differs.
    contexts = contexts_now(con, items, "missing")
    scorer.prepare(contexts, items)
    space = scorer.scorer.components[1].space
    built_at = int(time.time())
    meta = {(r["tmdb_id"], r["media_type"]): r for r in con.execute("SELECT tmdb_id, media_type, poster_path, overview FROM items")}
    labels = write_households(con, items, built_at)
    print("households:", {k: sum(1 for v in labels.values() if v == k) for k in ("adult", "family", "kids")})
    print("neighbour pairs:", write_neighbours(con, items, space, embeddings, built_at))
    users = 0
    for surface, table in SURFACES.items():
        if surface != "missing":
            contexts = contexts_now(con, items, surface)
        con.execute(f"DELETE FROM {table}")
        for u, ctx in contexts.items():
            con.executemany(
                f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rank_user(scorer, space, ctx, items, meta, u, built_at, limit=SURFACE_LIMITS[surface]),
            )
        users = len(contexts)
    seconds = time.time() - started
    con.execute("INSERT INTO builds VALUES (?,?,?,?)", (built_at, users, seconds, tmdb.network_calls - calls0))
    record_meta(con, version, pooled)
    con.commit()
    print(f"built {users} users x {len(SURFACES)} surfaces in {seconds:.0f}s, {tmdb.network_calls - calls0} TMDb calls, "
          f"{datetime.fromtimestamp(built_at, timezone.utc):%Y-%m-%d %H:%M}Z")


def rank_user(
    scorer, space, ctx: UserContext, items, meta, plex_id: int, built_at: int, limit: int | None = LIST_SIZE
) -> list[tuple]:
    """One person's ranked list over the context's candidates, as rows for either surface table.
    `limit` None ranks every candidate: titles the blend could not score follow by votes."""
    if len(ctx.seeds) >= MIN_SEEDS:
        scores = scorer(ctx, items)
        order = sorted(scores, key=lambda k: -scores[k])
        if limit is None:
            scored = set(order)
            order += sorted((k for k in ctx.candidates if k not in scored), key=lambda k: -items[k].vote_count)
        order = order[:limit]
        continuations = scorer.scorer.last_continuations
    else:
        order, continuations = popularity_fallback(ctx, items, limit=limit), {}
    rows = []
    for rank, k in enumerate(order):
        it = items[k]
        rows.append((
            plex_id, rank, k[0], k[1], it.title, it.year, it.vote_average, it.vote_count,
            meta[k]["poster_path"], meta[k]["overview"], int(signals.is_kids(it)),
            json.dumps(explain(space, ctx, k, items, continuations)), float(len(order) - rank), built_at,
        ))
    return rows
