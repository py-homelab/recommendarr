"""The blend: per-user, per-media-type percentiles of each component, averaged with weights
renormalised over the components that scored the item (so TV and post-2023 titles, which
EASE cannot score, are not penalised). Then list-level treatment: continuation slots (next
unwatched entry of a franchise the user follows), media-mix interleaving toward the user's
own recent starts, and optional decade calibration."""

import math
from collections import Counter

import numpy as np

from .data import Item, Key, Seed, UserContext
from .eval import js_divergence, movie_share_target

WINDOW = 10


def percentiles(scores: dict[Key, float], keys: list[Key]) -> dict[Key, float]:
    vals = np.array([scores[k] for k in keys])
    order = vals.argsort().argsort()
    n = max(1, len(keys) - 1)
    return {k: order[i] / n for i, k in enumerate(keys)}


class Blend:
    def __init__(self, components, weights=None, continuations=True, media_mix=True,
                 decade_gamma=0.0, continuation_bonus=0.1, continuation_min_rating=6.0, name=None):
        self.components = components
        self.weights = weights or [1.0] * len(components)
        self.continuations, self.media_mix, self.decade_gamma = continuations, media_mix, decade_gamma
        self.continuation_bonus, self.continuation_min_rating = continuation_bonus, continuation_min_rating
        tag = ",".join(f"{w:g}" for w in self.weights)
        self.name = name or f"BL_blend(w={tag},cont={int(continuations)},mix={int(media_mix)},dec={decade_gamma:g},cb={continuation_bonus:g})"
        self.last_continuations: dict[Key, Key] = {}

    def prepare(self, contexts, items):
        for c in self.components:
            if hasattr(c, "prepare"):
                c.prepare(contexts, items)

    def fused(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        total: dict[Key, float] = {}
        weight_sum: dict[Key, float] = {}
        for comp, w in zip(self.components, self.weights):
            if not w:
                continue
            scores = comp(ctx, items)
            for mt in ("movie", "show"):
                keys = [k for k in scores if k[1] == mt]
                if not keys:
                    continue
                for k, pct in percentiles(scores, keys).items():
                    total[k] = total.get(k, 0.0) + w * pct
                    weight_sum[k] = weight_sum.get(k, 0.0) + w
        return {k: total[k] / weight_sum[k] for k in total}

    def continuation_keys(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, Key]:
        """Next unwatched entry of each franchise the user follows -> the seed it follows.
        Only entries rated at least continuation_min_rating qualify; bad sequels are not
        continuations anyone wants."""
        latest: dict[int, Seed] = {}
        for s in ctx.seeds:
            it = items.get(s.key)
            if it and it.collection_id and it.release_date:
                prev = latest.get(it.collection_id)
                if prev is None or it.release_date > items[prev.key].release_date:
                    latest[it.collection_id] = s
        nexts: dict[int, Key] = {}
        for k in ctx.candidates:
            it = items[k]
            seed = latest.get(it.collection_id)
            if seed and it.release_date and it.release_date > items[seed.key].release_date \
                    and it.vote_average >= self.continuation_min_rating:
                if it.collection_id not in nexts or it.release_date < items[nexts[it.collection_id]].release_date:
                    nexts[it.collection_id] = k
        return {k: latest[items[k].collection_id].key for k in nexts.values()}

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        fused = self.fused(ctx, items)
        self.last_continuations = self.continuation_keys(ctx, items) if self.continuations else {}
        for k in self.last_continuations:
            if k in fused:
                fused[k] += self.continuation_bonus
        ranked = sorted(fused, key=lambda k: -fused[k])
        if self.media_mix or self.decade_gamma:
            ranked = self.rerank(ranked, fused, ctx, items)
        return {k: float(len(ranked) - i) for i, k in enumerate(ranked)}

    def rerank(self, ranked, fused, ctx, items, depth=300):
        """Greedy: within every window of 10 keep the movie share near the user's target;
        optionally penalise drift from the user's decade distribution."""
        target_movie = movie_share_target(ctx)
        decade_target = Counter((items[s.key].year // 10) * 10 for s in ctx.seeds if s.key in items and items[s.key].year)
        pool = ranked[:depth]
        rest = ranked[depth:]
        out, chosen_decades = [], Counter()
        movies = conts = 0
        while pool and len(out) < depth:
            window_n = len(out) % WINDOW
            movies_in_window = movies
            best, best_val = None, -math.inf
            for k in pool[:60]:
                val = fused[k]
                if self.media_mix:
                    want_movie = (movies_in_window / max(1, window_n)) < target_movie if window_n else True
                    if (k[1] == "movie") != want_movie:
                        val -= 0.15
                if conts >= 1 and k in self.last_continuations:
                    val -= 0.15   # at most one franchise continuation per window of ten
                if self.decade_gamma and items[k].year:
                    trial = chosen_decades.copy(); trial[(items[k].year // 10) * 10] += 1
                    val -= self.decade_gamma * js_divergence(trial, decade_target)
                if val > best_val:
                    best, best_val = k, val
            pool.remove(best)
            out.append(best)
            if items[best].year:
                chosen_decades[(items[best].year // 10) * 10] += 1
            in_window = bool(len(out) % WINDOW)
            movies = (movies + (best[1] == "movie")) if in_window else 0
            conts = (conts + (best in self.last_continuations)) if in_window else 0
        return out + pool + rest
