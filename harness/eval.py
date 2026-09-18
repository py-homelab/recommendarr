"""Run every scorer over every split and write the report.

Primary metric, fixed in advance: macro NDCG@50 on the arrival-time folds, per media type;
units are (fold, user); Δ against B1 with a user-clustered bootstrap CI and a per-user
win/loss count. Everything else is diagnostic or a gate."""

import math
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

import numpy as np

from . import baselines, config, data

K_LIST = (10, 20, 50, 100)
PRIMARY_K = 50
LIST_K = 50
REFERENCE = "B1_shortlist_row"
REFERENCES = ("B1_shortlist_row", "B2_global_popularity")   # today's engine, and the bar to beat
TEST_FOLDS = {"fold8", "fold9", "fold10"}   # never used for tuning; the adoption decision reads these
BOOTSTRAP = 2000
MEDIA_TYPES = ("movie", "show")


def ranked(scores: dict, candidates: set, media_type: str | None = None) -> list:
    keys = [k for k in candidates if (media_type is None or k[1] == media_type)]
    return sorted(keys, key=lambda k: scores.get(k, -math.inf), reverse=True)


def ndcg(ranking: list, positives: set, k: int) -> float:
    if not positives:
        return math.nan
    dcg = sum(1 / math.log2(i + 2) for i, key in enumerate(ranking[:k]) if key in positives)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(positives))))
    return dcg / ideal


def recall(ranking: list, positives: set, k: int) -> float:
    if not positives:
        return math.nan
    return len(positives & set(ranking[:k])) / len(positives)


def js_divergence(p: Counter, q: Counter) -> float:
    keys = set(p) | set(q)
    ps, qs = sum(p.values()) or 1, sum(q.values()) or 1
    out = 0.0
    for key in keys:
        a, b = p[key] / ps, q[key] / qs
        m = (a + b) / 2
        out += 0.5 * (a * math.log2(a / m) if a else 0) + 0.5 * (b * math.log2(b / m) if b else 0)
    return out


def decade_target(ctx, items) -> Counter:
    return Counter(
        (items[s.key].year // 10) * 10 for s in ctx.seeds if s.key in items and items[s.key].year
    )


def movie_share_target(ctx) -> float:
    recent = [s for s in ctx.seeds if s.first_at > ctx.cutoff - 365 * 86400]
    pool = recent if len(recent) >= 5 else ctx.seeds
    return sum(s.key[1] == "movie" for s in pool) / max(1, len(pool))


def evaluate_split(scorers, contexts, positives, items, unit_prefix):
    """positives: user_id -> set of keys. Returns rows of per-(unit, scorer) metrics and
    the top-50 lists needed for cross-user metrics."""
    for s in scorers:
        if hasattr(s, "prepare"):
            s.prepare(contexts, items)
    rows, top_lists = [], {}
    for u, ctx in contexts.items():
        pos_all = positives.get(u, set())
        pos = pos_all & ctx.candidates
        for s in scorers:
            scores = s(ctx, items)
            combined = ranked(scores, ctx.candidates)[:LIST_K]
            top_lists[(unit_prefix, u, s.name)] = combined
            row = {
                "unit": unit_prefix, "user": u, "scorer": s.name,
                "positives": len(pos_all), "coverage": len(pos) / len(pos_all) if pos_all else math.nan,
                "scored": len(scores),
                "pool_recall": len([k for k in pos if k in scores]) / len(pos) if pos else math.nan,
            }
            for mt in MEDIA_TYPES:
                r = ranked(scores, ctx.candidates, mt)
                p = {k for k in pos if k[1] == mt}
                row[f"ndcg@{PRIMARY_K}_{mt}"] = ndcg(r, p, PRIMARY_K)
                for k in K_LIST:
                    row[f"recall@{k}_{mt}"] = recall(r, p, k)
            row["ndcg@50_all"] = ndcg(ranked(scores, ctx.candidates), pos, PRIMARY_K)
            years = sorted(items[k].year for k in combined if items[k].year)
            row["median_year"] = years[len(years) // 2] if years else math.nan
            row["movie_share"] = sum(k[1] == "movie" for k in combined) / max(1, len(combined))
            row["movie_share_target"] = movie_share_target(ctx)
            row["decade_js"] = js_divergence(
                Counter((items[k].year // 10) * 10 for k in combined if items[k].year),
                decade_target(ctx, items),
            )
            rows.append(row)
    # personalisation: this user's list on their positives vs other users' lists on them
    for row in rows:
        u, name = row["user"], row["scorer"]
        ctx, pos = contexts[u], positives.get(u, set()) & contexts[u].candidates
        own = ndcg(top_lists[(unit_prefix, u, name)], pos, PRIMARY_K)
        others = [
            ndcg([k for k in top_lists[(unit_prefix, v, name)] if k in ctx.candidates], pos, PRIMARY_K)
            for v in contexts if v != u
        ]
        others = [o for o in others if not math.isnan(o)]
        row["own_ndcg"], row["others_ndcg"] = own, float(np.mean(others)) if others else math.nan
        mine = set(top_lists[(unit_prefix, u, name)])
        jac = [
            len(mine & set(top_lists[(unit_prefix, v, name)])) / max(1, len(mine | set(top_lists[(unit_prefix, v, name)])))
            for v in contexts if v != u
        ]
        row["jaccard"] = float(np.mean(jac)) if jac else math.nan
    return rows


def paired_delta(rows, metric, reference=REFERENCE):
    """Δ per unit vs the reference scorer, then a user-clustered bootstrap CI and win/loss."""
    ref = {(r["unit"], r["user"]): r[metric] for r in rows if r["scorer"] == reference}
    out = {}
    by_scorer = defaultdict(list)
    for r in rows:
        base = ref.get((r["unit"], r["user"]))
        if base is None or math.isnan(base) or math.isnan(r[metric]):
            continue
        by_scorer[r["scorer"]].append((r["user"], r[metric] - base))
    rng = np.random.default_rng(0)
    for name, deltas in by_scorer.items():
        users = sorted({u for u, _ in deltas})
        per_user = {u: [d for uu, d in deltas if uu == u] for u in users}
        means = []
        for _ in range(BOOTSTRAP):
            sample = rng.choice(users, size=len(users), replace=True)
            vals = [d for u in sample for d in per_user[u]]
            means.append(float(np.mean(vals)))
        wins = sum(np.mean(per_user[u]) > 1e-9 for u in users)
        losses = sum(np.mean(per_user[u]) < -1e-9 for u in users)
        out[name] = {
            "delta": float(np.mean([d for _, d in deltas])),
            "ci": (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))),
            "wins": wins, "losses": losses, "users": len(users),
        }
    return out


def summarise(rows, scorers):
    cols = [c for c in rows[0] if c not in ("unit", "user", "scorer")]
    table = {}
    for s in scorers:
        sub = [r for r in rows if r["scorer"] == s.name]
        table[s.name] = {c: float(np.nanmean([r[c] for r in sub])) for c in cols}
        table[s.name]["units"] = len(sub)
    return table


def fmt(x, digits=3):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def write_report(fold_rows, holdout_rows, scorers, path):
    lines = [f"# Offline evaluation — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC", ""]
    for title, rows, primary in (
        ("Arrival-time folds (primary)", fold_rows, True),
        ("Temporal holdout (secondary; library included in candidates)", holdout_rows, False),
    ):
        if not rows:
            continue
        table = summarise(rows, scorers)
        lines += [f"## {title}", "", f"Units: {table[scorers[0].name]['units']} (fold × user).", ""]
        lines += ["| scorer | NDCG@50 movie | NDCG@50 show | recall@50 movie | recall@50 show | recall@100 movie | recall@100 show | coverage | scored | pool recall |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for name, t in table.items():
            lines.append(f"| {name} | {fmt(t['ndcg@50_movie'])} | {fmt(t['ndcg@50_show'])} | {fmt(t['recall@50_movie'])} | {fmt(t['recall@50_show'])} | {fmt(t['recall@100_movie'])} | {fmt(t['recall@100_show'])} | {fmt(t['coverage'], 2)} | {t['scored']:.0f} | {fmt(t['pool_recall'], 2)} |")
        lines += ["", "coverage = share of a user's positives that are in the candidate set at all (catalogue, "
                  "not in library, not already requested, released by window end); pool recall = share of "
                  "those the scorer assigned any score to (its own candidate pool)."]
        lines += ["", "### The complaint as numbers (top-50 combined list)", "",
                  "| scorer | personalisation lift | Jaccard@50 vs others | median year | movie share | movie share target | decade JS |",
                  "|---|---|---|---|---|---|---|"]
        for name, t in table.items():
            lift = t["own_ndcg"] / t["others_ndcg"] if t["others_ndcg"] else math.inf
            lines.append(f"| {name} | {fmt(lift, 2)} | {fmt(t['jaccard'], 2)} | {t['median_year']:.0f} | {fmt(t['movie_share'], 2)} | {fmt(t['movie_share_target'], 2)} | {fmt(t['decade_js'])} |")
        if primary:
            test_rows = [r for r in rows if r["unit"] in TEST_FOLDS]
            for label, subset in (("all folds", rows), (f"test folds only ({', '.join(sorted(TEST_FOLDS))}; tuning never saw these)", test_rows)):
                for reference in REFERENCES:
                    lines += ["", f"### Paired Δ vs {reference}, {label} (user-clustered bootstrap 95% CI, per-user wins/losses)", ""]
                    for metric in ("ndcg@50_movie", "ndcg@50_show", "ndcg@50_all"):
                        lines += [f"**{metric}**", "", "| scorer | Δ | 95% CI | wins | losses | users |", "|---|---|---|---|---|---|"]
                        for name, d in paired_delta(subset, metric, reference).items():
                            if name != reference:
                                lines.append(f"| {name} | {d['delta']:+.3f} | [{d['ci'][0]:+.3f}, {d['ci'][1]:+.3f}] | {d['wins']} | {d['losses']} | {d['users']} |")
                        lines.append("")
        lines.append("")
    lines += [
        "## Caveats", "",
        "- TMDb recommendation/similar lists are as of today, not as of each cutoff; this favours "
        "the TMDb-based scorers (B0, B1, later the graph). Candidates are limited to titles released "
        "by the end of each fold's window, which bounds but does not remove the effect.",
        "- B0 gates on TMDb rating and a TMDb-vote proxy (500 movies / 180 shows) for the live "
        "inbox's 5,000 IMDb votes via MDBList.",
        "- Titles anyone had requested before a cutoff are excluded from that fold's candidates, "
        "as picks would park them; positives among them are counted against coverage, not recall.",
        "- Library membership at a cutoff is reconstructed from Seerr's added dates; titles since "
        "removed from the library are unknown and may appear as candidates.",
        "- Fold positives include Seerr requests; those filed through picks were shaped by "
        "Shortlist's inbox and favour B0/B1. They are not yet separable (picks.db not pulled).",
        "",
    ]
    path.write_text("\n".join(lines))


def fold_evaluation(con, items, scorers, fold_ids=None, quiet=False):
    rows = []
    for f in con.execute("SELECT fold_id FROM folds"):
        fid = f["fold_id"]
        if fold_ids is not None and fid not in fold_ids:
            continue
        contexts = data.fold_contexts(con, items, fid)
        positives = defaultdict(set)
        for r in con.execute("SELECT user_id, tmdb_id, media_type FROM fold_positives WHERE fold_id = ?", (fid,)):
            positives[r["user_id"]].add((r["tmdb_id"], r["media_type"]))
        rows += evaluate_split(scorers, contexts, positives, items, f"fold{fid}")
        if not quiet:
            print(f"fold {fid}: {len(contexts)} users done")
    return rows


def run(con, scorers=None) -> None:
    items = data.load_items(con)
    scorers = scorers or [cls() for cls in baselines.BASELINES]
    fold_rows = fold_evaluation(con, items, scorers)
    positives = defaultdict(set)
    for r in con.execute("SELECT user_id, tmdb_id, media_type FROM holdout"):
        positives[r["user_id"]].add((r["tmdb_id"], r["media_type"]))
    holdout_rows = evaluate_split(scorers, data.holdout_contexts(con, items, int(time.time())), positives, items, "holdout")
    config.REPORTS_DIR.mkdir(exist_ok=True)
    path = config.REPORTS_DIR / f"eval-{datetime.now():%Y%m%d-%H%M}.md"
    write_report(fold_rows, holdout_rows, scorers, path)
    print(f"report: {path}")
    for name, t in summarise(fold_rows, scorers).items():
        print(f"{name:40} ndcg50 movie {fmt(t['ndcg@50_movie'])} show {fmt(t['ndcg@50_show'])}  "
              f"r@100 movie {fmt(t['recall@100_movie'])} show {fmt(t['recall@100_show'])}  "
              f"lift {fmt(t['own_ndcg'] / t['others_ndcg'] if t['others_ndcg'] else math.inf, 2)}  "
              f"median year {t['median_year']:.0f}  movie share {fmt(t['movie_share'], 2)}")
