import os
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,          -- Tautulli user_id == Plex account id
    username TEXT,
    completed_plays INTEGER DEFAULT 0,
    evaluated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plays (
    row_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    date INTEGER NOT NULL,
    media_type TEXT NOT NULL,             -- movie | episode
    rating_key INTEGER,
    grandparent_rating_key INTEGER,
    title TEXT,
    grandparent_title TEXT,
    year INTEGER,
    originally_available_at TEXT,
    season INTEGER,
    episode INTEGER,
    play_duration INTEGER,
    percent_complete INTEGER,
    watched_status REAL,
    player TEXT,
    platform TEXT,
    product TEXT
);
CREATE INDEX IF NOT EXISTS plays_user ON plays (user_id, date);

-- One row per Plex rating key of a movie or show that appears in history.
CREATE TABLE IF NOT EXISTS titles (
    rating_key INTEGER PRIMARY KEY,
    media_type TEXT NOT NULL,             -- movie | show
    title TEXT,
    year INTEGER,
    tmdb_id INTEGER,
    source TEXT                           -- tautulli | search | unresolved
);

-- The library as Seerr sees it, keyed by TMDb id.
CREATE TABLE IF NOT EXISTS library (
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,             -- movie | show
    added_at INTEGER,
    seerr_status INTEGER,
    PRIMARY KEY (tmdb_id, media_type)
);
CREATE TABLE IF NOT EXISTS seerr_requests (
    request_id INTEGER PRIMARY KEY,
    user_id INTEGER,                      -- Plex account id, NULL for local Seerr users
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,             -- movie | show
    created_at INTEGER NOT NULL,
    status INTEGER
);
CREATE INDEX IF NOT EXISTS seerr_requests_user ON seerr_requests (user_id, created_at);
"""


def connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def check_temp_dir() -> str | None:
    """Prove SQLite can spill to disk, by making it: a temp table larger than its one-page cache.
    Returns None when it can, else the reason. A build that cannot spill fails on its first big sort."""
    import sqlite3 as _sqlite3

    try:
        config.DATA_DIR.mkdir(exist_ok=True)
        con = _sqlite3.connect(config.DATA_DIR / "temp_probe.db")
        try:
            con.execute("PRAGMA temp_store = FILE")
            con.execute("PRAGMA temp.cache_size = 1")
            con.execute("CREATE TEMP TABLE probe (x TEXT)")
            con.executemany("INSERT INTO probe VALUES (?)", (("x" * 200,) for _ in range(2000)))
            con.execute("SELECT x FROM probe ORDER BY x DESC").fetchall()
        finally:
            con.close()
            (config.DATA_DIR / "temp_probe.db").unlink(missing_ok=True)
    except _sqlite3.Error as exc:
        return f"SQLite cannot write temp files ({exc}); SQLITE_TMPDIR={os.environ.get('SQLITE_TMPDIR')!r}"
    return None
