"""LLM components under test (Gemini; OpenRouter as fallback for generation).

E1 EmbeddingSpace: gemini-embedding-2 vectors of each title's metadata text (title, year,
type, genres, keywords, overview), a drop-in item space for the per-seed content kNN.
E2 LLMRerank: gemini-3.8-flash reranks the blend's top-N for a user given their history.
Both cache every API response on disk; a rerun costs nothing. User-side use sends viewing
histories to Google; Pavel approved this for all users on 2026-09-17."""

import hashlib
import json
import re
import sqlite3
import time
import zlib

import numpy as np
import requests

from . import config
from .data import Item, Key, UserContext

EMBED_MODEL = "gemini-embedding-2"
EMBED_DIMS = 768
GEN_MODEL = "gemini-3.8-flash"
GEMINI = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-3.8-flash"

GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
          18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
          9648: "Mystery", 10749: "Romance", 878: "Science Fiction", 10770: "TV Movie", 53: "Thriller",
          10752: "War", 37: "Western", 10759: "Action & Adventure", 10762: "Kids", 10763: "News",
          10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"}

_cache_con = None


def _cache() -> sqlite3.Connection:
    global _cache_con
    if _cache_con is None:
        _cache_con = sqlite3.connect(config.DATA_DIR / "llm_cache.db", check_same_thread=False)
        _cache_con.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, model TEXT, body BLOB, created_at INTEGER)")
    return _cache_con


def _cached(key: str):
    row = _cache().execute("SELECT body FROM responses WHERE key = ?", (key,)).fetchone()
    return json.loads(zlib.decompress(row[0])) if row else None


def _store(key: str, model: str, body) -> None:
    _cache().execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?)",
                     (key, model, zlib.compress(json.dumps(body).encode()), int(time.time())))
    _cache().commit()


def _post(url: str, body: dict, headers: dict) -> dict:
    """Quotas are per minute; wait them out rather than fail (a 30k-item embedding run
    simply takes as long as the quota allows)."""
    for attempt in range(40):
        r = requests.post(url, json=body, headers=headers, timeout=120)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", min(60, 5 * (attempt + 1)))))
            continue
        if not r.ok:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError("LLM: gave up after retries")


# ---------------------------------------------------------------- E1: item embeddings

def keyword_names() -> dict[int, str]:
    """Keyword id -> name, from the TMDb detail responses already in the cache."""
    names = {}
    con = sqlite3.connect(config.DATA_DIR / "tmdb_cache.db")
    for (body,) in con.execute("SELECT body FROM responses WHERE key LIKE '/movie/%append%' OR key LIKE '/tv/%append%'"):
        b = json.loads(zlib.decompress(body)) or {}
        kw = b.get("keywords") or {}
        for k in kw.get("keywords") or kw.get("results") or []:
            names[k["id"]] = k["name"]
    return names


def item_text(it: Item, overview: str | None, kw: dict[int, str]) -> str:
    kind = "Film" if it.media_type == "movie" else "TV series"
    parts = [f"{it.title} ({it.year or 'n/a'}). {kind}."]
    if it.genres:
        parts.append("Genres: " + ", ".join(GENRES.get(g, str(g)) for g in it.genres) + ".")
    if it.keywords:
        parts.append("Themes: " + ", ".join(kw[k] for k in it.keywords[:25] if k in kw) + ".")
    if it.certification:
        parts.append(f"Rated {it.certification}.")
    if overview:
        parts.append(overview[:300])
    return " ".join(parts)


def embed_texts(texts: list[str], provider="openrouter") -> np.ndarray:
    """Gemini's free tier allows 100 embedded texts per minute (a 5-hour catalogue run);
    OpenRouter serves the same model for ~$1 per catalogue, so it is the default. Vectors are
    truncated to EMBED_DIMS (the model is Matryoshka-trained) and re-normalised."""
    key = f"emb:{provider}:" + hashlib.sha1(("\n".join(texts) + EMBED_MODEL).encode()).hexdigest()
    body = _cached(key)
    if body is None:
        if provider == "gemini":
            body = _post(
                f"{GEMINI}/models/{EMBED_MODEL}:batchEmbedContents",
                {"requests": [{"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": t}]},
                               "taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": EMBED_DIMS} for t in texts]},
                {"x-goog-api-key": config.secret("GEMINI_API_KEY")},
            )
        else:
            body = _post(
                "https://openrouter.ai/api/v1/embeddings",
                {"model": f"google/{EMBED_MODEL}", "input": texts},
                {"Authorization": f"Bearer {config.secret('OPENROUTER_API_KEY')}"},
            )
        _store(key, EMBED_MODEL, body)
    if "embeddings" in body:
        X = np.array([e["values"] for e in body["embeddings"]], dtype=np.float32)
    else:
        X = np.array([e["embedding"] for e in sorted(body["data"], key=lambda e: e["index"])], dtype=np.float32)
    return X[:, :EMBED_DIMS]


def build_embeddings(con, items: dict[Key, Item], batch=100) -> tuple[list[Key], np.ndarray]:
    """Incremental: only titles without a vector are embedded. Without an OPENROUTER_API_KEY
    new titles are simply left out (the blend renormalises over available components)."""
    path = config.DATA_DIR / f"embeddings_{EMBED_MODEL}_{EMBED_DIMS}.npz"
    keys, X = [], np.zeros((0, EMBED_DIMS), dtype=np.float32)
    if path.exists():
        z = np.load(path, allow_pickle=True)
        keys, X = [tuple(k) for k in z["keys"].tolist()], z["X"]
    have = set(keys)
    missing = [k for k in items if k not in have]
    if not missing:
        return keys, X
    try:
        config.secret("OPENROUTER_API_KEY")
    except SystemExit:
        print(f"embeddings: {len(missing)} titles have no vector and OPENROUTER_API_KEY is not set; skipping")
        return keys, X
    kw = keyword_names()
    overviews = {(r[0], r[1]): r[2] for r in con.execute("SELECT tmdb_id, media_type, overview FROM items")}
    texts = [item_text(items[k], overviews.get(k), kw) for k in missing]
    rows = []
    for i in range(0, len(texts), batch):
        rows.append(embed_texts(texts[i:i + batch]))
        if (i // batch) % 50 == 0:
            print(f"  embedded {min(i + batch, len(texts))}/{len(texts)}")
    new = np.vstack(rows)
    new /= np.maximum(np.linalg.norm(new, axis=1, keepdims=True), 1e-9)
    keys, X = keys + missing, np.vstack([X, new])
    np.savez(path, keys=np.array(keys, dtype=object), X=X)
    return keys, X


class EmbeddingSpace:
    """Same interface as content.ItemSpace (X rows unit-normalised, index, votes)."""

    def __init__(self, con, items: dict[Key, Item]):
        keys, X = build_embeddings(con, items)
        self.keys, self.X = keys, X
        self.index = {k: i for i, k in enumerate(keys)}
        self.votes = np.array([items[k].vote_count for k in keys], dtype=np.float64)


# ---------------------------------------------------------------- E2: user-side reranker

def generate(prompt: str, provider="gemini") -> str:
    key = f"gen:{provider}:" + hashlib.sha1((prompt + GEN_MODEL).encode()).hexdigest()
    body = _cached(key)
    if body is None:
        if provider == "gemini":
            body = _post(
                f"{GEMINI}/models/{GEN_MODEL}:generateContent",
                {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}},
                {"x-goog-api-key": config.secret("GEMINI_API_KEY")},
            )
        else:
            body = _post(
                OPENROUTER,
                {"model": OPENROUTER_MODEL, "temperature": 0, "messages": [{"role": "user", "content": prompt}],
                 "response_format": {"type": "json_object"}},
                {"Authorization": f"Bearer {config.secret('OPENROUTER_API_KEY')}"},
            )
        _store(key, GEN_MODEL, body)
    if provider == "gemini":
        return body["candidates"][0]["content"]["parts"][0]["text"]
    return body["choices"][0]["message"]["content"]


PROMPT = """You rank titles for one member of a family Plex server. Predict which of the candidate titles this person is most likely to choose to watch next, judging from their own viewing history. Weigh recent and heavily watched history more. Do not favour famous titles for their own sake; favour fit with this person's demonstrated taste.

THEIR HISTORY (most relevant first; "eps" = episodes completed):
{history}

TITLES THEY STARTED AND ABANDONED:
{negatives}

CANDIDATES (id | title | year | type | genres | synopsis):
{candidates}

Return JSON only: {{"ranking": [<candidate ids, best first, all {n} of them>]}}"""


class LLMRerank:
    def __init__(self, scorer, top_n=60, history_n=60, provider="gemini", name=None):
        self.scorer, self.top_n, self.history_n, self.provider = scorer, top_n, history_n, provider
        self.name = name or f"{scorer.name}+llm(top{top_n},{provider})"
        self.overviews = None

    def prepare(self, contexts, items):
        if hasattr(self.scorer, "prepare"):
            self.scorer.prepare(contexts, items)

    def _prompt(self, ctx: UserContext, cands: list[Key], items) -> str:
        seeds = sorted(ctx.seeds, key=lambda s: -s.weight)[: self.history_n]
        history = "\n".join(
            f"- {items[s.key].title} ({items[s.key].year}, {'film' if s.key[1] == 'movie' else 'series'})"
            for s in seeds if s.key in items)
        negatives = "\n".join(f"- {items[k].title} ({items[k].year})" for k in list(ctx.negatives)[:15] if k in items) or "- none"
        lines = []
        for i, k in enumerate(cands):
            it = items[k]
            syn = (self.overviews.get(k) or "")[:140].replace("\n", " ")
            lines.append(f"{i} | {it.title} | {it.year} | {'film' if k[1] == 'movie' else 'series'} | "
                         f"{', '.join(GENRES.get(g, '') for g in it.genres[:3])} | {syn}")
        return PROMPT.format(history=history, negatives=negatives, candidates="\n".join(lines), n=len(cands))

    def __call__(self, ctx: UserContext, items: dict[Key, Item]) -> dict[Key, float]:
        if self.overviews is None:
            from . import db
            con = db.connect()
            self.overviews = {(r[0], r[1]): r[2] for r in con.execute("SELECT tmdb_id, media_type, overview FROM items")}
        base = self.scorer(ctx, items)
        order = sorted(base, key=lambda k: -base[k])
        head, tail = order[: self.top_n], order[self.top_n:]
        if len(head) < 5:
            return base
        try:
            text = generate(self._prompt(ctx, head, items), self.provider)
            ranking = json.loads(re.search(r"\{.*\}", text, re.S).group(0))["ranking"]
            ranked = [head[int(i)] for i in ranking if str(i).isdigit() and int(i) < len(head)]
        except Exception as exc:
            print(f"llm rerank failed for user {ctx.user_id}: {exc!r}")
            return base
        seen = set(ranked)
        new_order = ranked + [k for k in head if k not in seen] + tail
        return {k: float(len(new_order) - i) for i, k in enumerate(new_order)}
