# Experiment log

Every scorer earns its place here. Protocol and adoption rule: `docs/research/04`, last
section. Reports live in the gitignored `reports/`; the numbers that matter are copied here.
NDCG@50 and recall@100 are macro averages over (fold × user) units on the arrival-time folds;
"test" = folds 8–10, which tuning never sees.

## 2026-09-17 — baselines (94 units, 9 users, 11 folds)

| scorer | NDCG@50 movie | NDCG@50 show | recall@100 movie | recall@100 show | pool recall | lift | Jaccard@50 | median year | movie share |
|---|---|---|---|---|---|---|---|---|---|
| B0 inbox as today | 0.025 | 0.013 | 0.047 | 0.089 | 0.07 | 1.12 | 0.28 | 2008 | 0.29 |
| B1 Shortlist row ranking | 0.013 | 0.015 | 0.033 | 0.083 | 0.10 | 1.42 | 0.06 | 2020 | 0.46 |
| B2 global popularity | 0.036 | 0.020 | 0.070 | 0.112 | 1.00 | 1.00 | 1.00 | 2010 | 0.92 |
| B3 household popularity | 0.033 | 0.017 | 0.070 | 0.112 | 1.00 | 0.99 | 0.97 | 2010 | 0.92 |
| B4 single-vector content kNN | 0.002 | 0.005 | 0.004 | 0.025 | 1.00 | 2.36 | 0.06 | 2015 | 0.63 |

Findings:
- B0 reproduces the live inbox (live: 28% movies, median year 2010, min demand 3) without
  being tuned to it — the harness measures the complaint.
- The one-hop TMDb seed pools contain only 7–10% of what users go on to watch. Ranking the
  pool better (B1) cannot fix recall; candidate generation is the ceiling.
- Plain popularity beats today's engine with CI excluding 0 for movies. It is the bar.
- Positives are bimodal in age (42% released within 2 years of the cutoff, the rest spread
  back decades) and popular (median 91st vote-count percentile among candidates).
- Protocol fixes made: titles anyone requested before a cutoff leave the candidate set (picks
  parks them; without this B3 "won" by knowing pending requests); B0's 5,000-IMDb-vote gate is
  proxied by 500/180 TMDb votes for movies/shows.

## 2026-09-17 — G1 graph PPR (personalised PageRank over TMDb recs+similar)

Config chosen on folds 0–7: restart 0.3, hub damping global-PR^1.0, similar edges 0.4,
popularity prior (1+votes)^1.0, all seeds weighted by engagement × recency.

Without a popularity prior the walk is worse than B1: it settles in dense niche clusters
(stand-up specials, anime) and TMDb's graph hubs are obscure action B-movies. TMDb's
`/recommendations` is noisy (The Matrix → Terminator Genisys, Geostorm, Automata). With the
prior:

| comparison | NDCG@50 movie Δ | NDCG@50 show Δ | notes |
|---|---|---|---|
| vs B1, test folds | +0.052 [+0.016, +0.095], 5W/0L | +0.010 [−0.005, +0.029], 3W/1L | clears the rule on movies |
| vs B2, test folds | +0.005 [−0.041, +0.064], 3W/3L | −0.000 [−0.020, +0.022], 2W/2L | tie |
| vs B2, all folds | +0.001 [−0.023, +0.026] | +0.011 [+0.003, +0.021], 6W/1L | includes tuning folds |

All folds: recall@100 movie 0.096 (B2 0.070), show 0.115 (B2 0.112); lift 2.00, Jaccard 0.09.

Decision: **adopted provisionally as the first blend component.** On accuracy it ties
popularity on the test folds (the rule's tie-break favours the simpler method), but
popularity fails the personalisation gate outright (identical list for everyone, which is
the complaint), and G1 delivers accuracy ≥ popularity while being personal. Its ceiling is
TMDb's graph quality; Trakt `related` edges and the content scorer are the next candidates
to lift it. The combined top-50 is 94% movies because movie vote counts dwarf TV's — the
media mix is a blend-stage calibration, not a scorer property.

## 2026-09-17 — C1 content, M1 MovieLens EASE, the blend, signals (final run)

All numbers: arrival-time folds, macro over 94 (fold × user) units unless marked "test"
(folds 8–10, 24 units, never used for tuning). Δ = NDCG@50 paired difference with
user-clustered bootstrap 95% CI and per-user wins/losses.

| scorer | NDCG@50 movie | NDCG@50 show | recall@100 movie | recall@100 show | lift | Jaccard@50 | median year | movie share |
|---|---|---|---|---|---|---|---|---|
| B0 inbox as today | 0.025 | 0.013 | 0.047 | 0.089 | 1.12 | 0.28 | 2008 | 0.29 |
| B1 Shortlist row ranking | 0.013 | 0.015 | 0.033 | 0.083 | 1.42 | 0.06 | 2020 | 0.46 |
| B2 global popularity | 0.036 | 0.020 | 0.070 | 0.112 | 1.00 | 1.00 | 2010 | 0.92 |
| G1 graph PPR | 0.037 | 0.031 | 0.096 | 0.115 | 2.00 | 0.09 | 2013 | 0.94 |
| C1 content per-seed kNN | 0.033 | 0.050 | 0.091 | 0.124 | 1.52 | 0.15 | 2011 | 0.92 |
| M1 MovieLens EASE (movies only) | 0.049 | – | 0.103 | – | 2.32 | 0.06 | 2006 | 1.00 |
| **Blend + intent (shipped)** | **0.074** | **0.043** | **0.179** | **0.126** | 1.84 | 0.12 | 2011 | 0.61 |
| Blend + intent + family filter | 0.053 | 0.045 | 0.143 | 0.123 | 1.31 | 0.11 | 2012 | 0.61 |

Shipped configuration: graph (α 0.3, hub damping 1.0, similar 0.4, (1+votes)^1.0) weight 1;
content (m 10, p 2, (1+votes)^1.0) weight 2; EASE (λ 500, engagement-weighted seeds, no
debias) weight 1; per-media percentiles, weights renormalised over available components;
Seerr requests as seeds; continuation bonus 0.1 gated at rating ≥ 6.0, max one per window
of ten; media-mix interleave to the user's recent movie share; decade calibration off.

| comparison | NDCG@50 movie Δ | NDCG@50 show Δ | NDCG@50 all Δ |
|---|---|---|---|
| shipped vs B1 (today), all folds | +0.061 [+0.044, +0.077], 8W/0L | +0.028 [−0.005, +0.051], 6W/3L | +0.035 [+0.018, +0.047], 8W/1L |
| shipped vs B1, **test** | +0.088 [+0.034, +0.153], 6W/0L | +0.020 [−0.017, +0.070], 2W/1L | +0.056 [+0.017, +0.105], 6W/0L |
| shipped vs B2 (popularity), all folds | +0.038 [+0.009, +0.064], 7W/1L | +0.023 [+0.006, +0.039], 6W/2L | +0.017 [−0.003, +0.035], 6W/2L |
| shipped vs B2, **test** | +0.042 [−0.006, +0.118], 4W/2L | +0.009 [−0.035, +0.066], 2W/3L | +0.020 [−0.017, +0.075], 3W/3L |

Secondary protocol (in-library rediscovery holdout, 9 users): shipped NDCG@50 movie 0.179 /
show 0.173 vs popularity 0.043 / 0.065 and B0 0.030 / 0.008 — four times popularity.

Findings and decisions:
- **Content per-seed kNN is the TV scorer**: shows NDCG 0.050 vs 0.031 graph / 0.019
  popularity; clears the rule vs popularity on shows even on the test folds
  (+0.031 [+0.004, +0.060], 4W/0L). Weak negatives and cross-media seeding: no effect yet.
- **EASE is the movie scorer**: 0.049 vs 0.037 graph. Popularity debiasing (β > 0) only
  hurt; positives are popular. Top-200 truncation cost a quarter of NDCG → ship whole
  matrix (8,620² float32 ≈ 300 MB) or top-1000. Lists skew old (median 2006) on their own.
- **Rank fusion doubles the best single component** on movies (0.074) and matches the best
  on shows; the components are complementary (each wins a different medium).
- **Intent seeds** (own Seerr requests): recall@100 movies 0.164 → 0.179. Adopted.
- **Continuations**: hard head slots put 24 sequels (Caddyshack II, Home Alone 3) at the top
  of the owner's deck. As a gated bonus they are accuracy-neutral and sane. Product feature.
- **Family filter costs accuracy** (0.074 → 0.053 movies) because 23% of the owner's
  arrivals are kids' titles (115/115 kids' seeds on the living-room TV). Not a ranking fix:
  the API serves kids' titles as a separate lane (`family=exclude|include|only`).
- **TV popularity prior** at 0.5 instead of 1.0 drops shows NDCG 0.050 → 0.039: the
  household really does watch the big shows. Stranger Things heads most decks until someone
  requests it.
- **Decade calibration** (γ 0.05) costs shows NDCG with no gain; off by default, exposed as a
  user knob later. The shipped deck's median year (2011–2013) reflects what is missing *and
  unrequested*: the household requests fresh titles itself, so the residual skews older.
- Adoption verdict: clears the rule against today's engine on movies and overall (test
  folds, CI excludes 0, 6W/0L); against popularity it is positive everywhere but the test-fold
  CI includes 0 for movies and shows (24 units); over all 94 units it clears on both media.
  Popularity fails the personalisation gate outright (Jaccard 1.00), so the blend ships.

Not run: LLM taste cards / reranker (no Gemini or OpenRouter key present); Trakt `related`
edges (no client id); Plex watchlist (no Plex token); pooled swipe reranker (events table
live since 2026-09-17, no data yet).

## 2026-09-17 — LLM components (Gemini via OpenRouter; total spend $1.93)

**E1 item embeddings** (`gemini-embedding-2`, 768 dims, text = title, year, type, genres,
keyword names, certification, overview; 29,869 items, ~$1). As a drop-in content space
with the TF-IDF settings it was bad (shows 0.021, lift 1.04): dense cosines bunch near 1
and need a far sharper kernel. With sim^64 and m=10 on the tuning folds: movies 0.043,
shows 0.048, show recall@100 0.145 (TF-IDF 0.090).

As a **fourth blend component** (weight 3 next to graph 1 / TF-IDF 1 / EASE 1), paired vs
the shipped blend:

| | NDCG@50 movie Δ | NDCG@50 show Δ | recall@100 show Δ |
|---|---|---|---|
| all folds | −0.004 [−0.013, +0.007] | **+0.011 [+0.004, +0.018], 8W/0L** | +0.025 [−0.005, +0.076] |
| test folds | −0.006 [−0.030, +0.020] | **+0.029 [+0.009, +0.049], 5W/0L** | +0.019 [+0.000, +0.048] |

Decision: **adopted for shows only** (per-media weights: movies 1/2/1/0, shows 1/1/1/3).
Shows NDCG 0.043 → 0.054, show recall@100 0.126 → 0.152, lift 1.85 → 2.02. Artefact
`embeddings_gemini-embedding-2_768.npz` (92 MB) ships with the data; the nightly build
embeds only new titles and skips them if no OPENROUTER_API_KEY is set. Gemini's own free
tier (100 texts/min) is too slow for the catalogue; OpenRouter serves the same model.

**E2 user-side reranker** (`gemini-3.8-flash` reranks the blend's top 60 from the user's
60 most-engaged titles and abandonments; 93 calls, ~$0.90): movies +0.006 [−0.001, +0.012]
all folds, +0.004 test; shows ±0. **Not adopted**: no reliable gain, and it would send
every user's history to a third party nightly for nothing measurable. Revisit as a
`why`-writer or with swipe labels, not as a ranker.

**Dataset search** (see `docs/research/02`, last section): no open per-user TV dataset
that joins to TMDb exists (MTS KION 2021 is the nearest; ContentWise is anonymised);
MovieLens 32M (10/2023) remains the freshest open movie set. TV stays on graph + content.
