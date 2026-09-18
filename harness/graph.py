"""G1: personalised PageRank over TMDb's recommendation graph.

Nodes are catalogue titles of one media type; an edge i -> j exists when j is in i's
/recommendations list (weight 1, decaying to 0.5 down the list) or /similar list (lower
weight). The walk restarts at the user's seeds in proportion to their engagement weight, so
a title reachable from many seeds accumulates mass — a whole-history blend rather than
one-hop lists. Dividing by global PageRank^beta strips hub titles that every walk lands on."""

import numpy as np
import scipy.sparse as sp

from .data import Item, Key, UserContext

MEDIA_TYPES = ("movie", "show")


class Graph:
    def __init__(self, items: dict[Key, Item], media_type: str, similar_weight: float):
        keys = [k for k in items if k[1] == media_type]
        self.index = {k: i for i, k in enumerate(keys)}
        self.keys = keys
        n = len(keys)
        rows, cols, vals = [], [], []
        for k in keys:
            it = items[k]
            for weight, ids in ((1.0, it.recommendations), (similar_weight, it.similar)):
                if not weight:
                    continue
                m = len(ids)
                for pos, tmdb_id in enumerate(ids):
                    j = self.index.get((tmdb_id, media_type))
                    if j is not None:
                        rows.append(self.index[k]); cols.append(j)
                        vals.append(weight * (1 - 0.5 * pos / max(1, m - 1)))
        adj = sp.csr_matrix((vals, (rows, cols)), shape=(n, n), dtype=np.float32)
        out = np.asarray(adj.sum(axis=1)).ravel()
        self.dangling = out == 0
        inv = np.where(self.dangling, 0.0, 1.0 / np.maximum(out, 1e-12))
        # transition^T so that p_next = T @ p spreads mass along out-edges
        self.T = (sp.diags(inv) @ adj).T.tocsr()
        self.global_pr = self.walk(np.full(n, 1.0 / n), alpha=0.15, iters=50)
        self.votes = np.array([items[k].vote_count for k in keys], dtype=np.float64)

    def walk(self, restart: np.ndarray, alpha: float, iters: int) -> np.ndarray:
        p = restart.copy()
        for _ in range(iters):
            lost = float(p[self.dangling].sum())
            p = alpha * restart + (1 - alpha) * (self.T @ p + lost * restart)
        return p


class GraphPPR:
    # Defaults chosen on folds 0-7 (harness tune, 2026-09-17): the popularity prior is
    # essential (positives sit at the 91st vote-count percentile); exponents above ~1.5 only
    # collapse the list toward global popularity.
    def __init__(self, alpha=0.3, beta=1.0, similar_weight=0.4, iters=30, pop_power=1.0,
                 max_seeds=None, show_pop_power=None, name=None):
        self.alpha, self.beta, self.similar_weight, self.iters = alpha, beta, similar_weight, iters
        self.pop_power, self.max_seeds = pop_power, max_seeds
        self.show_pop_power = pop_power if show_pop_power is None else show_pop_power
        self.name = name or f"G1_graph_ppr(a={alpha},b={beta},s={similar_weight},pop={pop_power}/{self.show_pop_power},seeds={max_seeds})"
        self.graphs: dict[str, Graph] = {}

    def prepare(self, contexts, items: dict[Key, Item]) -> None:
        if not self.graphs:
            self.graphs = {mt: Graph(items, mt, self.similar_weight) for mt in MEDIA_TYPES}

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        out = {}
        seeds = sorted(ctx.seeds, key=lambda s: -s.weight)
        if self.max_seeds:
            seeds = seeds[: self.max_seeds]
        for mt, g in self.graphs.items():
            restart = np.zeros(len(g.keys))
            for s in seeds:
                i = g.index.get(s.key)
                if i is not None:
                    restart[i] += s.weight
            if restart.sum() == 0:
                continue
            restart /= restart.sum()
            p = g.walk(restart, self.alpha, self.iters)
            score = p / np.power(g.global_pr, self.beta)
            pop = self.pop_power if mt == "movie" else self.show_pop_power
            if pop:
                score = score * np.power(1.0 + g.votes, pop)
            for k in ctx.candidates:
                if k[1] == mt:
                    i = g.index.get(k)
                    if i is not None:
                        out[k] = float(score[i])
        return out
