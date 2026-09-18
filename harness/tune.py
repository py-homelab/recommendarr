"""Hyperparameter grids on the early folds only (folds 0-7); folds 8-10 are the test set
and are never looked at here."""

import itertools
import math

from . import baselines, blend, content, data, graph, movielens, signals
from .eval import fmt, fold_evaluation, summarise

TUNE_FOLDS = set(range(8))


def graph_grid():
    for beta, pop in itertools.product((0.5, 1.0, 1.5), (1.0, 1.5, 2.0, 3.0)):
        yield graph.GraphPPR(alpha=0.3, beta=beta, similar_weight=0.4, pop_power=pop)


def content_grid():
    for m, p, pop in itertools.product((3, 5, 10), (1.0, 2.0, 3.0), (0.5, 1.0)):
        yield content.ContentSeedKNN(m=m, p=p, pop_power=pop)
    yield content.ContentSeedKNN(m=5, p=2.0, pop_power=1.0, negative_weight=0.0)
    yield content.ContentSeedKNN(m=5, p=2.0, pop_power=1.0, cross_media=False)


def movielens_grid():
    for lam, beta in itertools.product((200.0, 500.0, 2000.0), (0.0, 0.3, 0.6)):
        yield movielens.MovieLensEASE(lam=lam, beta=beta)
    yield movielens.MovieLensEASE(lam=500.0, beta=0.0, weighted=True)
    yield movielens.MovieLensEASE(lam=500.0, beta=0.0, top_k=200)


def blend_grid():
    comps = [graph.GraphPPR(), content.ContentSeedKNN(), movielens.MovieLensEASE()]
    for w in ((1, 1, 1), (1, 2, 1), (2, 1, 1), (1, 1, 2), (0, 1, 1), (1, 0, 1), (1, 1, 0)):
        yield blend.Blend(comps, list(map(float, w)), continuations=False, media_mix=False)
    for cb in (0.1, 0.2):
        yield blend.Blend(comps, [1.0, 2.0, 1.0], continuations=True, media_mix=True, continuation_bonus=cb)
    yield blend.Blend(comps, [1.0, 2.0, 1.0], continuations=True, media_mix=True, decade_gamma=0.05)
    for spp in (0.5,):
        c2 = [graph.GraphPPR(show_pop_power=spp), content.ContentSeedKNN(show_pop_power=spp), movielens.MovieLensEASE()]
        yield blend.Blend(c2, [1.0, 2.0, 1.0], continuations=True, media_mix=True, name=f"BL(show_pop={spp})")


def final_blend(embedding_space=None):
    """The shipped ranker. With an embedding space (gemini-embedding-2 item vectors) a fourth
    component joins for shows only, where it cleared the adoption rule (2026-09-17)."""
    comps = [graph.GraphPPR(), content.ContentSeedKNN(), movielens.MovieLensEASE()]
    weights = [1.0, 2.0, 1.0]
    if embedding_space is not None:
        comps.append(content.ContentSeedKNN(space=embedding_space, p=64))
        weights = {"movie": [1.0, 2.0, 1.0, 0.0], "show": [1.0, 1.0, 1.0, 3.0]}
    return blend.Blend(comps, weights, continuations=True, media_mix=True,
                       name="BL_final" + ("_embed" if embedding_space is not None else ""))


def signals_grid(con):
    reqs = signals.requests_by_user(con)
    yield final_blend()
    yield signals.FamilyFilter(final_blend())
    yield signals.FamilyFilter(final_blend(), filter_candidates=False)
    yield signals.IntentSeeds(final_blend(), reqs)
    yield signals.IntentSeeds(final_blend(), reqs, weight=2.0)
    yield signals.FamilyFilter(signals.IntentSeeds(final_blend(), reqs))


GRIDS = {"graph": graph_grid, "content": content_grid, "movielens": movielens_grid, "blend": blend_grid}


def run(con, which="content") -> None:
    items = data.load_items(con)
    grid = signals_grid(con) if which == "signals" else GRIDS[which]()
    scorers = [baselines.ShortlistRow(), baselines.GlobalPopularity(), graph.GraphPPR()] + list(grid)
    rows = fold_evaluation(con, items, scorers, fold_ids=TUNE_FOLDS, quiet=True)
    table = summarise(rows, scorers)
    print(f"tune folds {sorted(TUNE_FOLDS)}, {table[scorers[0].name]['units']} units")
    print(f"{'scorer':44} {'ndcg50 mov':>10} {'ndcg50 show':>11} {'r@100 mov':>9} {'r@100 show':>10} {'lift':>5} {'year':>5} {'mov%':>5}")
    for name, t in sorted(table.items(), key=lambda kv: -(kv[1]["ndcg@50_movie"] + kv[1]["ndcg@50_show"])):
        lift = t["own_ndcg"] / t["others_ndcg"] if t["others_ndcg"] else math.inf
        print(f"{name:44} {fmt(t['ndcg@50_movie']):>10} {fmt(t['ndcg@50_show']):>11} "
              f"{fmt(t['recall@100_movie']):>9} {fmt(t['recall@100_show']):>10} {fmt(lift, 2):>5} "
              f"{t['median_year']:>5.0f} {fmt(t['movie_share'], 2):>5}")
