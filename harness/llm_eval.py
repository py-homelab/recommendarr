"""Measure the LLM components against the shipped blend on every fold (no hyperparameters
are tuned here, so all folds are reported, with the test folds separately)."""

import sys

from . import content, data, db, llm, signals, tune
from .eval import TEST_FOLDS, fmt, fold_evaluation, paired_delta, summarise


def run(which: str) -> None:
    con = db.connect()
    items = data.load_items(con)
    reqs = signals.requests_by_user(con)
    base = signals.IntentSeeds(tune.final_blend(), reqs)
    scorers = [base]
    if which in ("rerank", "all"):
        scorers.append(llm.LLMRerank(signals.IntentSeeds(tune.final_blend(), reqs), top_n=60, provider="openrouter"))
    if which in ("embed", "all"):
        space = llm.EmbeddingSpace(con, items)
        scorers.append(content.ContentSeedKNN(space=space, name="C2_content_gemini_embed"))
        scorers.append(content.ContentSeedKNN(name="C1_content_knn"))
        emb_blend = tune.final_blend()
        emb_blend.components[1] = content.ContentSeedKNN(space=space)
        emb_blend.name = "BL_final(embed)"
        scorers.append(signals.IntentSeeds(emb_blend, reqs))
    rows = fold_evaluation(con, items, scorers, quiet=True)
    table = summarise(rows, scorers)
    print(f"{'scorer':46} {'ndcg50 mov':>10} {'ndcg50 show':>11} {'r@100 mov':>9} {'r@100 show':>10} {'lift':>5}")
    for name, t in table.items():
        lift = t["own_ndcg"] / t["others_ndcg"] if t["others_ndcg"] else float("inf")
        print(f"{name:46} {fmt(t['ndcg@50_movie']):>10} {fmt(t['ndcg@50_show']):>11} {fmt(t['recall@100_movie']):>9} {fmt(t['recall@100_show']):>10} {fmt(lift, 2):>5}")
    for label, subset in (("all folds", rows), ("test folds", [r for r in rows if r["unit"] in TEST_FOLDS])):
        print(f"--- paired Δ vs {base.name}, {label}")
        for metric in ("ndcg@50_movie", "ndcg@50_show", "ndcg@50_all"):
            for name, d in paired_delta(subset, metric, base.name).items():
                if name != base.name:
                    print(f"  {metric:14} {name:46} {d['delta']:+.3f} [{d['ci'][0]:+.3f}, {d['ci'][1]:+.3f}] {d['wins']}W/{d['losses']}L")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "rerank")
