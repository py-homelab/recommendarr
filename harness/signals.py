"""Context transforms that any scorer can be wrapped in, so each signal is A/B-able.

FamilyFilter: kids' content watched mostly on a shared TV under an adult's account is the
household's viewing, not theirs; those seeds are down-weighted (routed to a household
profile in the engine). Also drops kids' titles from the candidates of that adult surface.
IntentSeeds: the user's own Seerr requests before the cutoff are explicit statements of
taste; they join the seeds at full engagement."""

import dataclasses

from .data import Item, Key, Seed, UserContext

KIDS_CERTS = {"G", "TV-Y", "TV-Y7", "TV-Y7-FV", "TV-G"}
GENRE_ANIMATION, GENRE_FAMILY, GENRE_KIDS = 16, 10751, 10762


def is_kids(it: Item) -> bool:
    if it.certification in KIDS_CERTS:
        return True
    genres = set(it.genres)
    return GENRE_KIDS in genres or {GENRE_ANIMATION, GENRE_FAMILY} <= genres


class FamilyFilter:
    def __init__(self, scorer, tv_threshold=0.5, keep=0.1, filter_candidates=True):
        self.scorer, self.tv_threshold, self.keep = scorer, tv_threshold, keep
        self.filter_candidates = filter_candidates
        self.name = f"{scorer.name}+family(keep={keep},cands={int(filter_candidates)})"

    def prepare(self, contexts, items):
        if hasattr(self.scorer, "prepare"):
            self.scorer.prepare(contexts, items)

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        seeds = []
        for s in ctx.seeds:
            it = items.get(s.key)
            if it and is_kids(it) and s.tv_share >= self.tv_threshold:
                seeds.append(dataclasses.replace(s, weight=s.weight * self.keep))
            else:
                seeds.append(s)
        candidates = ctx.candidates
        if self.filter_candidates:
            candidates = {k for k in candidates if not is_kids(items[k])}
        return self.scorer(dataclasses.replace(ctx, seeds=seeds, candidates=candidates), items)


class IntentSeeds:
    def __init__(self, scorer, requests_by_user: dict[int, list[tuple[Key, int]]], weight=1.0):
        self.scorer, self.requests, self.weight = scorer, requests_by_user, weight
        self.name = f"{scorer.name}+intent(w={weight:g})"

    def prepare(self, contexts, items):
        if hasattr(self.scorer, "prepare"):
            self.scorer.prepare(contexts, items)

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        have = {s.key for s in ctx.seeds}
        extra = [
            Seed(key, self.weight, created_at, created_at, 1.0)
            for key, created_at in self.requests.get(ctx.user_id, [])
            if created_at <= ctx.cutoff and key not in have and key in items
        ]
        return self.scorer(dataclasses.replace(ctx, seeds=ctx.seeds + extra), items)


def requests_by_user(con) -> dict[int, list[tuple[Key, int]]]:
    out = {}
    for r in con.execute("SELECT user_id, tmdb_id, media_type, created_at FROM seerr_requests WHERE user_id IS NOT NULL"):
        out.setdefault(r["user_id"], []).append(((r["tmdb_id"], r["media_type"]), r["created_at"]))
    return out
