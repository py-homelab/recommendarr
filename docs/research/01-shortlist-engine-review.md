# How Shortlist v1.8.0 recommends — source review and live diagnosis (2026-09-17)

Reviewed from a clone of `stevezau/shortlist` at tag `v1.8.0` (the image we run), plus the
live instance's settings, row definitions, last run (id 13) and Pavel's per-user trace. All
reads were read-only. Line references are to that tag.

## 1. Algorithm class

A hybrid **item-to-item recommender that outsources similarity to TMDb.** No model is
trained on our users; the only cross-user signal is the "demand" count on missing titles.

### Pipeline (per user, nightly at 03:30)

1. **Seeds** — `engine/history.py::derive_seeds`. The last **30 distinct** watched titles,
   weighted **purely by recency**: `0.5 ** (days / 45)`. Watch count is deliberately not
   scored (an 18× rewatch years ago must not dominate). Each media type present is
   guaranteed ≥ a third of the seed budget. Optional `seed_window` cycling (off for all our
   rows). Plex ratings ≤ 2.0 remove a seed (`dislike_threshold`), only if the account's
   ratings look human (≥80% whole numbers).
2. **Candidates** — `engine/candidates.py::gather_candidates`, sources from
   `candidates.sources` (**ours: `tmdb_similar`, `tmdb_discover`**):
   - `tmdb_similar`: for each seed, TMDb `/{movie|tv}/{id}/recommendations` (weight 1.0;
     built from TMDb users' favourites and ratings — TMDb has no watch data, so this is
     collaborative filtering over its contributor community, not co-watching) **and**
     `/similar` (weight 0.6, genre/keyword based). Affinity = weight × position decay (bottom of list = half of top),
     × `genre_coherence` (penalises genres the candidate has that the seed lacks, floor 0.5).
   - `tmdb_discover`: 40 titles, `sort_by=popularity.desc`, `vote_count>=200`, in the top 3
     genres of the seeds (weighted). No seed provenance.
   - `trakt` (related) and `llm_web` (LLM web search via the "curator") exist but are **off**.
     The configured Gemini curator is therefore **idle**: the LLM never ranks, never writes
     reasons (`engine/picker.py` docstring), it only ever proposes titles via web search.
3. **Filter** — `filter_candidates`: must be **in a delivery library**, not watched by this
   user, not in an excluded genre. Identity is `(tmdb_id, media_type)`.
4. **Score** — `engine/ranking.py::score`:
   `(1 + seed_frequency) × rating(5.0 if unrated) × (1 + max seed weight) × affinity ×
   recency_factor`, where `recency_factor = 0.5 ** (age_years / 8 × recency)`. Our
   `recommendations.recency = 0.5` → 16-year half-life on release date. Undated titles get 1.0.
5. **Cut and pick** — `pre_rank`: round-robin across *sources* to `candidates_pre_rank` (80),
   per media type; `diversify_by_seed`: one title per seed per pass until the row is full
   (15). Delivered as a per-user Plex collection per library, `pick_order=best`.

### Rows configured on our instance

| slug | media | size | notes |
|---|---|---|---|
| `picked` "✨ {library} Picked for You" | both | 15 | defaults |
| `library_name_you_ve_already_seen` "☕ …you've already seen" | both | 15 | `rewatch=True`, `watched_pct=1.0`, refresh 11 d |
| `new_library_name_to_try` "🌱 New … to try" | both | 15 | refresh nightly |
| `tonight_s_library_name` "🍿 Tonight's Movies" | movie | 10 | refresh 7 d |
| `more_library_name_to_watch` "📺 More TV to watch" | show | 10 | `unstarted_only=True` |

Global: `max_seeds 30`, `recency 0.5`, `refresh_days 8`, `cold_start popular`,
`min_history 10`, `dislike_threshold 2.0`, `use_plex_ratings True`.

All rows share one gathered pool per user (`RowPolicy.pool_key`), so the four rows differ
only in filtering and cut, not in candidates.

## 2. The missing-title inbox is built differently — this is the diagnosis

`engine/rows.py::_record_demand` feeds `requests_mod.collect_missing(pools[0], …)` — **the
raw gathered pool** (every TMDb return for every seed), not the ranked cut. Docstring of
`collect_missing`: *"Watched/excluded/stale filtering is intentionally NOT applied."* So the
inbox gets **none** of step 4: no affinity, no age decay, no genre coherence, no diversify.

Then `engine/requests.py`:

- `accumulate` merges per-row demand maps; `demand` = number of distinct wanters.
- `_gate_rows` → `_gate_by_source` (our `rating_source = imdb` via MDBList): the pool is
  sorted **by demand desc, then TMDb rating, then votes**, and walked with a budget of
  `max(100, 4 × requests.max_per_run)` **live lookups per run** (cached ratings are free to
  walk past). Floors: `min_rating 7.0` (IMDb), `min_votes 5000`, `min_year 1969`,
  `language prefer en`, `min_demand 1`.
- `auto_send=False`, so everything qualifying is queued for the owner. picks reads this queue.

### Last run (id 13, 2026-09-17 08:30 UTC), all 12 users

| Metric | Value |
|---|---|
| `requests_wanted` (distinct missing titles across users) | 7,595 |
| `requests_pool` (per-row sum) | 16,304 |
| `requests_examined` (walked) | 1,752 |
| `requests_lookups` (live IMDb) | 100 |
| `requests_queued` | 340 |

### Live inbox composition (348 pending)

| | All | Pavel (`Nooblazor`) |
|---|---|---|
| Shows / movies | 252 / 96 | 155 / 42 |
| Year median | 2010 | 2012 (movies 2005, shows 2014) |
| Decades | 60s 2 · 70s 18 · 80s 32 · 90s 45 · 00s 74 · 10s 118 · 20s 59 | |
| Demand (distinct wanters) | **min 3**, max 9 | |
| `why.source` | tmdb_similar 3,468 · tmdb_discover 124 | |

Consequences:

1. **The inbox is the demand head.** Nothing wanted by only 1–2 people has ever been rated,
   so the personal tail never appears. What survives is what many users' seeds point at:
   generic, well-known, older titles (The Jeffersons: 9 of 12 users).
2. **Old skew**: demand-first ordering plus IMDb ≥ 7.0 / 5,000 votes favours canon; picks
   then sorts by rating by default, compounding it.
3. **TV skew**: Pavel's seeds are 18 shows / 12 movies; ~40 TMDb returns per seed; the
   1,382-movie library already holds most popular movie suggestions while the 507-show TV
   library holds far fewer popular show suggestions, so the *missing* pool is TV-heavy.
   Nothing balances media in the inbox (the rows are per library, so they never had to).
4. **Family viewing pollutes the owner's seeds**: Bluey, Bluey Minisodes, Sesame Street,
   Bear in the Big Blue House, Cars 1–3 are among Pavel's 30 seeds; his movie discover genres
   came out Animation/Family/Adventure. Tautulli shows every one of those plays came from the
   living-room TV (`BRAVIA VH21`), while 30 Rock / Breaking Bad / Sunny are mostly iPhone.

### Pavel's trace, run 13

- seeds: 30 (18 show, 12 movie); history total 486 (Shortlist's view; Tautulli says 655
  distinct completed).
- gather: tmdb_similar contributed 978, tmdb_discover 40 (sample of returned release years:
  2010s 126 · 2020s 86 · 2000s 68 · 1990s 35 · older 24).
- per-row in-library candidates after filtering were small (Movies 24–57, TV 20–38), cut cap
  80, so the Plex rows are fine and mostly carried forward between refresh nights.

## 3. Cheap mitigations available without a new engine (not applied)

- picks: interleave movies/shows; default sort newest; "from year" filter. Reorders the head only.
- Shortlist: raise `requests.max_per_run` (auto-send is off, so this only widens the lookup
  budget: 4× the value, floor 100; MDBList free quota ≈ 1,000/day, so 50 → 200/night is
  safe); raise `requests.min_year`; per-user "Don't seed" on the kids' titles, or a Plex Home
  profile for them.
- Upgrade to 1.9.1: adds genre-avoidance / franchise / cast dials to `score` and native Seerr
  routing; does **not** change the demand-first inbox walk.
- Upstream feature request: rank the request pool with the row score, or expose a per-user
  missing list.

## 4. What Shortlist is good at, and why we keep it for now

`engine/privacy.py` (972 lines), the share-filter machinery (`label!=Shortlist_*` per
shared account, backed up before modification), hub anchoring, and the collection reconcile
service are the hard, Plex-specific parts, and they work. The in-library rows get the full
ranking. The gap is specifically the missing-title side, which is what the new engine
targets first.
