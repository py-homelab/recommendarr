"""Synthetic end-to-end check of scorers, metrics and the report writer, no network."""

import math
import random

from harness import baselines, eval as evaluation
from harness.data import Item, Seed, UserContext


def make_items(n=300, seed=1):
    rng = random.Random(seed)
    items = {}
    for i in range(n):
        mt = "movie" if i % 3 else "show"
        key = (i, mt)
        items[key] = Item(
            key, f"t{i}", 1980 + rng.randrange(45), f"{1980 + rng.randrange(45)}-01-01",
            rng.sample(range(10), 2), rng.sample(range(40), 4), rng.sample(range(100), 5),
            [rng.randrange(30)], "en", "PG", None, 100, None, 5 + rng.random() * 4, rng.randrange(20000),
            rng.random() * 100, [], [], True,
        )
    same_type = lambda mt: [k[0] for k in items if k[1] == mt]
    for key, it in items.items():
        pool = same_type(key[1])
        it.recommendations = rng.sample(pool, 20)
        it.similar = rng.sample(pool, 20)
    return items


def make_contexts(items, cutoff=1_700_000_000):
    rng = random.Random(2)
    keys = list(items)
    contexts, positives = {}, {}
    seeds_by_user = {}
    for u in (1, 2, 3):
        chosen = rng.sample(keys, 40)
        seeds_by_user[u] = [Seed(k, rng.random(), cutoff - rng.randrange(10**7), cutoff - rng.randrange(10**6), 1.0) for k in chosen]
    for u, seeds in seeds_by_user.items():
        seen = {s.key for s in seeds}
        contexts[u] = UserContext(
            u, cutoff, seeds, set(), set(keys) - seen,
            {v: s for v, s in seeds_by_user.items() if v != u},
            {v: {keys[0]} for v in seeds_by_user if v != u},
        )
        positives[u] = set(rng.sample(sorted(set(keys) - seen), 15))
    return contexts, positives


def test_metrics():
    ranking = [(1, "movie"), (2, "movie"), (3, "movie")]
    assert evaluation.ndcg(ranking, {(1, "movie")}, 10) == 1.0
    assert evaluation.recall(ranking, {(3, "movie"), (9, "movie")}, 10) == 0.5
    assert math.isnan(evaluation.ndcg(ranking, set(), 10))


def test_shortlist_seeds_guarantee_per_media_type():
    items = make_items()
    contexts, _ = make_contexts(items)
    seeds = baselines.shortlist_seeds(contexts[1])
    assert len(seeds) == baselines.MAX_SEEDS
    by_type = {mt: sum(k[1] == mt for k, _ in seeds) for mt in ("movie", "show")}
    assert min(by_type.values()) >= baselines.MAX_SEEDS // 3
    assert all(0 < w <= 1 for _, w in seeds)


def test_baselines_and_report(tmp_path):
    items = make_items()
    contexts, positives = make_contexts(items)
    scorers = [cls() for cls in baselines.BASELINES]
    rows = evaluation.evaluate_split(scorers, contexts, positives, items, "fold0")
    assert len(rows) == len(scorers) * len(contexts)
    for r in rows:
        assert 0 <= r["ndcg@50_movie"] <= 1 and 0 <= r["ndcg@50_show"] <= 1
        assert r["coverage"] == 1.0
        assert r["scored"] > 0
    deltas = evaluation.paired_delta(rows, "ndcg@50_all")
    assert set(deltas) == {s.name for s in scorers}
    assert deltas[evaluation.REFERENCE]["delta"] == 0.0
    path = tmp_path / "report.md"
    evaluation.write_report(rows, rows, scorers, path)
    text = path.read_text()
    assert "B0_shortlist_inbox" in text and "Paired Δ" in text
