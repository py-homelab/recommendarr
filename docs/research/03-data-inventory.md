# Data inventory (read 2026-09-17, all read-only)

## Library (Tautulli `get_libraries`)

| Library | Type | Count |
|---|---|---|
| Movies | movie | 1,382 |
| TV Shows | show | 507 shows, 1,676 seasons, 21,515 episodes |
| Audiobooks | artist | ignore |

Movies genres are normalised nightly by Kometa from TMDb (merge map: e.g. Science Fiction →
Sci-Fi; TV Movie dropped). TV genres are raw TMDb combo strings ("Sci-Fi & Fantasy").

## Users (Tautulli `get_users` + `get_history` counts)

16 Tautulli users. Plays = history rows; a play is "completed" at `watched_status == 1`
(Tautulli's own threshold, default 85%).

| username | plays | movie | episode | last play |
|---|---|---|---|---|
| Nooblazor (Pavel, owner) | 23,679 | 1,286 | 22,385 | 2026-09-17 |
| hild39 | 8,623 | 493 | 8,131 | 2026-09-17 |
| allend661 | 1,668 | 253 | 1,415 | 2026-09-11 |
| jo47139 | 1,575 | 279 | 1,296 | 2026-09-17 |
| happyjo1 | 1,570 | 181 | 1,389 | 2026-09-06 |
| Local | 1,300 | 123 | 1,177 | 2023 |
| megan899 | 764 | 109 | 654 | 2026-09-16 |
| jrschel | 687 | 55 | 632 | 2026-09-17 |
| br8909 | 616 | 55 | 559 | 2026-08-28 |
| william.pe2 | 236 | 27 | 202 | 2026-06 |
| hkoli6 | 17 | 7 | 10 | 2026-08 |
| alosc1, ke8849, emma7065 | ≤5 | | | |
| jus9029, laineybo | 0 | | | |

So: **9 users with >100 completed plays**, 12 active in Shortlist. Collaborative filtering
trained on our own users alone is hopeless at this scale (9 × ~1,900 items); the
collaborative signal has to come from TMDb's co-watch graph or an external dataset
(see 04-design-direction.md).

### Pavel, distinct completed titles (deduped by show)

| Window | Distinct titles | Plays (movie / episode) |
|---|---|---|
| last 30 d | 26 | 10 / 741 |
| last 90 d | 86 | 69 / 2,162 |
| last 365 d | 184 | 255 / 5,964 |
| all time | 655 (391 movies, 264 shows) | 851 / 21,093 |

Movie : show ratio by distinct titles ≈ 3 : 2, by plays ≈ 1 : 25. Any media balance must
use distinct titles, not plays.

### The family-viewing signal (Pavel's account, last 60 days)

| Title | Player / platform | plays |
|---|---|---|
| Bluey Minisodes | BRAVIA VH21 / Android | 44 |
| Sesame Street | BRAVIA VH21 | 12 (+1 iPhone) |
| Cars, Cars 2, Cars 3 | BRAVIA VH21 | 3 / 2 / 4 |
| Bear in the Big Blue House | BRAVIA VH21 | 4 |
| 30 Rock, Breaking Bad, Sunny, Taskmaster, Silo | iPhone 172 · BRAVIA 84 | |

The living-room TV is a shared context under the owner's account; `player`/`platform` per
play is enough to cluster it out without asking anyone to change how they watch.

## Tautulli history row fields (per play)

`date, started, stopped, duration, play_duration, paused_counter, percent_complete,
watched_status, media_type (movie|episode|track), rating_key, parent_rating_key,
grandparent_rating_key, title, parent_title, grandparent_title, full_title, year,
originally_available_at, guid, user, user_id, friendly_name, player, platform, product,
machine_id, ip_address, transcode_decision, group_count, group_ids, live, location`.

`guid` is the Plex GUID (`plex://movie/…`); TMDb ids come from the library item's GUIDs
(Plex `/library/metadata/{key}` → `Guid` children `tmdb://…`, `imdb://…`, `tvdb://…`).
Shortlist resolves the same way. Pull once per rating key and cache.

Endpoints used: `get_users`, `get_libraries`, `get_history&user_id=&media_type=&length=`
(supports `order_column=date&order_dir=desc`, `after=`/`before=` dates). ~24k rows for the
owner returned in one call without trouble.

### Additional sources used by the harness (added 2026-09-17)

- **Library membership over time**: Seerr `GET /api/v1/media?filter=allavailable` gives every
  library title keyed by `tmdbId` with `mediaAddedAt`. Tautulli's `get_library_media_info`
  is a stale cache (510 of 1,382 movies, last refreshed 2024-03) and refreshing it is a
  write, so it is not used.
- **Seerr request history**: `GET /api/v1/request?sort=added` — 1,216 requests 2023–2026,
  every one with `requestedBy.plexId` matching a Tautulli `user_id`. Ground truth for the
  missing-title surface.
- **Deleted titles**: `get_metadata` returns HTTP 400 for rating keys no longer in Plex
  (233 movies, 51 shows of the history); these fall back to a TMDb title+year search.
- **Plex watchlist** (per user, synced into Seerr for logged-in users) is the next intent
  source; not pulled yet.
- **picks `events` table** (live since 2026-09-17 23:44Z, homelab-stacks commit `149199d`,
  at `/mnt/ssd-storage/configs/picks/picks.db` on the NAS):
  `events(id, plex_id, tmdb_id, media_type 'movie'|'show', event shown|request|never|later|skip|undo,
  surface deck|grid|NULL, position, source DEFAULT 'shortlist', meta JSON, created_at ISO-8601 UTC)`.
  `shown` = card was the top card (deck) or tile ≥50% in viewport (grid), deduped per
  person/title/surface/UTC day, server-filtered to the user's current suggestion set.
  `position` = 0-based rank as served under the user's filter/sort at page load. Every action
  writes a row; `undo` meta `{"undone": ...}`, `request` meta `{"seerr_request_id", "status"}`.
  `skip` writes no dismissal and does not back-propagate. Existing tables unchanged. This is
  the exposure log the pooled feedback model and interleaving will need.

## Shortlist API shapes (owner token)

- `GET /api/users` → `[{id, username, display_name, plex_account_id, slug, enabled, …}]`
- `GET /api/requests?limit=` → inbox entries: `arr_slug, demand, detail, excluded, id,
  imdb_id, language, media_type, overview, poster_path, rating, row_slug, status, tags,
  title, tmdb_id, updated_at, vote_count, wanters[], why[{user,row,seed,source,row_slug}],
  year`. `?wanted_by=<username>` filters.
- `GET /api/runs?limit=`; `GET /api/runs/{id}` (`stats.requests_*`, `users[]`);
  `GET /api/runs/{id}/users/{uid}/trace` → `{trace: {history{total,recent[]},
  seeds[{title,media,weight,recency_days,watch_count}], gathers[{pool,sources[],
  discover_genres}], selection[]}, breakdown[], requests{}}`.
- `GET /api/collections` → row specs (see 01).

## Seerr (3.4.1)

- `GET /api/v1/user?take=200` → `plexId` ↔ Seerr `id`.
- `GET /api/v1/media` → what the library has / what is requested, keyed by `tmdbId` + type.
- `POST /api/v1/request` with header `X-API-User: <id>` files as that user (pending unless
  they have auto-approve); shows need `seasons: [n]`.
- Per-user quota `GET /api/v1/user/{id}/quota`.

## picks SQLite (per-user feedback, the label source)

`dismissals(plex_id, tmdb_id, media_type, kind never|later, until, title, year,
created_at)`, `requests_log(plex_id, tmdb_id, media_type, seerr_user_id, seerr_request_id,
seerr_status, created_at)`, `title_meta(tmdb_id, media_type, genres…)`, `backprop_log`.
Lives at `ssd-storage/configs/picks/picks.db` on the NAS (568:568, recordsize 16K).

## Re-pulling the samples (not copied into this repo)

The numbers above came from four JSON pulls on 2026-09-17. They were deliberately not copied
here because they carry friends' Plex usernames. Re-create them read-only into the gitignored
`docs/research/samples/`:

```bash
cd ~/Code/homelab-stacks && bash <<'EOF'
OUT=~/Code/recommendarr/docs/research/samples; mkdir -p "$OUT"
TOK=$(sops -d truenas/media/secrets.sops.env | sed -n 's/^SHORTLIST_API_TOKEN=//p' | tr -d '"'"'")
B=http://192.168.1.119:5959; H="Authorization: Bearer $TOK"
curl -sS -H "$H" "$B/api/requests?limit=5000" > "$OUT/inbox.json"
RID=$(curl -sS -H "$H" "$B/api/runs?limit=1" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
curl -sS -H "$H" "$B/api/runs/$RID" > "$OUT/run.json"
UID_=$(curl -sS -H "$H" "$B/api/users" | python3 -c 'import json,sys; print([u["id"] for u in json.load(sys.stdin) if u["username"]=="Nooblazor"][0])')
curl -sS -H "$H" "$B/api/runs/$RID/users/$UID_/trace" > "$OUT/trace.json"
KEY=$(sops -d truenas/monitoring/secrets.sops.env | sed -n 's/^TAUTULLI_API_KEY=//p' | tr -d '"'"'")
curl -sS "http://192.168.1.119:8181/api/v2?apikey=$KEY&cmd=get_users" > "$OUT/tautulli_users.json"
EOF
```
