"""Baseline scorers. A scorer is a callable (ctx, items) -> {candidate key: score}; higher is
better; unscored candidates rank last. B0 and B1 reproduce Shortlist 1.8.0 (see
docs/research/01) so the numbers say how today's inbox and its row ranking would fare."""

import math
from collections import Counter, defaultdict

from .data import Item, Key, UserContext

MAX_SEEDS = 30
SEED_HALF_LIFE_DAYS = 45
RELEASE_HALF_LIFE_YEARS = 16          # recency 0.5 on an 8-year base
SOURCE_WEIGHT = {"recommendations": 1.0, "similar": 0.6}
INBOX_MIN_RATING, INBOX_MIN_YEAR = 7.0, 1969
# The live gate is 5,000 IMDb votes; IMDb counts run roughly 10× TMDb's for movies and
# 25-30× for shows, so these are the TMDb-vote proxies.
INBOX_MIN_VOTES = {"movie": 500, "show": 180}


def shortlist_seeds(ctx: UserContext) -> list[tuple[Key, float]]:
    """Last 30 distinct titles by last watch, weight 0.5 ** (days / 45), each media type
    present guaranteed a third of the budget."""
    by_recency = sorted(ctx.seeds, key=lambda s: min(s.last_at, ctx.cutoff), reverse=True)
    per_type = defaultdict(list)
    for s in by_recency:
        per_type[s.key[1]].append(s)
    chosen = []
    if len(per_type) > 1:
        for group in per_type.values():
            chosen.extend(group[: MAX_SEEDS // 3])
    rest = [s for s in by_recency if s not in chosen]
    chosen.extend(rest[: MAX_SEEDS - len(chosen)])
    out = []
    for s in chosen:
        days = (ctx.cutoff - min(s.last_at, ctx.cutoff)) / 86400
        out.append((s.key, 0.5 ** (days / SEED_HALF_LIFE_DAYS)))
    return out


def genre_coherence(candidate: Item, seed: Item) -> float:
    if not candidate.genres:
        return 1.0
    extra = len(set(candidate.genres) - set(seed.genres))
    return max(0.5, 1 - extra / len(candidate.genres))


def shortlist_pool(ctx: UserContext, items: dict[Key, Item]) -> dict[Key, dict]:
    """Every TMDb recommendation/similar return for every seed, with Shortlist's affinity."""
    pool: dict[Key, dict] = {}
    for seed_key, seed_weight in shortlist_seeds(ctx):
        seed = items.get(seed_key)
        if seed is None:
            continue
        for source, ids in (("recommendations", seed.recommendations), ("similar", seed.similar)):
            n = len(ids)
            for pos, tmdb_id in enumerate(ids):
                key = (tmdb_id, seed_key[1])
                cand = items.get(key)
                if cand is None or key not in ctx.candidates:
                    continue
                position_decay = 1 - 0.5 * pos / max(1, n - 1)
                affinity = SOURCE_WEIGHT[source] * position_decay * genre_coherence(cand, seed)
                entry = pool.setdefault(key, {"seeds": set(), "max_seed_weight": 0.0, "affinity": 0.0, "best_seed": seed_key})
                entry["seeds"].add(seed_key)
                if affinity * seed_weight > entry["affinity"] * entry["max_seed_weight"]:
                    entry["best_seed"] = seed_key
                entry["max_seed_weight"] = max(entry["max_seed_weight"], seed_weight)
                entry["affinity"] = max(entry["affinity"], affinity)
    return pool


def row_score(cand: Item, entry: dict, cutoff_year: float) -> float:
    rating = cand.vote_average if cand.vote_count else 5.0
    age = max(0.0, cutoff_year - cand.year) if cand.year else 0.0
    return (
        (1 + len(entry["seeds"])) * rating * (1 + entry["max_seed_weight"])
        * entry["affinity"] * 0.5 ** (age / RELEASE_HALF_LIFE_YEARS)
    )


def diversify_by_seed(scored: list[tuple[Key, float, Key]]) -> dict[Key, float]:
    """Round-robin one title per seed per pass, seeds ordered by their best candidate."""
    by_seed: dict[Key, list] = defaultdict(list)
    for key, score, seed in sorted(scored, key=lambda t: t[1], reverse=True):
        by_seed[seed].append(key)
    order, queues = [], list(by_seed.values())
    while queues:
        queues = [q for q in queues if q]
        for q in queues:
            order.append(q.pop(0))
    return {key: len(order) - i for i, key in enumerate(order)}


def _cutoff_year(ctx: UserContext) -> float:
    return 1970 + ctx.cutoff / (365.25 * 86400)


class ShortlistInbox:
    """B0: the demand-sorted inbox. Demand counts how many of this fold's users have the
    title in their pool, so it needs every user's pool before it can score one."""

    name = "B0_shortlist_inbox"

    def __init__(self):
        self.demand: Counter | None = None
        self.pools: dict[int, dict] = {}

    def prepare(self, contexts: dict[int, UserContext], items: dict[Key, Item]) -> None:
        self.pools = {u: shortlist_pool(ctx, items) for u, ctx in contexts.items()}
        self.demand = Counter(key for pool in self.pools.values() for key in pool)

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        pool = self.pools[ctx.user_id]
        qualifying = [
            key for key in pool
            if items[key].vote_average >= INBOX_MIN_RATING
            and items[key].vote_count >= INBOX_MIN_VOTES[key[1]]
            and (items[key].year or 0) >= INBOX_MIN_YEAR
        ]
        order = sorted(
            qualifying,
            key=lambda k: (self.demand[k], items[k].vote_average, items[k].vote_count),
            reverse=True,
        )
        return {key: len(order) - i for i, key in enumerate(order)}


class ShortlistRow:
    """B1: Shortlist's row ranking applied to the missing titles, diversified by seed."""

    name = "B1_shortlist_row"

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        pool = shortlist_pool(ctx, items)
        year = _cutoff_year(ctx)
        scored = [(key, row_score(items[key], e, year), e["best_seed"]) for key, e in pool.items()]
        return diversify_by_seed(scored)


class GlobalPopularity:
    """B2: the same list for everyone, by TMDb vote count (popularity is a live figure)."""

    name = "B2_global_popularity"

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        return {key: float(items[key].vote_count) for key in ctx.candidates}


class HouseholdPopularity:
    """B3: what the other users asked for or watched before the cutoff, then vote count."""

    name = "B3_household_popularity"

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        wanted = Counter()
        for keys in ctx.household_requests.values():
            wanted.update(k for k in keys if k in ctx.candidates)
        for seeds in ctx.household_seeds.values():
            wanted.update(s.key for s in seeds if s.key in ctx.candidates)
        return {key: wanted[key] * 1e9 + items[key].vote_count for key in ctx.candidates}


class ContentKNN:
    """B4: cosine between a TF-IDF genre+keyword profile (weighted seed sum) and each
    candidate. The plain single-vector content approach."""

    name = "B4_content_knn"

    def __init__(self):
        self.idf: dict[str, float] = {}
        self.vectors: dict[Key, dict[str, float]] = {}

    def prepare(self, contexts: dict[int, UserContext], items: dict[Key, Item]) -> None:
        if self.vectors:
            return
        df = Counter()
        for it in items.values():
            df.update(self._terms(it))
        n = len(items)
        self.idf = {t: math.log(n / c) for t, c in df.items()}
        for key, it in items.items():
            vec = {t: self.idf[t] for t in self._terms(it)}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self.vectors[key] = {t: v / norm for t, v in vec.items()}

    @staticmethod
    def _terms(it: Item) -> set[str]:
        return {f"g{g}" for g in it.genres} | {f"k{k}" for k in it.keywords}

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        profile: dict[str, float] = defaultdict(float)
        for s in ctx.seeds:
            for t, v in self.vectors.get(s.key, {}).items():
                profile[t] += s.weight * v
        norm = math.sqrt(sum(v * v for v in profile.values())) or 1.0
        out = {}
        for key in ctx.candidates:
            vec = self.vectors.get(key)
            if vec:
                out[key] = sum(profile.get(t, 0.0) * v for t, v in vec.items()) / norm
        return out


BASELINES = [ShortlistInbox, ShortlistRow, GlobalPopularity, HouseholdPopularity, ContentKNN]
