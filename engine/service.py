"""HTTP API. Stdlib only. It listens on an internal network and trusts the plex_id it is given,
exactly as picks trusts nothing but the authentik claim — identity is resolved by the caller,
never here. A background thread rebuilds nightly.

For picks:
GET /healthz
GET /api/suggestions/<plex_id>?limit=200&family=exclude|include|only
    -> {"plex_id", "built_at", "items": [{title, year, rating, votes, tmdb_id, media_type,
        poster_path, overview, kids, why: [{seed, seed_tmdb_id, media_type}]}]}

For Shortlist (the engine protocol, docs/guides/engines.md in the Shortlist fork):
GET  /v1/info       -> {"name", "version", "surfaces", "serves_cold", "ready", "built_at"}
POST /v1/recommend  <- {plex_account_id, surface, media, limit_per_media, library, exclude,
                        excluded_genres, seeds, history, ...}
                    -> {"engine", "ordered": true, "items": [...], "trace": {...}}
Optional ENGINE_TOKEN: when set, /v1/* requires `Authorization: Bearer <token>`.
"""

import hmac
import json
import os
import signal
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from harness import db
from harness.llm import GENRES

from . import build

NAME = "recommendarr"
VERSION = "0.2.0"
PORT = int(os.environ.get("ENGINE_PORT", "8090"))
BUILD_HOUR = int(os.environ.get("ENGINE_BUILD_HOUR", "4"))
TOKEN = os.environ.get("ENGINE_TOKEN", "")
STALE_AFTER = 24 * 3600        # a start with no build, or one older than this, builds first
DEFAULT_LIMIT = 200
MAX_BODY = 4 * 1024 * 1024     # a person's history plus a library's ids is well under a megabyte
_build_lock = threading.Lock()
GENRE_IDS = {name.lower(): gid for gid, name in GENRES.items()}


def last_build() -> int | None:
    con = db.connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS builds (built_at INTEGER PRIMARY KEY, users INTEGER, seconds REAL, network_calls INTEGER)")
        return con.execute("SELECT MAX(built_at) FROM builds").fetchone()[0]
    finally:
        con.close()


def run_build(reason: str) -> None:
    with _build_lock:
        print(f"build starting ({reason})")
        try:
            build.build(db.connect())
        except Exception as exc:  # keep serving the previous lists
            print(f"build failed ({reason}): {exc!r}")


def fetch(plex_id: int, limit: int, family: str) -> dict:
    con = db.connect()
    try:
        where = {"exclude": "AND kids = 0", "only": "AND kids = 1"}.get(family, "")
        rows = con.execute(
            f"SELECT * FROM suggestions WHERE plex_id = ? {where} ORDER BY rank LIMIT ?", (plex_id, limit)
        ).fetchall()
        built = con.execute("SELECT MAX(built_at) FROM builds").fetchone()[0]
    finally:
        con.close()
    return {
        "plex_id": plex_id,
        "built_at": built,
        "items": [
            {
                "title": r["title"], "year": r["year"], "rating": r["rating"], "votes": r["votes"],
                "tmdb_id": r["tmdb_id"], "media_type": r["media_type"], "poster_path": r["poster_path"],
                "overview": r["overview"], "kids": bool(r["kids"]), "why": json.loads(r["why"] or "[]"),
            }
            for r in rows
        ],
    }


def _reason(why: list[dict]) -> str | None:
    if not why:
        return None
    first = why[0]
    if first.get("kind") == "next_in_series":
        return f"Next after {first['seed']}"
    return f"Because you watched {first['seed']}"


def recommend(req: dict) -> dict:
    """The engine protocol's answer for one request, straight from the nightly tables.

    The request's `library` (tmdb ids per media type) is honoured as the candidate universe on the
    library surface and as an exclusion set on the missing one; `exclude` and `excluded_genres` are
    applied as sent. The engine ranks from Tautulli's history, not the request's — that history is
    only counted against ours here so a mismatch shows in the trace.
    """
    plex_id = int(req["plex_account_id"])
    surface = req.get("surface") or "library"
    if surface not in build.SURFACES:
        raise ValueError(f"unknown surface {surface!r}")
    media = set(req.get("media") or ["movie", "show"])
    limit = int(req.get("limit_per_media") or DEFAULT_LIMIT)
    library = {m: set(ids) for m, ids in (req.get("library") or {}).items()}
    exclude = {(int(t), m) for t, m in (req.get("exclude") or [])}
    excluded_genres = {GENRE_IDS[g.lower()] for g in req.get("excluded_genres") or [] if g.lower() in GENRE_IDS}
    con = db.connect()
    try:
        rows = con.execute(
            f"SELECT * FROM {build.SURFACES[surface]} WHERE plex_id = ? ORDER BY rank", (plex_id,)
        ).fetchall()
        genres = {}
        if rows:
            marks = ",".join("(?,?)" for _ in rows)
            genres = {
                (r[0], r[1]): json.loads(r[2])
                for r in con.execute(
                    f"SELECT tmdb_id, media_type, genres FROM items WHERE (tmdb_id, media_type) IN ({marks})",
                    [v for r in rows for v in (r["tmdb_id"], r["media_type"])],
                )
            }
        built = con.execute("SELECT MAX(built_at) FROM builds").fetchone()[0]
        known_history = con.execute(
            "SELECT COUNT(*) FROM engagements WHERE user_id = ? AND label = 'positive'", (plex_id,)
        ).fetchone()[0]
    finally:
        con.close()
    items, per_media, dropped = [], {m: 0 for m in media}, {}
    for r in rows:
        key = (r["tmdb_id"], r["media_type"])
        fate = None
        if r["media_type"] not in media:
            fate = "other_media"
        elif surface == "library" and library and key[0] not in library.get(key[1], ()):
            fate = "not_in_these_libraries"
        elif surface == "missing" and key[0] in library.get(key[1], ()):
            fate = "in_library"
        elif key in exclude:
            fate = "excluded"
        elif excluded_genres and excluded_genres & set(genres.get(key, [])):
            fate = "excluded_genre"
        elif per_media[key[1]] >= limit:
            fate = "over_limit"
        if fate:
            dropped[fate] = dropped.get(fate, 0) + 1
            continue
        per_media[key[1]] += 1
        why = json.loads(r["why"] or "[]")
        items.append({
            "tmdb_id": r["tmdb_id"], "media_type": r["media_type"], "title": r["title"], "year": r["year"],
            "genres": [GENRES.get(g, str(g)) for g in genres.get(key, [])],
            "rating": r["rating"], "votes": r["votes"], "vote_count": r["votes"],
            "poster_path": r["poster_path"], "overview": r["overview"],
            "reason": _reason(why), "kids": bool(r["kids"]),
            "seed": {"tmdb_id": why[0]["seed_tmdb_id"], "title": why[0]["seed"], "media_type": why[0]["media_type"]} if why else None,
            "why": why,
        })
    sent_history = len(req.get("history") or [])
    return {
        "engine": {"name": NAME, "version": VERSION},
        "ordered": True,
        "items": items,
        "trace": {
            "built_at": built, "surface": surface, "ranked": len(rows), "returned": len(items), "dropped": dropped,
            # How the caller's view of this person compares with Tautulli's: a wide gap means one of
            # the two histories is stale, which is worth seeing before wondering about the ranking.
            "history_sent": sent_history, "history_known": known_history,
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        """The /v1 endpoints honour ENGINE_TOKEN when it is set; constant-time compare."""
        if not TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        return header.startswith("Bearer ") and hmac.compare_digest(header[7:], TOKEN)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/v1/recommend":
            return self._json(404, {"error": "not found"})
        if not self._authorised():
            return self._json(401, {"error": "bad token"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._json(400, {"error": "body required"})
        try:
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict) or "plex_account_id" not in req:
                raise ValueError("plex_account_id required")
            return self._json(200, recommend(req))
        except (ValueError, KeyError, TypeError) as exc:
            return self._json(400, {"error": f"bad request: {exc}"})

    def do_GET(self) -> None:
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if url.path == "/healthz":
            built = last_build()
            return self._json(200, {"ok": True, "ready": built is not None, "built_at": built})
        if url.path == "/v1/info":
            if not self._authorised():
                return self._json(401, {"error": "bad token"})
            built = last_build()
            return self._json(200, {
                "name": NAME, "version": VERSION, "surfaces": sorted(build.SURFACES), "serves_cold": True,
                "ready": built is not None, "built_at": built,
            })
        if len(parts) == 3 and parts[:2] == ["api", "suggestions"] and parts[2].isdigit():
            q = parse_qs(url.query)
            limit = min(int(q.get("limit", [DEFAULT_LIMIT])[0]), 1000)
            family = q.get("family", ["exclude"])[0]
            return self._json(200, fetch(int(parts[2]), limit, family))
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args) -> None:
        print(f"{datetime.now():%H:%M:%S} {self.address_string()} {fmt % args}")


def nightly() -> None:
    """Build on start if there is no build or it is stale, then every day at BUILD_HOUR, so
    a fresh deploy or a recreated container never serves an empty list."""
    built = last_build()
    if built is None or time.time() - built > STALE_AFTER:
        run_build("no build yet" if built is None else "last build stale")
    while True:
        now = datetime.now()
        target = now.replace(hour=BUILD_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        run_build("nightly")


def serve() -> None:
    threading.Thread(target=nightly, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # As PID 1 in a container Python ignores SIGTERM unless handled; stop serving cleanly.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"engine listening on :{PORT}")
    server.serve_forever()
    print("engine stopped")
