"""Read-only pull of watch history (Tautulli) and library + request history (Seerr)."""

from datetime import datetime

import requests

from . import config

PAGE = 10_000
SEERR_PAGE = 100
SEERR_MEDIA_TYPES = {"movie": "movie", "tv": "show"}


def tautulli(cmd: str, **params):
    r = requests.get(
        config.TAUTULLI_URL,
        params={"apikey": config.secret("TAUTULLI_API_KEY"), "cmd": cmd, **params},
        timeout=120,
    )
    if r.status_code == 400 and cmd == "get_metadata":
        return {}  # rating key no longer exists in Plex
    if not r.ok:
        # Never raise_for_status() here: its message embeds the URL, which carries the API key.
        raise RuntimeError(f"Tautulli {cmd}: HTTP {r.status_code}")
    response = r.json()["response"]
    if response["result"] != "success":
        raise RuntimeError(f"Tautulli {cmd}: {response.get('message')}")
    return response["data"]


def seerr(path: str, **params):
    r = requests.get(
        config.SEERR_URL + path,
        params=params,
        headers={"X-Api-Key": config.secret("SEERR_PICKS_API_KEY")},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def seerr_pages(path: str, **params):
    skip = 0
    while True:
        page = seerr(path, take=SEERR_PAGE, skip=skip, **params)
        yield from page["results"]
        skip += SEERR_PAGE
        if skip >= page["pageInfo"]["results"]:
            return


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def pull_history(con) -> None:
    users = [u for u in tautulli("get_users") if u["user_id"]]
    for u in users:
        con.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET username = excluded.username",
            (u["user_id"], u["username"]),
        )
        start = 0
        while True:
            page = tautulli(
                "get_history",
                user_id=u["user_id"],
                grouping=0,
                start=start,
                length=PAGE,
                order_column="date",
                order_dir="asc",
            )
            rows = [r for r in page["data"] if r["media_type"] in ("movie", "episode")]
            con.executemany(
                "INSERT OR REPLACE INTO plays VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["row_id"], r["user_id"], r["date"], r["media_type"],
                        r["rating_key"] or None, r["grandparent_rating_key"] or None,
                        r["title"], r["grandparent_title"], r["year"] or None,
                        r["originally_available_at"], r["parent_media_index"] or None,
                        r["media_index"] or None, r["play_duration"], r["percent_complete"],
                        r["watched_status"], r["player"], r["platform"], r["product"],
                    )
                    for r in rows
                ],
            )
            start += PAGE
            if start >= page["recordsFiltered"]:
                break
    con.execute(
        "UPDATE users SET completed_plays = "
        "(SELECT COUNT(*) FROM plays p WHERE p.user_id = users.user_id AND p.watched_status = 1)"
    )
    con.execute(
        "UPDATE users SET evaluated = completed_plays > ?", (config.MIN_COMPLETED_PLAYS,)
    )
    con.commit()


def pull_library(con) -> None:
    rows = []
    for m in seerr_pages("/media", filter="allavailable"):
        media_type = SEERR_MEDIA_TYPES.get(m["mediaType"])
        if media_type and m.get("tmdbId"):
            rows.append((m["tmdbId"], media_type, _epoch(m.get("mediaAddedAt")), m["status"]))
    con.execute("DELETE FROM library")
    con.executemany("INSERT OR REPLACE INTO library VALUES (?,?,?,?)", rows)
    con.commit()


def pull_requests(con) -> None:
    rows = []
    for q in seerr_pages("/request", sort="added"):
        media = q.get("media") or {}
        media_type = SEERR_MEDIA_TYPES.get(q["type"])
        if media_type and media.get("tmdbId"):
            rows.append(
                (
                    q["id"], (q.get("requestedBy") or {}).get("plexId"), media["tmdbId"],
                    media_type, _epoch(q["createdAt"]), q["status"],
                )
            )
    con.execute("DELETE FROM seerr_requests")
    con.executemany("INSERT OR REPLACE INTO seerr_requests VALUES (?,?,?,?,?,?)", rows)
    con.commit()


def run(con) -> None:
    pull_history(con)
    pull_library(con)
    pull_requests(con)
    for label, sql in [
        ("users", "SELECT COUNT(*) FROM users"),
        ("evaluated users", "SELECT COUNT(*) FROM users WHERE evaluated"),
        ("plays", "SELECT COUNT(*) FROM plays"),
        ("library movies", "SELECT COUNT(*) FROM library WHERE media_type = 'movie'"),
        ("library shows", "SELECT COUNT(*) FROM library WHERE media_type = 'show'"),
        ("library rows without added_at", "SELECT COUNT(*) FROM library WHERE added_at IS NULL"),
        ("seerr requests", "SELECT COUNT(*) FROM seerr_requests"),
        ("seerr requests with a Plex user", "SELECT COUNT(*) FROM seerr_requests WHERE user_id IS NOT NULL"),
    ]:
        print(f"{label:34} {con.execute(sql).fetchone()[0]:>7}")
