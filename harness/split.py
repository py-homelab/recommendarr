"""Persisted evaluation splits, so every scorer is measured on identical data.

Primary: arrival-time folds. For each quarterly cutoff T, a scorer sees engagements before
T and must rank the catalogue minus the library as of T. Positives are titles that arrived
after T and the user engaged with within the window, plus Seerr requests the user made in
the window for titles the library lacked at T. This simulates the missing-title surface.

Secondary: per user and media type, the most recent min(30, 20%) positives by first
engagement are held out."""

from datetime import date, datetime, timezone

WINDOW_DAYS = 180
MIN_TRAIN_POSITIVES = 10
FIRST_CUTOFF = date(2023, 7, 1)
HOLDOUT_MAX = 30
HOLDOUT_SHARE = 0.2

SCHEMA = """
DROP TABLE IF EXISTS folds;
DROP TABLE IF EXISTS fold_users;
DROP TABLE IF EXISTS fold_positives;
DROP TABLE IF EXISTS holdout;
CREATE TABLE folds (fold_id INTEGER PRIMARY KEY, cutoff INTEGER NOT NULL, window_end INTEGER NOT NULL);
CREATE TABLE fold_users (fold_id INTEGER, user_id INTEGER, train_positives INTEGER, PRIMARY KEY (fold_id, user_id));
CREATE TABLE fold_positives (
    fold_id INTEGER NOT NULL, user_id INTEGER NOT NULL, tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL, kind TEXT NOT NULL,      -- watched | requested
    PRIMARY KEY (fold_id, user_id, tmdb_id, media_type)
);
CREATE TABLE holdout (user_id INTEGER, tmdb_id INTEGER, media_type TEXT, PRIMARY KEY (user_id, tmdb_id, media_type));
"""


def quarterly_cutoffs(last_window_end: int):
    d = FIRST_CUTOFF
    while True:
        cutoff = int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp())
        window_end = cutoff + WINDOW_DAYS * 86400
        if window_end > last_window_end:
            return
        yield cutoff, window_end
        d = date(d.year + (d.month + 3 > 12), (d.month + 2) % 12 + 1, 1)


def build_folds(con, now: int) -> None:
    for fold_id, (cutoff, window_end) in enumerate(quarterly_cutoffs(now)):
        con.execute("INSERT INTO folds VALUES (?,?,?)", (fold_id, cutoff, window_end))
        con.execute(
            """
            INSERT INTO fold_users
            SELECT ?, e.user_id, COUNT(*) FROM engagements e JOIN users u ON u.user_id = e.user_id
            WHERE u.evaluated AND e.label = 'positive' AND e.first_at <= ?
            GROUP BY e.user_id HAVING COUNT(*) >= ?
            """,
            (fold_id, cutoff, MIN_TRAIN_POSITIVES),
        )
        con.execute(
            """
            INSERT INTO fold_positives
            SELECT ?, e.user_id, e.tmdb_id, e.media_type, 'watched'
            FROM engagements e
            JOIN library l ON l.tmdb_id = e.tmdb_id AND l.media_type = e.media_type
            JOIN fold_users fu ON fu.fold_id = ? AND fu.user_id = e.user_id
            WHERE e.label = 'positive'
              AND l.added_at > ? AND l.added_at <= ?
              AND e.first_at > ? AND e.first_at <= ?
            """,
            (fold_id, fold_id, cutoff, window_end, cutoff, window_end),
        )
        con.execute(
            """
            INSERT OR IGNORE INTO fold_positives
            SELECT ?, q.user_id, q.tmdb_id, q.media_type, 'requested'
            FROM seerr_requests q
            JOIN fold_users fu ON fu.fold_id = ? AND fu.user_id = q.user_id
            LEFT JOIN library l ON l.tmdb_id = q.tmdb_id AND l.media_type = q.media_type
            WHERE q.created_at > ? AND q.created_at <= ?
              AND (l.added_at IS NULL OR l.added_at > ?)
            """,
            (fold_id, fold_id, cutoff, window_end, cutoff),
        )


def build_holdout(con) -> None:
    for u in con.execute("SELECT user_id FROM users WHERE evaluated"):
        for media_type in ("movie", "show"):
            rows = con.execute(
                "SELECT tmdb_id FROM engagements WHERE user_id = ? AND media_type = ? "
                "AND label = 'positive' ORDER BY first_at DESC",
                (u["user_id"], media_type),
            ).fetchall()
            n = min(HOLDOUT_MAX, int(len(rows) * HOLDOUT_SHARE))
            con.executemany(
                "INSERT INTO holdout VALUES (?,?,?)",
                [(u["user_id"], r["tmdb_id"], media_type) for r in rows[:n]],
            )


def run(con, now: int) -> None:
    con.executescript(SCHEMA)
    build_folds(con, now)
    build_holdout(con)
    con.commit()
    report(con)


def report(con) -> None:
    users = [r["user_id"] for r in con.execute(
        "SELECT user_id FROM users WHERE evaluated ORDER BY completed_plays DESC")]
    print("arrival-time positives per fold (watched movies+shows / requested), one column per user:")
    print("cutoff      " + " ".join(f"{'u' + str(i):>9}" for i in range(len(users))))
    for f in con.execute("SELECT * FROM folds"):
        cells = []
        for uid in users:
            r = con.execute(
                "SELECT SUM(kind = 'watched') w, SUM(kind = 'requested') q FROM fold_positives "
                "WHERE fold_id = ? AND user_id = ?", (f["fold_id"], uid)).fetchone()
            cells.append(f"{(r['w'] or 0):>4}/{(r['q'] or 0):<4}" if r["w"] is not None else "        -")
        print(f"{datetime.fromtimestamp(f['cutoff'], timezone.utc).date()}  " + " ".join(cells))
    total = con.execute("SELECT COUNT(*) FROM fold_positives").fetchone()[0]
    print(f"total fold positives: {total}")
    print("secondary holdout sizes (movie/show):", [
        tuple(r) for r in con.execute(
            "SELECT SUM(media_type='movie'), SUM(media_type='show') FROM holdout GROUP BY user_id")])
