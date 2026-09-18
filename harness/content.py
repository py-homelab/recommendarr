"""C1: per-seed nearest-neighbour aggregation over structured TF-IDF item vectors.

score(c) = sum over the user's top-m most similar seeds s of w_s * sim(c, s)^p, minus the
same over weak negatives. No profile vector: a user with sitcoms, prestige drama and anime is
three neighbourhoods, not their average. Features are keywords, genres, top cast, creators or
directors, language, certification, runtime bucket, decade and collection, one shared
vocabulary across movies and shows so either can inform the other."""

import math
from collections import Counter

import numpy as np
import scipy.sparse as sp

from .data import Item, Key, UserContext

GROUP_WEIGHTS = {"k": 1.0, "g": 1.0, "c": 0.5, "d": 0.5, "l": 0.3, "r": 0.3, "t": 0.3, "y": 0.3, "o": 1.0}


def item_terms(it: Item) -> list[str]:
    terms = [f"k{k}" for k in it.keywords] + [f"g{g}" for g in it.genres]
    terms += [f"c{c}" for c in it.cast] + [f"d{d}" for d in it.crew]
    if it.language:
        terms.append(f"l{it.language}")
    if it.certification:
        terms.append(f"r{it.certification}")
    if it.runtime:
        terms.append(f"t{min(it.runtime // 30, 6)}")
    if it.year:
        terms.append(f"y{it.year // 10}")
    if it.collection_id:
        terms.append(f"o{it.collection_id}")
    return terms


class ItemSpace:
    """Row-normalised TF-IDF matrix over every item, built once and shared."""

    def __init__(self, items: dict[Key, Item], group_weights=GROUP_WEIGHTS):
        self.keys = list(items)
        self.index = {k: i for i, k in enumerate(self.keys)}
        df = Counter()
        per_item = []
        for k in self.keys:
            terms = set(item_terms(items[k]))
            per_item.append(terms)
            df.update(terms)
        vocab = {t: i for i, t in enumerate(df)}
        n = len(self.keys)
        idf = {t: math.log(n / c) for t, c in df.items()}
        rows, cols, vals = [], [], []
        for i, terms in enumerate(per_item):
            for t in terms:
                rows.append(i); cols.append(vocab[t]); vals.append(idf[t] * group_weights[t[0]])
        X = sp.csr_matrix((vals, (rows, cols)), shape=(n, len(vocab)), dtype=np.float32)
        norms = np.sqrt(np.asarray(X.multiply(X).sum(axis=1)).ravel())
        self.X = sp.diags(1.0 / np.maximum(norms, 1e-9)) @ X
        self.votes = np.array([items[k].vote_count for k in self.keys], dtype=np.float64)


class ContentSeedKNN:
    # Defaults chosen on folds 0-7 (2026-09-17): m=10, p=2 balances NDCG and recall@100;
    # negatives and cross-media seeding made no measurable difference at this stage.
    def __init__(self, m=10, p=2.0, pop_power=1.0, negative_weight=0.5, cross_media=True, show_pop_power=None, space=None, name=None):
        self.m, self.p, self.pop_power = m, p, pop_power
        self.show_pop_power = pop_power if show_pop_power is None else show_pop_power
        self.negative_weight, self.cross_media = negative_weight, cross_media
        self.name = name or f"C1_content_knn(m={m},p={p},pop={pop_power}/{self.show_pop_power},neg={negative_weight},x={int(cross_media)})"
        self.space = space

    def prepare(self, contexts, items: dict[Key, Item]) -> None:
        if self.space is None:
            self.space = ItemSpace(items)

    def _aggregate(self, cand_idx, seed_idx, weights) -> np.ndarray:
        sims = self.space.X[cand_idx] @ self.space.X[seed_idx].T
        sims = sims.toarray() if sp.issparse(sims) else np.asarray(sims)
        sims = np.maximum(sims, 0) ** self.p * weights[None, :]
        m = min(self.m, sims.shape[1])
        top = np.partition(sims, -m, axis=1)[:, -m:] if sims.shape[1] > m else sims
        return top.sum(axis=1)

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        cands = [k for k in ctx.candidates if k in self.space.index]
        cand_idx = np.array([self.space.index[k] for k in cands])
        out = {}
        for mt in ("movie", "show"):
            sel = np.array([i for i, k in enumerate(cands) if k[1] == mt])
            if len(sel) == 0:
                continue
            seeds = [s for s in ctx.seeds if s.key in self.space.index and (self.cross_media or s.key[1] == mt)]
            if not seeds:
                continue
            score = self._aggregate(
                cand_idx[sel], np.array([self.space.index[s.key] for s in seeds]),
                np.array([s.weight for s in seeds]),
            )
            negs = [k for k in ctx.negatives if k in self.space.index and (self.cross_media or k[1] == mt)]
            if negs and self.negative_weight:
                score -= self.negative_weight * self._aggregate(
                    cand_idx[sel], np.array([self.space.index[k] for k in negs]), np.ones(len(negs)))
            pop = self.pop_power if mt == "movie" else self.show_pop_power
            if pop:
                score = np.maximum(score, 0) * np.power(1.0 + self.space.votes[cand_idx[sel]], pop)
            for j, i in enumerate(sel):
                out[cands[i]] = float(score[j])
        return out
