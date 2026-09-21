"""HTTP API. Stdlib only. It listens on an internal network and trusts the plex_id it is given,
exactly as picks trusts nothing but the authentik claim — identity is resolved by the caller,
never here. A background thread rebuilds nightly.

For picks:
GET /healthz
GET /api/suggestions/<plex_id>?limit=200&family=exclude|include|only
    -> {"plex_id", "built_at", "items": [{title, year, rating, votes, tmdb_id, media_type,
        poster_path, overview, kids, why: [{seed, seed_tmdb_id, media_type}]}]}

For Shortlist (the engine protocol, docs/guides/engines.md in the Shortlist fork):
GET  /v1/info       -> {"name", "version", "surfaces", "features", "serves_cold", "ready", "built_at", ...}
POST /v1/recommend  <- {plex_account_id, surface, media, limit_per_media, library, exclude,
                        excluded_genres, seeds, seed_focus, season, history, ...}
                    -> {"engine", "ordered": true, "items": [...], "trace": {...}}
Optional ENGINE_TOKEN: when set, /v1/* requires `Authorization: Bearer <token>`.
"""

import hmac
import json
import os
import signal
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from harness import db
from harness.llm import GENRES

from . import build, identity

NAME = "recommendarr"
VERSION = "0.5.0"
# What this engine does with the request beyond ranking, so Shortlist can say when a row setting has
# no effect: `season` narrows the candidates to the season's titles before the cut, `seed_focus` ranks
# by closeness to the request's seeds instead of the person's whole taste.
FEATURES = ["season", "seed_focus"]
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
            build.build(db.connect(), VERSION)
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
            f"SELECT * FROM suggestions WHERE plex_id = ? {where} ORDER BY rank LIMIT ?",
            (identity.canonical(plex_id), limit),  # a pooled profile reads its household's list
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


def _focus(con, seeds: list, rows: list) -> tuple[list, dict] | None:
    """The person's list re-ranked by closeness to a few watches (a "Because you watched X" row).

    A title's score is its summed similarity to the seeds, times a milder pull toward where the
    person's own ranking put it (0.5 at the bottom of their list, 1.0 at the top), so the row is about
    those watches first and this person's taste second. Titles like none of the seeds drop out.
    None when the build has no neighbour table yet (a build from before v0.4.0): unfocused, not wrong.
    """
    weights: dict[tuple, tuple[float, str]] = {}
    for s in seeds:
        try:
            weights[(int(s["tmdb_id"]), str(s["media_type"]))] = (float(s.get("weight") or 1.0), str(s.get("title") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    if not weights:
        return None
    marks = ",".join("(?,?)" for _ in weights)
    try:
        found = con.execute(
            f"SELECT tmdb_id, media_type, n_tmdb_id, n_media_type, sim FROM item_neighbours WHERE (tmdb_id, media_type) IN ({marks})",
            [v for k in weights for v in k],
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    score: dict[tuple, float] = {}
    best: dict[tuple, tuple[tuple, float]] = {}
    for r in found:
        seed, cand = (r[0], r[1]), (r[2], r[3])
        v = weights[seed][0] * r[4]
        score[cand] = score.get(cand, 0.0) + v
        if v > best.get(cand, (None, 0.0))[1]:
            best[cand] = (seed, v)
    n = max(len(rows), 1)
    placed = [
        (score[k] * (0.5 + 0.5 * (1 - i / n)), r)
        for i, r in enumerate(rows)
        if (k := (r["tmdb_id"], r["media_type"])) in score
    ]
    placed.sort(key=lambda p: -p[0])
    why = {
        cand: [{"seed": weights[seed][1], "seed_tmdb_id": seed[0], "media_type": seed[1], "kind": "similar"}]
        for cand, (seed, _v) in best.items()
    }
    return [r for _s, r in placed], why


def recommend(req: dict) -> dict:
    """The engine protocol's answer for one request, straight from the nightly tables.

    The request's `library` (tmdb ids per media type) is honoured as the candidate universe on the
    library surface and as an exclusion set on the missing one; `exclude` and `excluded_genres` are
    applied as sent. The engine ranks from Tautulli's history, not the request's — that history is
    only counted against ours here so a mismatch shows in the trace.
    """
    asked_for = int(req["plex_account_id"])
    # A pooled household profile is answered with the canonical account's lists (`identity`).
    plex_id = identity.canonical(asked_for)
    surface = req.get("surface") or "library"
    if surface not in build.SURFACES:
        raise ValueError(f"unknown surface {surface!r}")
    media = set(req.get("media") or ["movie", "show"])
    limit = int(req.get("limit_per_media") or DEFAULT_LIMIT)
    library = {m: set(ids) for m, ids in (req.get("library") or {}).items()}
    exclude = {(int(t), m) for t, m in (req.get("exclude") or [])}
    excluded_genres = {GENRE_IDS[g.lower()] for g in req.get("excluded_genres") or [] if g.lower() in GENRE_IDS}
    # A seasonal row: its season's titles are the candidates, cut after narrowing, not before.
    season = req.get("season") if isinstance(req.get("season"), dict) else None
    season_ids = {m: {int(t) for t in ids} for m, ids in season.items()} if season else None
    focus_why: dict = {}
    focused = None
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
        ranked_total = len(rows)
        if req.get("seed_focus") and req.get("seeds"):
            focused = _focus(con, req["seeds"], rows)
            if focused is not None:
                rows, focus_why = focused
        built = con.execute("SELECT MAX(built_at) FROM builds").fetchone()[0]
        con.executescript(build.HOUSEHOLD_SCHEMA)
        hh = con.execute("SELECT * FROM households WHERE plex_id = ?", (plex_id,)).fetchone()
        known_history = con.execute(
            "SELECT COUNT(*) FROM engagements WHERE user_id = ? AND label = 'positive'", (plex_id,)
        ).fetchone()[0]
        con.executescript(build.META_SCHEMA)
        recorded = con.execute("SELECT value FROM engine_meta WHERE key = 'identity_groups'").fetchone()
        identity_pending = (recorded[0] if recorded else "") != identity.normalised()
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
        elif season_ids is not None and key[0] not in season_ids.get(key[1], ()):
            fate = "not_in_season"
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
        why = focus_why.get(key) or json.loads(r["why"] or "[]")
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
             "window_titles": hh["window_titles"], "window_days": build.HOUSEHOLD_WINDOW_DAYS,
             # The pooled household this account belongs to (its canonical id), when it is in one.
             # Every account in a group gets the SAME counts above, so they cannot tell a children's
             # profile from the adults' one: the caller has to pin each profile's label itself, and
             # this field is how it can notice two grouped accounts it has left unpinned.
             **({"group": identity.group_of(asked_for)} if identity.group_of(asked_for) is not None else {})}
            if hh else None
        ),
        "items": items,
        "trace": {
            "built_at": built, "surface": surface, "ranked": ranked_total, "returned": len(items), "dropped": dropped,
            **({"seed_focus": {"seeds": len(req.get("seeds") or []), "like_them": len(rows),
                               "applied": focused is not None}} if req.get("seed_focus") else {}),
            # How the caller's view of this person compares with Tautulli's: a wide gap means one of
            # the two histories is stale, which is worth seeing before wondering about the ranking.
            "history_sent": sent_history, "history_known": known_history,
            # Answered from another account's lists: this one is pooled under it (`identity`). The
            # gap above is then structural, not staleness — one profile's history was sent, the
            # whole household's is known.
            **({"answered_as": plex_id} if plex_id != asked_for else {}),
            # The map changed and the rebuild for it has not finished (or failed): these lists were
            # pooled by the OLD map, so a newly added profile is reading its household's pre-pooling list.
            **({"identity_pending": True} if identity_pending else {}),
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
                "name": NAME, "version": VERSION, "surfaces": sorted(build.SURFACES), "features": FEATURES,
                "serves_cold": True,
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


def built_by() -> str | None:
    """The engine version that made the newest build — a new version rebuilds on start, so a change to
    what a build writes (a children's-title rule, say) reaches the lists before the next nightly."""
    con = db.connect()
    try:
        con.executescript(build.META_SCHEMA)
        row = con.execute("SELECT value FROM engine_meta WHERE key = 'built_by'").fetchone()
        return row[0] if row else None
    finally:
        con.close()


def identity_built() -> str:
    """The identity map the newest build pooled accounts by ("" when it pooled nobody, or predates it)."""
    con = db.connect()
    try:
        con.executescript(build.META_SCHEMA)
        row = con.execute("SELECT value FROM engine_meta WHERE key = 'identity_groups'").fetchone()
        return row[0] if row else ""
    finally:
        con.close()


def neighbours_built() -> bool:
    """Whether the last build wrote the neighbour table and full library lists (v0.4.0)."""
    con = db.connect()
    try:
        con.executescript(build.NEIGHBOURS_SCHEMA)
        return con.execute("SELECT 1 FROM item_neighbours LIMIT 1").fetchone() is not None
    finally:
        con.close()


def nightly() -> None:
    """Build on start if there is no build, it is stale, it predates what this version serves
    (household labels, neighbours, another engine version), or it pooled accounts by a different
    identity map than the one set now; then every day at BUILD_HOUR — so a fresh deploy, an upgrade
    or a changed map never serves an answer the nightly build would have filled in."""
    built = last_build()
    if built is None or time.time() - built > STALE_AFTER:
        run_build("no build yet" if built is None else "last build stale")
    elif not households_built():
        run_build("last build has no household labels")
    elif not neighbours_built():
        run_build("last build predates focused rows and full library lists")
    elif built_by() != VERSION:
        run_build(f"last build was made by {built_by() or 'an older version'}, not {VERSION}")
    elif identity_built() != identity.normalised():
        run_build(f"{identity.ENV} changed since the last build")
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
    try:
        identity.members()
    except ValueError as e:
        # At start, not at 03:00: a map the nightly build would choke on (or, worse, one quietly
        # ignored) leaves a household's profiles ungrouped with nothing on screen to say so.
        raise SystemExit(f"engine cannot start: {e}") from None
    threading.Thread(target=nightly, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # As PID 1 in a container Python ignores SIGTERM unless handled; stop serving cleanly.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"engine listening on :{PORT}")
    server.serve_forever()
    print("engine stopped")
