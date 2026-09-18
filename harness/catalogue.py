"""A local TMDb catalogue: the candidate universe for every scorer, plus the metadata the
content and graph scorers are built from.

Membership: movies with vote_count >= 100 and shows with vote_count >= 50 (via /discover,
sliced by year so no query exceeds TMDb's 500-page cap), plus every title in the library or
in anyone's history regardless of votes (flagged in_catalogue = 0, usable as seeds only)."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import tmdb

MIN_VOTES = {"movie": 100, "show": 50}
FIRST_YEAR = 1950
TOP_CAST = 10
WORKERS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    in_catalogue INTEGER NOT NULL,
    title TEXT,
    release_date TEXT,                    -- YYYY-MM-DD, first air date for shows
    year INTEGER,
    genres TEXT,                          -- JSON list of genre ids
    keywords TEXT,                        -- JSON list of keyword ids
    cast TEXT,                            -- JSON list of person ids, top billed first
    crew TEXT,                            -- JSON list of director / creator person ids
    language TEXT,
    countries TEXT,                       -- JSON list of ISO codes
    certification TEXT,                   -- US rating where known
    collection_id INTEGER,
    runtime INTEGER,
    seasons INTEGER,
    vote_average REAL,
    vote_count INTEGER,
    popularity REAL,
    recommendations TEXT,                 -- JSON list of tmdb ids, TMDb order
    similar TEXT,
    poster_path TEXT,
    overview TEXT,
    PRIMARY KEY (tmdb_id, media_type)
);
"""


def discover_year(media_type: str, year: int) -> set[int]:
    kind = tmdb.KIND[media_type]
    date_field = "primary_release_date" if kind == "movie" else "first_air_date"
    ids, page = set(), 1
    while True:
        body = tmdb.get(
            f"/discover/{kind}",
            **{
                "vote_count.gte": MIN_VOTES[media_type],
                f"{date_field}.gte": f"{year}-01-01",
                f"{date_field}.lte": f"{year}-12-31",
                "sort_by": "vote_count.desc",
                "page": page,
            },
        )
        ids.update(r["id"] for r in body["results"])
        if page >= min(body["total_pages"], 500):
            return ids
        page += 1


def discover_ids(media_type: str) -> set[int]:
    years = range(FIRST_YEAR, date.today().year + 2)
    with ThreadPoolExecutor(WORKERS) as pool:
        return set().union(*pool.map(lambda y: discover_year(media_type, y), years))


def fetch_item(media_type: str, tmdb_id: int) -> tuple | None:
    kind = tmdb.KIND[media_type]
    extra = "release_dates" if kind == "movie" else "content_ratings"
    body = tmdb.get(
        f"/{kind}/{tmdb_id}",
        append_to_response=f"recommendations,similar,keywords,credits,external_ids,{extra}",
    )
    if body is None:
        return None
    is_movie = kind == "movie"
    release_date = body.get("release_date" if is_movie else "first_air_date") or None
    keywords = body.get("keywords") or {}
    credits = body.get("credits") or {}
    crew_roles = {"Director"} if is_movie else {"Creator", "Executive Producer"}
    crew = [c["id"] for c in credits.get("crew") or [] if c.get("job") in crew_roles]
    if not is_movie:
        crew = [c["id"] for c in body.get("created_by") or []] + crew
    certification = None
    if is_movie:
        for entry in (body.get("release_dates") or {}).get("results") or []:
            if entry["iso_3166_1"] == "US":
                certification = next((r["certification"] for r in entry["release_dates"] if r.get("certification")), None)
    else:
        for entry in (body.get("content_ratings") or {}).get("results") or []:
            if entry["iso_3166_1"] == "US":
                certification = entry.get("rating") or None
    return (
        tmdb_id, media_type,
        body.get("title") or body.get("name"), release_date,
        int(release_date[:4]) if release_date else None,
        json.dumps([g["id"] for g in body.get("genres") or []]),
        json.dumps([k["id"] for k in keywords.get("keywords") or keywords.get("results") or []]),
        json.dumps([c["id"] for c in (credits.get("cast") or [])[:TOP_CAST]]),
        json.dumps(list(dict.fromkeys(crew))),
        body.get("original_language"),
        json.dumps(body.get("origin_country") or [c["iso_3166_1"] for c in body.get("production_countries") or []]),
        certification,
        (body.get("belongs_to_collection") or {}).get("id"),
        body.get("runtime") if is_movie else (body.get("episode_run_time") or [None])[0],
        None if is_movie else body.get("number_of_seasons"),
        body.get("vote_average"), body.get("vote_count"), body.get("popularity"),
        json.dumps([r["id"] for r in (body.get("recommendations") or {}).get("results") or []]),
        json.dumps([r["id"] for r in (body.get("similar") or {}).get("results") or []]),
        body.get("poster_path"), body.get("overview"),
    )


def wanted(con) -> dict[tuple[int, str], bool]:
    """(tmdb_id, media_type) -> in_catalogue, for everything that needs an items row."""
    out = {}
    for media_type in ("movie", "show"):
        for tmdb_id in discover_ids(media_type):
            out[(tmdb_id, media_type)] = True
        print(f"discover: {sum(1 for k in out if k[1] == media_type):>6} {media_type}s")
    for sql in (
        "SELECT tmdb_id, media_type FROM library",
        "SELECT tmdb_id, media_type FROM titles WHERE tmdb_id IS NOT NULL",
        "SELECT tmdb_id, media_type FROM seerr_requests",
    ):
        for r in con.execute(sql):
            out.setdefault((r["tmdb_id"], r["media_type"]), False)
    return out


def run(con) -> None:
    con.executescript(SCHEMA)
    targets = wanted(con)
    have = {(r[0], r[1]) for r in con.execute("SELECT tmdb_id, media_type FROM items")}
    todo = [k for k in targets if k not in have]
    print(f"items wanted {len(targets)}, already stored {len(have)}, fetching {len(todo)}")

    def job(key):
        row = fetch_item(key[1], key[0])
        return key, row

    with ThreadPoolExecutor(WORKERS) as pool:
        for n, (key, row) in enumerate(pool.map(job, todo), 1):
            if row is not None:
                con.execute(
                    "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row[0], row[1], int(targets[key]), *row[2:]),
                )
            if n % 500 == 0:
                con.commit()
                print(f"  {n}/{len(todo)} fetched, {tmdb.network_calls} network calls")
    con.execute(
        "UPDATE items SET in_catalogue = 1 WHERE in_catalogue = 0 AND "
        "((media_type = 'movie' AND vote_count >= ?) OR (media_type = 'show' AND vote_count >= ?))",
        (MIN_VOTES["movie"], MIN_VOTES["show"]),
    )
    con.commit()
    for r in con.execute(
        "SELECT media_type, in_catalogue, COUNT(*) n FROM items GROUP BY 1, 2 ORDER BY 1, 2"
    ):
        print(f"{r['media_type']:6} in_catalogue={r['in_catalogue']} {r['n']:>6}")
    print(f"network calls this run: {tmdb.network_calls}")
