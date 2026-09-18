# Design direction (as discussed 2026-09-17; revised the same day after an independent review)

> **Revision note.** An independent design review (2026-09-17) kept the diagnosis and the
> hybrid framing but replaced several components. The **"Roadmap after review"** section at
> the end is authoritative; the "Proposed pipeline" below is kept for the reasoning and is
> marked where superseded. Corrections to facts: the tag genome is **not** in ML-32M (it is
> in ML-25M / Tag Genome 2021, ≤2019, movies only); TMDb `/recommendations` is built from
> TMDb users' favourites/ratings, not co-watching; EASE's Gram matrix at 15k items is
> ~0.9 GB float32 and inversion in float64 peaks near 4 GB (fine on the workstation), and
> shipping only top-k *positive* neighbours drops the negative weights that distinguish EASE
> from item-kNN — ship top-200 by |weight| or a float16 memmap and measure the loss.

## Framing

Two surfaces, one engine:

1. **Missing titles per user** → consumed by the picks PWA. **First target.**
2. **In-library titles per user** → today Shortlist's Plex rows; replace later only if
   they also disappoint.

Decision so far: **keep Shortlist for Plex-side delivery**, replace only the missing-title
side. Shortlist's privacy/share-filter machinery is its hard, valuable part; its rows get the
full ranking; the gap is specifically the unranked inbox.

## Why "better" is hybrid, not home-grown collaborative filtering

9 viewers × ~1,900 library items cannot train a CF model. The collaborative signal has to
come from (a) TMDb's `/recommendations` (co-watch based, live, covers TV and new releases)
and (b) an external interaction dataset with our users folded in (below). What we can own is
the per-user side: profile, ranking, feedback.

## Proposed pipeline (original; items marked ⟂ are superseded by the roadmap below)

1. **History from Tautulli**, per user including managed users, with `player`/`platform`.
   Positives = completed plays (or ≥85%). Weight = recency decay (half-life ~60–90 d) ×
   dampened frequency (`log`) × Plex rating where present. Negatives = picks `never`
   dismissals; `later` = soft negative.
2. **Context split**: cluster plays by device; keep a "family TV" context out of the owner's
   adult profile (the Bluey/Sesame Street problem) without asking anyone to change habits.
3. ⟂ **Multi-cluster taste profile** (3–5 clusters per user) over TMDb genres, keywords, cast,
   language, decade, plus an embedding of the overview (Gemini embeddings; ~1,900 library
   items + candidates, cheap and one-off). One averaged vector turns sitcoms + prestige drama
   + animated family into mush; clusters let the deck draw from each.
4. **Candidates**: TMDb recs + similar per recent seed (as now); discover by the profile's
   own keywords with a year window; for in-library, score the whole unwatched library.
5. ⟂ **Score per user**: similarity to nearest cluster × vote-shrunk quality prior × a
   **release-year preference learned from that user's own watch-year distribution** (not a
   global slider) × TMDb affinity where seeded × external-CF score where available (below).
   Penalties: dismissed, disliked genres, family cluster on the adult surface. Then MMR
   diversification across clusters.
6. **Media quota from the user's own distinct-title ratio** (Pavel ≈ 3 movies : 2 shows),
   not from whatever TMDb returns.
7. ⟂ **Feedback loop**: picks swipes are labels; once a user has a few dozen, fit a tiny
   per-user logistic reranker over the same features.
8. **Explainability**: "because you watched X and two more like it", plus the cluster name.

## Injecting our users into an external CF dataset (Pavel's clarification)

The idea: train item-item CF on MovieLens (or similar) and fold our users in.

**What it buys**
- MovieLens 32M: ~200k users, ~87k movies, `links.csv` maps to `tmdbId` → exact join to
  Tautulli. (The tag genome — 1,128 relevance scores × ~13k movies — ships with ML-25M, not
  32M, ends in 2019 and has no TV; at most an offline probe of embedding quality.)
- With **EASE** (closed-form item-item) a new user needs no retraining: score = user's
  sparse history vector × B. **Implicit ALS** works with a fold-in step. Either gives one
  per-user vector blending the whole history (no tool reviewed has this) and a consistent
  score for any candidate at zero API calls.
- Enables a proper **offline evaluation**: hold out each viewer's last N watches, measure
  recall@k vs the TMDb-seed approach. That number decides the blend.

**Hard limit**: MovieLens is movies only; no comparable open per-user TV dataset is known
(Netflix Prize is 2006, movies). TV is 22,385 of Pavel's 23,679 plays and 155 of his 197
pending suggestions. TV stays on the content + TMDb path regardless.

**The trap**: MovieLens raters skew to the film canon and the data ends in 2023.
- The model will push 1990s–2000s classics unless the popularity prior is divided out and
  the user's own year preference multiplied back in — otherwise it rebuilds the current
  complaint.
- Items after the cutoff or with <~50 ratings have no vector; ~a fifth of the pending inbox
  is 2020s. TMDb recs cover those; keep them in.

**Practicalities**: restrict to items with ≥50 ratings (~15k); a float32 EASE Gram matrix at
that size is well under 1 GB to compute on the workstation; ship only top-50 neighbours per
item (a few MB) to the NAS; scoring a user is milliseconds in NumPy. Treat plays as implicit
positives (weight by completion and recency); MovieLens ratings ≥3.5 as positives so both
populations mean the same thing.

**Placement in the blend**

| Component | Covers | Source |
|---|---|---|
| EASE/ALS on MovieLens, fold-in per user | movies with enough ratings | offline, refresh yearly |
| TMDb recommendations + similar per seed | movies and TV incl. new releases | live API |
| Content profile with clusters + embeddings | everything incl. cold items | TMDb metadata + tag genome |
| Feedback reranker from picks swipes | per-user weights over the above | our own data |

## Non-negotiables carried over

Nothing auto-downloads. Never request as Pavel. Identity = Plex account id from the
authentik JWT claim. Read-only against live services unless told otherwise. Secrets stay in
homelab-stacks' sops files. Deploys go through homelab-stacks as a Compose service on
x86_64 TrueNAS (pull-only image or repo-config single-file mount).

## Roadmap after review (authoritative, 2026-09-17)

Decisions: standalone service (no Shortlist fork for now; a thin fork adding one "external
ranked list" candidate source is the only fork worth revisiting, and only to drive Plex rows);
all 9 active users evaluated; scorer order graph → content → MovieLens; LLM allowed item-side
and user-side for all users (Gemini, OpenRouter as fallback); picks gains impression logging
and a "skip" swipe now.

**Signal layer first** (`harness/engagement.py`): movie positive = completed; show positive =
≥3 distinct completed episodes, engagement `min(1, eps/8)`; weak negatives = movie abandoned
<30% or show dropped after ≤2 eps, both stale >60 days; rewatches capped (log); recency at two
timescales (~2-year half-life + 60-day boost); first-engagement date is the timestamp used
everywhere.

**Evaluation** (`harness/split.py`, `harness/eval.py`): primary = arrival-time folds
(quarterly cutoffs, candidates = local TMDb catalogue minus library-as-of-T, positives =
titles added after T and engaged within 180 days + Seerr requests in the window); secondary =
temporal holdout by first engagement. Primary metric macro NDCG@50 per media type; paired Δ vs
B1 with user-clustered bootstrap CI; adoption rule: CI excludes 0, or ≥7/9 users improve and
none regresses materially; ties go to the simpler method. Gates: personalisation lift,
inter-user Jaccard@50, decade/media calibration. Baselines B0 inbox-sim, B1 row-ranking, B2
global popularity, B3 household popularity, B4 plain content-kNN.

**Scorers, in build order (each lands only if it clears the adoption rule):**

1. **Graph**: personalised PageRank over the crawled `/recommendations` (+ `/similar` at lower
   weight, optional Trakt `related`) graph, separate movie/TV graphs, restart 0.2–0.3, divided
   by `globalPR^0.5` to strip hubs. The missing collaborative signal for TV and new titles.
2. **Content**: per-seed kNN aggregation (top-m seeds, negatives subtract) over structured
   TF-IDF (keywords, genres, cast, creator, network, certification, runtime, language); LLM
   item "taste cards" as an embedding variant. No clusters in scoring; clusters only to label.
3. **MovieLens EASE** (movies): ratings ≥2015, binarised ≥3.5, popularity-debiased, per-user
   fold-in; MovieLens held-out users as the dev set for its hyperparameters.
4. **Blend**: per-user percentiles per component, weighted mean renormalised over the
   components available for that item; quality as a gate not a multiplier; list-level
   calibration for freshness / decade / media mix (target = recent new-title starts, shrunk to
   household) with explicit per-user overrides; deterministic slots for missing seasons and
   next franchise entries; obtainable-release gate; a new-release source.
5. **Family handling**: per-play classifier (kids certification/genre × shared device × hour)
   routing to a household pseudo-profile — the device alone is wrong (84 adult plays on the
   BRAVIA).
6. **Intent + cold start**: Seerr requests and Plex watchlist as strong positives; household
   prior for the 7 thin users.
7. **LLM user-side reranker** as one more fused component under test.
8. **Output API** in picks' item shape; picks second source behind a flag. Then a **pooled**
   feedback model with shrunk per-user slopes on the picks `events` table, and team-draft
   interleaving across ranker arms.
