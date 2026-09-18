"""Map every movie/show rating key in the play history to a TMDb id."""

from . import config, tmdb
from .pull import tautulli


def _history_titles(con):
    return con.execute(
        """
        SELECT rating_key, 'movie' AS media_type, MAX(title) AS title, MAX(year) AS year
        FROM plays WHERE media_type = 'movie' AND rating_key IS NOT NULL GROUP BY rating_key
        UNION ALL
        SELECT grandparent_rating_key, 'show', MAX(grandparent_title),
               CAST(MIN(substr(originally_available_at, 1, 4)) AS INTEGER)
        FROM plays WHERE media_type = 'episode' AND grandparent_rating_key IS NOT NULL
        GROUP BY grandparent_rating_key
        """
    ).fetchall()


def _from_tautulli(rating_key: int) -> int | None:
    meta = tautulli("get_metadata", rating_key=rating_key) or {}
    for guid in meta.get("guids") or []:
        if guid.startswith("tmdb://"):
            return int(guid.removeprefix("tmdb://"))
    return None


def run(con) -> None:
    done = {
        r["rating_key"]
        for r in con.execute("SELECT rating_key FROM titles WHERE source != 'unresolved'")
    }
    try:
        config.secret("TMDB_API_KEY")
        can_search = True
    except SystemExit:
        can_search = False
    for t in _history_titles(con):
        if t["rating_key"] in done:
            continue
        tmdb_id, source = _from_tautulli(t["rating_key"]), "tautulli"
        if tmdb_id is None and can_search and t["title"]:
            tmdb_id, source = tmdb.search(t["media_type"], t["title"], t["year"]), "search"
        if tmdb_id is None:
            source = "unresolved"
        con.execute(
            "INSERT OR REPLACE INTO titles VALUES (?,?,?,?,?,?)",
            (t["rating_key"], t["media_type"], t["title"], t["year"], tmdb_id, source),
        )
        con.commit()

    for r in con.execute(
        "SELECT media_type, source, COUNT(*) AS n FROM titles GROUP BY 1, 2 ORDER BY 1, 2"
    ):
        print(f"{r['media_type']:6} {r['source']:11} {r['n']:>5}")
    if not can_search:
        print("TMDB_API_KEY not set: unresolved titles were not searched; rerun once it is.")
    print("unresolved share of completed plays, per evaluated user:")
    for r in con.execute(
        """
        SELECT u.user_id,
               AVG(t.tmdb_id IS NULL) AS share
        FROM users u
        JOIN plays p ON p.user_id = u.user_id AND p.watched_status = 1
        LEFT JOIN titles t ON t.rating_key = COALESCE(p.grandparent_rating_key, p.rating_key)
        WHERE u.evaluated GROUP BY u.user_id ORDER BY share DESC
        """
    ):
        flag = "  <-- above 5%" if r["share"] > 0.05 else ""
        print(f"  {r['user_id']:>10} {r['share']:6.1%}{flag}")
