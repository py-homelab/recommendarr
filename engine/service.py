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
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from harness import db
from harness.llm import GENRES

from . import build

NAME = "recommendarr"
VERSION = "0.3.1"
PORT = int(os.environ.get("ENGINE_PORT", "8090"))
BUILD_HOUR = int(os.environ.get("ENGINE_BUILD_HOUR", "4"))
TOKEN = os.environ.get("ENGINE_TOKEN", "")
STALE_AFTER = 24 * 3600        # a start with no build, or one older than this, builds first
# Lists older than this are no longer served to Shortlist's rows (it falls back to its own engine) and
# /healthz reports unhealthy: two missed nightlies plus slack. Serving them is fine for a day; claiming
# they are current for two is what hid a failing build for two days.
STALE_ALERT = int(os.environ.get("ENGINE_STALE_ALERT_HOURS", "50")) * 3600
DEFAULT_LIMIT = 200
MAX_BODY = 4 * 1024 * 1024     # a person's history plus a library's ids is well under a megabyte
_build_lock = threading.Lock()
GENRE_IDS = {name.lower(): gid for gid, name in GENRES.items()}


ATTEMPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS build_attempts (
    started_at INTEGER PRIMARY KEY,
    finished_at INTEGER,
    ok INTEGER,                           -- NULL while running
    reason TEXT,
    error TEXT
);
"""


def build_status() -> dict:
    """What /healthz and /v1/info report: the newest successful build, the newest attempt and how it
    ended, and whether the lists are stale enough that serving them as current would be a lie."""
    con = db.connect()
    try:
        con.executescript(ATTEMPTS_SCHEMA)
        con.execute("CREATE TABLE IF NOT EXISTS builds (built_at INTEGER PRIMARY KEY, users INTEGER, seconds REAL, network_calls INTEGER)")
        built = con.execute("SELECT MAX(built_at) FROM builds").fetchone()[0]
        last = con.execute("SELECT * FROM build_attempts ORDER BY started_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    now = time.time()
    running = last is not None and last["ok"] is None
    stale = (built is None and last is not None and last["ok"] == 0) or (built is not None and now - built > STALE_ALERT)
    return {
        "ready": built is not None and not stale,
        "built_at": built,
        "age_hours": round((now - built) / 3600, 1) if built else None,
        "stale": stale,
        "building": running,
        "last_build_at": last["started_at"] if last else None,
        "last_build_ok": None if last is None or last["ok"] is None else bool(last["ok"]),
        "last_build_error": last["error"] if last and last["ok"] == 0 else None,
    }


def last_build() -> int | None:
    con = db.connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS builds (built_at INTEGER PRIMARY KEY, users INTEGER, seconds REAL, network_calls INTEGER)")
        return con.execute("SELECT MAX(built_at) FROM builds").fetchone()[0]
    finally:
        con.close()


def _record_attempt(started: int, **fields) -> None:
    con = db.connect()
    try:
        con.executescript(ATTEMPTS_SCHEMA)
        if "ok" not in fields:
            con.execute("INSERT OR REPLACE INTO build_attempts (started_at, reason) VALUES (?, ?)", (started, fields["reason"]))
        else:
            con.execute(
                "UPDATE build_attempts SET finished_at = ?, ok = ?, error = ? WHERE started_at = ?",
                (int(time.time()), int(fields["ok"]), fields.get("error"), started),
            )
        con.commit()
    finally:
        con.close()


def run_build(reason: str) -> None:
    with _build_lock:
        started = int(time.time())
        print(f"build starting ({reason})")
        _record_attempt(started, reason=reason)
        try:
            build.build(db.connect())
        except Exception as exc:  # keep serving the previous lists — and say so everywhere
            traceback.print_exc()
            print(f"build failed ({reason}): {exc!r}")
            _record_attempt(started, ok=False, error=f"{type(exc).__name__}: {exc}"[:500])
        else:
            _record_attempt(started, ok=True)


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
        con.executescript(build.HOUSEHOLD_SCHEMA)
        hh = con.execute("SELECT * FROM households WHERE plex_id = ?", (plex_id,)).fetchone()
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
        # Who watches under this account, from its own last 12 months: "adult", "family" (a household
        # sharing the account with children — keep children's titles to their own row) or "kids" (a
        # child's own account). Null when the engine has not built a label for this person yet.
        "household": (
            {"label": hh["label"], "kids_share": round(hh["kids_share"], 3), "kids_titles": hh["kids_titles"],
             "window_titles": hh["window_titles"], "window_days": build.HOUSEHOLD_WINDOW_DAYS}
            if hh else None
        ),
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
        status = build_status()
        if status["stale"]:
            # Stale lists would rank everyone's rows from days-old viewing without anyone noticing; a
            # refusal makes Shortlist fall back to its own engine and say so in the run trace.
            return self._json(503, {"error": f"lists are stale (last good build {status['age_hours']} h ago; "
                                             f"last error: {status['last_build_error']})"})
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
            # 503 once the lists are stale (or the only build ever attempted failed): the process is up,
            # but a green healthcheck would say the lists are current when they are not.
            status = build_status()
            return self._json(503 if status["stale"] else 200, {"ok": not status["stale"], **status})
        if url.path == "/v1/info":
            if not self._authorised():
                return self._json(401, {"error": "bad token"})
            return self._json(200, {
                "name": NAME, "version": VERSION, "surfaces": sorted(build.SURFACES), "serves_cold": True,
                **build_status(),
            })
        if len(parts) == 3 and parts[:2] == ["api", "suggestions"] and parts[2].isdigit():
            q = parse_qs(url.query)
            limit = min(int(q.get("limit", [DEFAULT_LIMIT])[0]), 1000)
            family = q.get("family", ["exclude"])[0]
            return self._json(200, fetch(int(parts[2]), limit, family))
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args) -> None:
        print(f"{datetime.now():%H:%M:%S} {self.address_string()} {fmt % args}")


def households_built() -> bool:
    """Whether the last build wrote household labels (a build from before v0.3.0 did not)."""
    con = db.connect()
    try:
        con.executescript(build.HOUSEHOLD_SCHEMA)
        return con.execute("SELECT COUNT(*) FROM households").fetchone()[0] > 0
    finally:
        con.close()


def nightly() -> None:
    """Build on start if there is no build, it is stale, or it predates what this version serves
    (household labels), then every day at BUILD_HOUR — so a fresh deploy or an upgrade never serves
    an answer the nightly build would have filled in."""
    built = last_build()
    if built is None or time.time() - built > STALE_AFTER:
        run_build("no build yet" if built is None else "last build stale")
    elif not households_built():
        run_build("last build has no household labels")
    while True:
        now = datetime.now()
        target = now.replace(hour=BUILD_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        run_build("nightly")


def serve() -> None:
    problem = db.check_temp_dir()
    if problem:
        # Fail loudly at start: every build would die on its first big sort while the service kept
        # answering from its previous lists.
        raise SystemExit(f"engine cannot build: {problem}. Point SQLITE_TMPDIR at a writable directory.")
    threading.Thread(target=nightly, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # As PID 1 in a container Python ignores SIGTERM unless handled; stop serving cleanly.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"engine listening on :{PORT}")
    server.serve_forever()
    print("engine stopped")
