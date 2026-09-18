"""Nightly build: refresh history, library, requests and catalogue, then rank the missing
titles for every user with the adopted blend and write the `suggestions` table the API
serves. Everything here is read-only against live services; the only writes are local."""

import json
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np

from harness import blend, catalogue, content, data, engagement, graph, llm, movielens, pull, resolve, signals, tmdb, tune
from harness.data import Key, Seed, UserContext

MIN_SEEDS = 3                 # below this a user gets the household/popularity fallback
LIST_SIZE = 300
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
CREATE TABLE IF NOT EXISTS builds (built_at INTEGER PRIMARY KEY, users INTEGER, seconds REAL, network_calls INTEGER);
"""


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


def contexts_now(con, items) -> dict[int, UserContext]:
    now = int(time.time())
    today = date.today()
    movie_limit = (today - timedelta(days=MOVIE_OBTAINABLE_LAG_DAYS)).isoformat()
    show_limit = today.isoformat()
    unavailable = {(r[0], r[1]) for r in con.execute("SELECT tmdb_id, media_type FROM library")}
    unavailable |= {(r[0], r[1]) for r in con.execute("SELECT tmdb_id, media_type FROM seerr_requests")}
    universe = {
        k for k, it in items.items()
        if it.in_catalogue and k not in unavailable and it.release_date
        and it.release_date <= (movie_limit if k[1] == "movie" else show_limit)
    }
    users = [r["user_id"] for r in con.execute("SELECT user_id FROM users")]
    return data._contexts(con, users, now, universe)


def popularity_fallback(ctx: UserContext, items, years=3) -> list[Key]:
    floor = str(date.today().year - years)
    recent = [k for k in ctx.candidates if items[k].release_date and items[k].release_date >= floor]
    return sorted(recent, key=lambda k: -items[k].vote_count)[:LIST_SIZE]


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


def build(con) -> None:
    started = time.time()
    calls0 = tmdb.network_calls
    con.executescript(SCHEMA)
    pull.run(con)
    resolve.run(con)
    engagement.run(con)
    refresh_catalogue(con)

    items = data.load_items(con)
    contexts = contexts_now(con, items)
    space = llm.EmbeddingSpace(con, items)
    scorer = signals.IntentSeeds(tune.final_blend(space if len(space.keys) else None), signals.requests_by_user(con))
    scorer.prepare(contexts, items)
    space = scorer.scorer.components[1].space
    built_at = int(time.time())
    meta = {(r["tmdb_id"], r["media_type"]): r for r in con.execute("SELECT tmdb_id, media_type, poster_path, overview FROM items")}
    con.execute("DELETE FROM suggestions")
    for u, ctx in contexts.items():
        if len(ctx.seeds) >= MIN_SEEDS:
            scores = scorer(ctx, items)
            order = sorted(scores, key=lambda k: -scores[k])[:LIST_SIZE]
            continuations = scorer.scorer.last_continuations
        else:
            order, continuations = popularity_fallback(ctx, items), {}
        rows = []
        for rank, k in enumerate(order):
            it = items[k]
            rows.append((
                u, rank, k[0], k[1], it.title, it.year, it.vote_average, it.vote_count,
                meta[k]["poster_path"], meta[k]["overview"], int(signals.is_kids(it)),
                json.dumps(explain(space, ctx, k, items, continuations)), float(LIST_SIZE - rank), built_at,
            ))
        con.executemany("INSERT OR REPLACE INTO suggestions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    seconds = time.time() - started
    con.execute("INSERT INTO builds VALUES (?,?,?,?)", (built_at, len(contexts), seconds, tmdb.network_calls - calls0))
    con.commit()
    print(f"built {len(contexts)} users in {seconds:.0f}s, {tmdb.network_calls - calls0} TMDb calls, "
          f"{datetime.fromtimestamp(built_at, timezone.utc):%Y-%m-%d %H:%M}Z")
