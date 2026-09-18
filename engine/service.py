"""HTTP API for picks. Stdlib only, no auth: it listens on an internal network and trusts
the plex_id in the path, exactly as picks trusts nothing but the authentik claim — identity
is resolved by picks, never here. A background thread rebuilds nightly.

GET /healthz
GET /api/suggestions/<plex_id>?limit=200&family=exclude|include|only
    -> {"plex_id", "built_at", "items": [{title, year, rating, votes, tmdb_id, media_type,
        poster_path, overview, kids, why: [{seed, seed_tmdb_id, media_type}]}]}
"""

import json
import os
import signal
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from harness import db

PORT = int(os.environ.get("ENGINE_PORT", "8090"))
BUILD_HOUR = int(os.environ.get("ENGINE_BUILD_HOUR", "4"))
STALE_AFTER = 24 * 3600        # a start with no build, or one older than this, builds first
DEFAULT_LIMIT = 200
_build_lock = threading.Lock()


def last_build() -> int | None:
    con = db.connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS builds (built_at INTEGER PRIMARY KEY, users INTEGER, seconds REAL, network_calls INTEGER)")
        return con.execute("SELECT MAX(built_at) FROM builds").fetchone()[0]
    finally:
        con.close()


def run_build(reason: str) -> None:
    from . import build

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


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if url.path == "/healthz":
            built = last_build()
            return self._json(200, {"ok": True, "ready": built is not None, "built_at": built})
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
