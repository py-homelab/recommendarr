"""Turn raw plays into one engagement row per (user, title).

A movie is a positive once completed. A show is a positive at three distinct completed
episodes; one or two episodes, then nothing for 60 days, is a weak negative, as is a movie
abandoned below 30% and never resumed. Every scorer and split reads this table, never plays."""

import math
import time

SHOW_POSITIVE_EPISODES = 3
SHOW_FULL_ENGAGEMENT_EPISODES = 8
ABANDON_PERCENT = 30
ABANDON_DAYS = 60

SCHEMA = """
DROP TABLE IF EXISTS engagements;
CREATE TABLE engagements (
    user_id INTEGER NOT NULL,
    tmdb_id INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    first_at INTEGER NOT NULL,            -- first completed play (or first play if none)
    last_at INTEGER NOT NULL,
    completed_plays INTEGER NOT NULL,
    distinct_episodes INTEGER NOT NULL,
    max_percent INTEGER NOT NULL,
    engagement REAL NOT NULL,             -- 0..1, before recency weighting
    tv_share REAL NOT NULL,               -- share of plays on a shared TV device
    label TEXT NOT NULL,                  -- positive | weak_negative | neutral
    PRIMARY KEY (user_id, tmdb_id, media_type)
);
"""

ROWS_SQL = """
SELECT p.user_id, t.tmdb_id, t.media_type,
       MIN(CASE WHEN p.watched_status = 1 THEN p.date END) AS first_completed,
       MIN(p.date) AS first_play,
       MAX(p.date) AS last_at,
       SUM(p.watched_status = 1) AS completed_plays,
       COUNT(DISTINCT CASE WHEN p.watched_status = 1 THEN p.rating_key END) AS distinct_episodes,
       MAX(p.percent_complete) AS max_percent,
       AVG(p.product LIKE '%TV%' OR p.player LIKE '%BRAVIA%' OR p.platform IN ('Roku', 'tvOS', 'webOS', 'Tizen', 'Vizio Blink', 'Android TV')) AS tv_share
FROM plays p
JOIN titles t ON t.rating_key = COALESCE(p.grandparent_rating_key, p.rating_key)
WHERE t.tmdb_id IS NOT NULL
GROUP BY p.user_id, t.tmdb_id, t.media_type
"""


def classify(media_type, completed_plays, distinct_episodes, max_percent, last_at, now):
    stale = (now - last_at) > ABANDON_DAYS * 86400
    if media_type == "movie":
        if completed_plays > 0:
            return 1.0, "positive"
        if max_percent < ABANDON_PERCENT and stale:
            return 0.0, "weak_negative"
        return 0.0, "neutral"
    if distinct_episodes >= SHOW_POSITIVE_EPISODES:
        return min(1.0, distinct_episodes / SHOW_FULL_ENGAGEMENT_EPISODES), "positive"
    if stale:
        return 0.0, "weak_negative"
    return 0.0, "neutral"


def recency_weight(first_at: int, cutoff: int) -> float:
    """Two timescales: a slow ~2-year half-life plus a 60-day boost for what is current."""
    days = max(0.0, (cutoff - first_at) / 86400)
    return 0.5 ** (days / 730) + 0.5 * 0.5 ** (days / 60)


def seed_weight(row, cutoff: int) -> float:
    """Engagement × capped rewatch bonus × recency, for use as a scorer's seed weight."""
    rewatch = 1 + math.log1p(max(0, row["completed_plays"] - 1)) / 3
    return row["engagement"] * min(rewatch, 2.0) * recency_weight(row["first_at"], cutoff)


def run(con) -> None:
    now = int(time.time())
    con.executescript(SCHEMA)
    rows = []
    for r in con.execute(ROWS_SQL):
        engagement, label = classify(
            r["media_type"], r["completed_plays"], r["distinct_episodes"],
            r["max_percent"], r["last_at"], now,
        )
        rows.append(
            (
                r["user_id"], r["tmdb_id"], r["media_type"],
                r["first_completed"] or r["first_play"], r["last_at"], r["completed_plays"],
                r["distinct_episodes"], r["max_percent"], engagement, r["tv_share"], label,
            )
        )
    con.executemany("INSERT INTO engagements VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    print("label counts per evaluated user (movie positives / show positives / weak negatives):")
    for r in con.execute(
        """
        SELECT e.user_id,
               SUM(label = 'positive' AND media_type = 'movie') AS mp,
               SUM(label = 'positive' AND media_type = 'show') AS sp,
               SUM(label = 'weak_negative') AS neg
        FROM engagements e JOIN users u ON u.user_id = e.user_id
        WHERE u.evaluated GROUP BY e.user_id ORDER BY mp + sp DESC
        """
    ):
        print(f"  {r['user_id']:>10} {r['mp']:>5} {r['sp']:>5} {r['neg']:>5}")
