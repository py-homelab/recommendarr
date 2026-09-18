"""TMDb v3 client with an on-disk response cache.

TMDb's API terms cap caching at six months, hence the TTL."""

import json
import sqlite3
import threading
import time
import zlib

import requests

from . import config

TTL_SECONDS = 180 * 86400
MIN_INTERVAL = 1 / 30  # requests per second ceiling, shared across threads
KIND = {"movie": "movie", "show": "tv"}

_lock = threading.Lock()
_rate_lock = threading.Lock()
_last_request = 0.0
_con = None
_session = requests.Session()
network_calls = 0


def _cache() -> sqlite3.Connection:
    global _con
    if _con is None:
        config.DATA_DIR.mkdir(exist_ok=True)
        _con = sqlite3.connect(config.DATA_DIR / "tmdb_cache.db", check_same_thread=False)
        _con.execute("PRAGMA journal_mode=WAL")
        _con.execute(
            "CREATE TABLE IF NOT EXISTS responses "
            "(key TEXT PRIMARY KEY, fetched_at INTEGER NOT NULL, body BLOB NOT NULL)"
        )
    return _con


def _throttle() -> None:
    global _last_request
    with _rate_lock:
        wait = _last_request + MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def get(path: str, **params) -> dict | None:
    """GET a TMDb path. Returns None for a 404 (deleted or unknown id)."""
    global network_calls
    key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    with _lock:
        row = _cache().execute(
            "SELECT body FROM responses WHERE key = ? AND fetched_at > ?",
            (key, time.time() - TTL_SECONDS),
        ).fetchone()
    if row:
        return json.loads(zlib.decompress(row[0]))

    api_key = config.secret("TMDB_API_KEY")
    headers, query = {}, dict(params)
    if api_key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        query["api_key"] = api_key
    for attempt in range(5):
        _throttle()
        r = _session.get(config.TMDB_URL + path, params=query, headers=headers, timeout=30)
        if r.status_code != 429 and r.status_code < 500:
            break
        time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
    network_calls += 1
    if r.status_code == 404:
        body = None
    elif not r.ok:
        # Never raise_for_status() here: its message embeds the URL, which may carry the API key.
        raise RuntimeError(f"TMDb {path}: HTTP {r.status_code}")
    else:
        body = r.json()
    with _lock:
        _cache().execute(
            "INSERT OR REPLACE INTO responses VALUES (?, ?, ?)",
            (key, int(time.time()), zlib.compress(json.dumps(body).encode())),
        )
        _cache().commit()
    return body


def search(media_type: str, title: str, year: int | None) -> int | None:
    kind = KIND[media_type]
    year_param = "year" if kind == "movie" else "first_air_date_year"
    date_field = "release_date" if kind == "movie" else "first_air_date"
    attempts = [{year_param: year}, {}] if year else [{}]
    for extra in attempts:
        results = (get(f"/search/{kind}", query=title, **extra) or {}).get("results") or []
        for hit in results:
            hit_year = (hit.get(date_field) or "")[:4]
            if not year or (hit_year.isdigit() and abs(int(hit_year) - year) <= 1):
                return hit["id"]
    return None
