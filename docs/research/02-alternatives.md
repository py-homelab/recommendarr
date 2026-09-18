# The field — what other projects do for per-user recommendations (source reviews, 2026-09-17)

Three reviewers read the recommendation code of each project (not just the README). Stars
and dates as of 2026-09-17. "Baseline" below means Shortlist's row ranking: per recent watch,
TMDb `/recommendations` + `/similar`, filter, rank by rating × affinity × age decay.

## Summary

| Project | Method | Per user | Surfaces | LLM | Ranking quality vs baseline |
|---|---|---|---|---|---|
| **Shortlist** 1.8 (ours) | TMDb recs+similar per seed, scored, diversified by seed | yes (share tokens) | both | idle (web-search only) | baseline for rows; **inbox unranked** |
| **SuggestArr** (1,315★, active) | TMDb `/recommendations` per recent watch, boolean filters, **TMDb order** | yes (Plex `accountID`) | missing only | optional | weaker: no scoring at all |
| **Recommendarr** (upstream deleted; fork 2025-04) | one chat prompt with the library pasted in | one app-login per Plex user | missing only | required | weakest; abandoned |
| **Immaculaterr** (75★, active) | one seed per watch → TMDb recs/similar/discover pools, TF-IDF overview similarity, points table across runs, optional OpenAI final pick | yes (`plexUserId`) | both | optional | ≈ baseline; better diversification, no user profile |
| **Curatarr** (35★, 2026-08) | **weighted per-user profile** (genre/director/studio/actor/keyword counters × recency × rating × rewatch, negative weights), scores every unwatched title; missing via TMDb discover by profile genres/keywords sorted by vote average | yes (Plex Home `switchUser`, Tautulli by email) | both | none | **best profile**; old-canon bias on missing side |
| **SeekAndWatch** (72★, active) | owner's server-wide history → TMDb recs, popularity sort, shuffled | **no** | missing only | none | not per user |
| **Streamystats** (810★, Jellyfin) | embeddings of every item (any OpenAI-compatible embedding API, pgvector); per-user nearest unwatched from >50%-watched sessions; `GET /api/recommendations?targetUserId=` | yes | in-library | embedding model | **best engine found**, Jellyfin only |
| **jellyfin-plugin-localrecs** (55★) | TF-IDF vectors over genres/actors/directors/tags/decade; recency-decayed user profile; cosine | yes | in-library | none | strong, Jellyfin only |
| **jellyfin-helper** (57★) | heuristic + learned + MLP ensemble, 38 features, Seerr discovery tab | yes | both | none | unverified claims, Jellyfin only |
| netplexflix Movie/TV-Recommendations-for-Plex (37★, dormant 2025-04) | profile of genres/director/actors/keywords weighted by frequency and Plex ratings; missing via Trakt personalised recs after uploading history | yes | both | none | script, dormant |
| playlisterr / PlexMind (≤6★, 2026-08) | local LLM picks owned unwatched titles per user → playlists | yes | in-library | required (local) | prompt-only |

## Details worth keeping

### SuggestArr (`giuseppe99barchetta/SuggestArr`)
- Seeds: Plex `/status/sessions/history/all?accountID=…` newest first, deduped per series,
  `max_content` 10; optional Trakt history merge.
- Per seed: TMDb `/recommendations` only (not `/similar`), paged.
- `_apply_filters`: rating, votes, language, year window, genres, keywords, runtime,
  providers, optional OMDb. **No scoring**; TMDb order; caps per seed (3 movies / 2 TV).
- Only re-ranking: user thumbs in its own UI. Excludes library + Seer-requested.
- Files Seer requests per user. `GET /api/jobs/suggestions?status=…` exposes results.
- Python (Flask + aiohttp), recommendation core ≈ 3.5k lines.

### Immaculaterr (`ohmzi/Immaculaterr`, TypeScript/NestJS)
- `recommendations.service.ts::buildSimilarMovieTitles`: one just-watched seed (webhook,
  sessions poller, or Tautulli).
- Pools (`tmdb.service.ts::getSplitRecommendationCandidatePools`): recs ≤120, similar ≤120,
  discover on top-4 genres `vote_count>=150 sort=vote_average.desc` ≤200, upcoming window.
- `rankCandidates`: weighted sum of base heuristic (`voteAvg*2 + log10(votes) + popularity*0.02`),
  TF-IDF cosine over title+genre+overview vs seed, quality, novelty (Jaccard distance),
  indie score; weights per intent/lane. Wildcard lanes (other-language, hidden gems,
  "change of taste"). Optional OpenAI picks final N from top ~250.
- Cross-watch memory = points table with linear decay; **no taste vector**.
- Per-user collections, but library-visible to everyone; missing → Radarr/Sonarr/Seerr
  behind a swipe deck ("Observatory"). API is admin-session scoped.

### Curatarr (`OrchestratedChaos/curatarr`, Python) — **borrow `utils/scoring.py`**
- Profile (`recommenders/base.py::_get_managed_users_watched_data`): per user, every watched
  item's genres/directors/studios/actors/keywords/languages/collections into Counters with
  weight = recency tier (1.0/0.75/0.5/0.25/0.1 at 30/90/180/365 d) × Plex rating
  (**negative** for ≤3/10) × `log2(views)+1`.
- Scoring (`calculate_similarity_score`): genre 0.25 / director 0.05 / studio 0.10 /
  actor 0.20 / keyword 0.50 with fuzzy keyword match, corpus IDF, sqrt normalisation, TF-IDF
  penalty for rare-in-profile genres, popularity dampening above 50k votes. Weights
  redistribute when a dimension is empty. Franchise ordering (next unwatched sequel first).
  Optional tiered pick (60% top / 30% mid / 10% tail).
- Missing (`recommenders/external.py`): iterative TMDb discover by top-5 genres + top-10
  keywords, then `/similar` on top hits, plus Trakt lists; same scorer; threshold relaxes.
- No JSON API (HTML results + `/status.json`).

### Streamystats (`fredrikburmester/streamystats`, Jellyfin) — the reference design
- `apps/nextjs-app/lib/db/similar-statistics.ts`: embeds every library item, per user takes
  sessions >50% watched, cosine-nearest unwatched, "based on" explanations, per-user hide
  list. Real per-user API with the media server's own auth.

### Trakt / TMDb personalised endpoints — not usable for 12 users from one credential
- Trakt `GET /recommendations/movies|shows` is OAuth-required and personalised to the
  authenticated Trakt account; each viewer would need their own Trakt account + grant.
  Trakt's **item-level** `GET /movies/{id}/related` and `/shows/{id}/related` are public
  (client id only, ~1,000 GETs per 5 min) and are a usable second edge source for an item
  graph; Trakt's user base is TV-centric and largely scrobbled from Plex.
- TMDb v3 `/recommendations` and `/similar` are item-to-item, not personalised. The only
  per-account endpoint is v4 `/4/account/{id}/movie/recommendations`, which needs each
  person's TMDb account to approve a request token.

## Takeaways for our design

1. Every Plex tool converges on seeds → TMDb similarity → filters. Only Curatarr builds a
   real per-user profile; only the Jellyfin tools use vectors.
2. Nobody does: taste **clustering** per user, a **learned release-year preference**, a
   **media quota** from the user's own ratio, a **device/context split** for shared
   accounts, or an **offline evaluation**.
3. Curatarr's profile scorer and Immaculaterr's candidate-pool/lane mixing are the two
   pieces of prior art worth reading before writing ours.
