"""M1: EASE (Steck 2019) trained on MovieLens 32M, our users folded in at scoring time.

Training data: ratings from 2015 on (cuts the film-canon skew of early raters), binarised at
>= 3.5, items with >= MIN_RATINGS such ratings. B = closed-form item-item weights, cached on
disk. A user is their binary vector over the same items; score = x @ B, optionally divided
by item popularity^beta. Movies only; shows get no score and fall back to other components.
Negative weights are kept: the matrix is shipped whole (float32, ~0.5 GB at 11k items) or
as top-k by |weight| for the NAS, and the harness measures what that truncation costs."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp

from . import config
from .data import Item, Key, UserContext

ML_DIR = config.DATA_DIR / "movielens" / "ml-32m"
CACHE_DIR = config.DATA_DIR / "movielens"
FROM_YEAR = 2015
MIN_RATING = 3.5
MIN_RATINGS = 50


def _load_interactions():
    ratings = pd.read_csv(ML_DIR / "ratings.csv", usecols=["userId", "movieId", "rating", "timestamp"])
    since = pd.Timestamp(f"{FROM_YEAR}-01-01").timestamp()
    ratings = ratings[(ratings.timestamp >= since) & (ratings.rating >= MIN_RATING)]
    counts = ratings.movieId.value_counts()
    ratings = ratings[ratings.movieId.isin(counts[counts >= MIN_RATINGS].index)]
    links = pd.read_csv(ML_DIR / "links.csv").dropna(subset=["tmdbId"])
    tmdb_of = dict(zip(links.movieId, links.tmdbId.astype(int)))
    ratings = ratings[ratings.movieId.isin(tmdb_of)]
    movie_ids = np.sort(ratings.movieId.unique())
    col = {m: i for i, m in enumerate(movie_ids)}
    user_ids, rows = np.unique(ratings.userId.values, return_inverse=True)
    X = sp.csr_matrix(
        (np.ones(len(ratings), dtype=np.float32), (rows, ratings.movieId.map(col).values)),
        shape=(len(user_ids), len(movie_ids)),
    )
    return X, [tmdb_of[m] for m in movie_ids]


def train(lam: float) -> tuple[np.ndarray, list[int], np.ndarray]:
    cache = CACHE_DIR / f"ease_lam{int(lam)}.npz"
    if cache.exists():
        z = np.load(cache)
        return z["B"], z["tmdb_ids"].tolist(), z["pop"]
    X, tmdb_ids = _load_interactions()
    print(f"EASE: {X.shape[0]} users × {X.shape[1]} movies, {X.nnz} interactions, λ={lam}")
    G = (X.T @ X).toarray().astype(np.float64)
    pop = np.diag(G).copy()
    G[np.diag_indices_from(G)] += lam
    P = np.linalg.inv(G)
    B = -P / np.diag(P)[None, :]
    B[np.diag_indices_from(B)] = 0.0
    B = B.astype(np.float32)
    np.savez(cache, B=B, tmdb_ids=np.array(tmdb_ids), pop=pop)
    return B, tmdb_ids, pop


class MovieLensEASE:
    # Defaults chosen on folds 0-7 (2026-09-17): popularity debiasing (beta > 0) only hurt;
    # engagement-weighted seeds beat binary; top-200 truncation cost a quarter of NDCG.
    def __init__(self, lam=500.0, beta=0.0, top_k=None, weighted=True, name=None):
        self.lam, self.beta, self.top_k, self.weighted = lam, beta, top_k, weighted
        self.name = name or f"M1_ease(lam={int(lam)},beta={beta},topk={top_k},w={int(weighted)})"
        self.B = None

    def prepare(self, contexts, items: dict[Key, Item]) -> None:
        if self.B is not None:
            return
        B, tmdb_ids, pop = train(self.lam)
        if self.top_k:
            keep = np.argpartition(np.abs(B), -self.top_k, axis=0)[-self.top_k:, :]
            mask = np.zeros_like(B, dtype=bool)
            mask[keep, np.arange(B.shape[1])[None, :]] = True
            B = np.where(mask, B, 0.0)
        self.B, self.pop = B, pop
        self.index = {t: i for i, t in enumerate(tmdb_ids)}
        self.coverage = sum(1 for k in items if k[1] == "movie" and k[0] in self.index)

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        x = np.zeros(self.B.shape[0], dtype=np.float32)
        for s in ctx.seeds:
            if s.key[1] == "movie" and s.key[0] in self.index:
                x[self.index[s.key[0]]] = s.weight if self.weighted else 1.0
        if not x.any():
            return {}
        score = x @ self.B
        if self.beta:
            score = score / np.power(self.pop, self.beta)
        out = {}
        for k in ctx.candidates:
            if k[1] == "movie":
                i = self.index.get(k[0])
                if i is not None:
                    out[k] = float(score[i])
        return out
