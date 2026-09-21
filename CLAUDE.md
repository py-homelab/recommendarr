# CLAUDE.md — recommendarr

A standalone, per-user recommendation engine for Pavel's Plex library. This directory is a
fresh project; the homelab GitOps monorepo it will eventually deploy through is
`~/Code/homelab-stacks` and is managed by a *different* Claude session. Read this whole file
before doing anything; then read `docs/research/` in order.

> **Naming note.** `fingerthief/recommendarr` was an existing (now deleted) LLM-prompt
> recommendation tool; a fork `sammcj/recommendarr` still exists. If this project is ever
> published, pick a name that does not collide.

---

## 1. Goal

Give **every Plex user their own, genuinely personal** suggestions, in two surfaces:

1. **Missing titles** — things the library does not have, shown to that person in the
   existing swipe/grid PWA `picks.yarmak.me`, which files a Seerr request *as them* when
   they swipe right. This is the surface Pavel is unhappy with today and the first target.
2. **In-library titles** (later, optional) — re-discovery rows inside Plex. Today Shortlist
   does this and does it acceptably; replacing it is a second phase, if at all.

The complaint that started this (2026-09-17): the suggestions are **too old** and **too
TV-heavy**, and they are not really personal — they are whatever many users' seeds point at.
The root cause is documented in `docs/research/01-shortlist-engine-review.md`. In one line:
Shortlist ranks its Plex rows properly but builds its missing-title inbox from the **raw,
unranked** TMDb pool, sorted by cross-user demand, and only the demand head ever gets rated.

### Hard constraints (Pavel's, standing)

- **Nothing is ever auto-queued for download.** The engine recommends; a human swipes; Seerr's
  own approval applies. No Radarr/Sonarr writes from this project, ever.
- **Never file a request as Pavel's own account** for testing — his Seerr account is admin and
  auto-approves, which downloads. Use a non-admin account.
- **Per user means per user.** Identity is the Plex account id. No surface may show one
  person another person's data. Family viewing on the owner's account (kids' shows on the
  living-room TV) must not pollute the owner's adult profile.
- **Read-only against live services** unless Pavel says otherwise. Tautulli, Plex, Shortlist,
  Seerr, TMDb reads are fine. Anything that writes to a live system: stop, state what changes
  and what the rollback is, and wait for a go-ahead. Standing exception (granted 2026-09-17):
  refreshing Tautulli's library media-info cache (`get_library_media_info&refresh=true`).
- **No secrets in this repo.** Keys live SOPS-encrypted in `~/Code/homelab-stacks` (see §3).
  If this project needs its own config, keep it in a gitignored `.env` and say so.
- Deployment, when it comes, goes through `homelab-stacks` (a Compose service on TrueNAS,
  x86_64, pull-only images or a repo-config single-file mount like picks). Do not design
  around anything that cannot run there. Coordinate with the homelab-stacks session for that
  step rather than editing that repo from here.
- Keep any plan file lean: open work only.

---

## 2. The environment this plugs into

All hosts are on one flat LAN `192.168.1.0/24`. TrueNAS (`192.168.1.119`) runs everything
below as Docker Compose projects.

| Service | Where | What it gives us |
|---|---|---|
| **Plex** 1.43 + Plex Pass | `media` stack; machine id in `truenas/media/.env` (`PLEX_MACHINE_ID`) | Libraries: **Movies 1,382**, **TV Shows 507 shows / 21,515 episodes**, Audiobooks (ignore). 16 shared users. |
| **Tautulli** | `http://192.168.1.119:8181/api/v2?apikey=…&cmd=…` | **Full per-user watch history**, incl. `player`/`platform` per play, `percent_complete`, `watched_status`, `grandparent_rating_key`, `guid`. `get_users`, `get_history(user_id, media_type, length)`, `get_libraries`. This is the preferred history source. |
| **Shortlist** 1.8.0 (`stevezau/shortlist`) | `http://192.168.1.119:5959`, Bearer token | Today's engine. Owner-level REST API: `/api/users`, `/api/requests` (the missing-title inbox picks reads), `/api/collections` (row specs), `/api/runs/{id}`, `/api/runs/{id}/users/{uid}/trace` (full per-user trace: seeds, gathers, selection). Read-only for us. |
| **Seerr** 3.4.1 | `http://seerr:5055` from inside the media stack; `request.yarmak.me` public | Requests. `X-API-User: <seerr user id>` files a request *as that user*. `/api/v1/user` maps `plexId` → Seerr id. `/api/v1/media` lists what the library has. |
| **picks** (the PWA) | `truenas/media/picks.py` in homelab-stacks, ~1,200 lines, stdlib only, SQLite at `/data/picks.db` | Per-user deck + grid. See §4. **This is the consumer of whatever this engine produces.** |
| **authentik** | `network` stack | Fronts picks; identity arrives as the Plex account id inside the `X-authentik-jwt` claim `ak_proxy.user_attributes.additionalHeaders["X-Plex-Account-Id"]`. Never trust a plain header. |
| **TMDb** | API v3 | Shortlist and Kometa both hold a key; it is **not** in the sops files. Pavel approved reusing **Kometa's** key (its `config.yml` lives NAS-side at `KOMETA_CONFIG_PATH`, see `truenas/media/.env`); export it as `TMDB_API_KEY` in the shell. TMDb's terms cap response caching at 6 months (the harness cache has a 180-day TTL) and forbid ML/AI use — irrelevant for private use, a real problem if this is ever published. |
| Gemini / OpenRouter | `GEMINI_API_KEY`, `OPENROUTER_API_KEY` in the gitignored `.envrc` | Item embeddings (`gemini-embedding-2` via OpenRouter, adopted for shows) and the LLM reranker (measured, not adopted). Gemini's free tier is too slow for the catalogue; see `docs/results.md`. |
| Kometa 2.4.8 | `media` stack, config on the NAS (not in git) | Owns Movies genre normalisation (TMDb genres + merge map) and four decade rows. Not relevant to ranking; relevant if we ever write Plex collections (label conventions, `label!=Shortlist_*` share filters). |

Workstation: Arch, **zsh** (no word-splitting of unquoted vars — write non-trivial shell via
`bash <<'EOF'`), podman not docker, `sops`+`age` installed with the key at
`~/.config/sops/age/keys.txt`.

---

## 3. Where the credentials are (names only)

Decrypt with `sops -d <file>` from inside `~/Code/homelab-stacks`:

| Key | File |
|---|---|
| `SHORTLIST_API_TOKEN` | `truenas/media/secrets.sops.env` |
| `SEERR_PICKS_API_KEY` (Seerr user `picks`, MANAGE_REQUESTS + MANAGE_USERS) | `truenas/media/secrets.sops.env` |
| `TAUTULLI_API_KEY` | `truenas/monitoring/secrets.sops.env` |
| `SEERR_API_KEY` (global admin key — avoid; the picks key is the right one) | `truenas/monitoring/secrets.sops.env` |

Pattern used in this session, read-only:

```bash
cd ~/Code/homelab-stacks && bash <<'EOF'
KEY=$(sops -d truenas/monitoring/secrets.sops.env | sed -n 's/^TAUTULLI_API_KEY=//p' | tr -d '"'"'")
curl -sS "http://192.168.1.119:8181/api/v2?apikey=$KEY&cmd=get_users"
EOF
```

Never echo a decrypted value into a file in this repo, a commit, or a chat message.

---

## 4. The picks PWA — the consumer

`~/Code/homelab-stacks/truenas/media/picks.py`. Read it before designing the engine's output.

- Stdlib `ThreadingHTTPServer`, single file, port 8080, dual-homed on `proxy` (Traefik in,
  Seerr out) and `media-internal` (Shortlist). No published port.
- Identity: `resolve(headers)` reads the Plex account id from the JWT claim (see §2). Fails
  closed to a "no picks yet" page.
- Data: `GET /api/me` returns state, name, Seerr quota, the person's Plex rows, and the
  **suggestion list** currently assembled from Shortlist `GET /api/requests?wanted_by=<user>`
  minus `rejected`/`excluded` minus this user's dismissals, joined with Seerr's media map
  (hides what Plex has, parks what is already requested), enriched with TMDb genres cached in
  `title_meta`. Each item carries `title, year, rating, tmdb_id, media_type, poster_path,
  overview, demand, why[] (row + seed per wanter), shortlist_status, requestable,
  reason_not_requestable`.
- Actions: `POST /api/act {tmdb_id, media_type, action: request|never|later|undo}` → Seerr
  request as the user (`X-API-User`, shows: first regular season only), or a per-user
  dismissal (`never` durable, `later` 30 days). Refuses keys outside the user's current set.
- SQLite tables: `dismissals(plex_id, tmdb_id, media_type, kind, until, title, year, created_at)`,
  `requests_log`, `title_meta`, `backprop_log`.
- Back-propagation: when *every* wanter of a title has dismissed it, picks deletes it from
  Shortlist's inbox (not-now delete), re-swept nightly at 04:30.
- UI: viewport-locked swipe deck on phones (right = request, left = never, up = later), poster
  grid organised by genre on desktop, shared filters (All/Movies/TV, genre, sort: rating |
  votes | year | title).

**The cleanest integration** is: this engine produces, per Plex account id, a ranked list of
missing titles with the same fields picks already renders, plus `why`; picks gains a second
source (or replaces the Shortlist source) behind a flag. The dismissal/request tables in picks
are the feedback signal and should be read, not duplicated.

---

## 5. Research done so far (read in order)

| File | Contents |
|---|---|
| `docs/research/01-shortlist-engine-review.md` | How Shortlist v1.8.0 actually recommends, from source, with the live config and last run's numbers. The diagnosis. |
| `docs/research/02-alternatives.md` | Source-level reviews of SuggestArr, Recommendarr, Immaculaterr, Curatarr, SeekAndWatch, plus a sweep of newer projects (Streamystats, jellyfin-localrecs, jellyfin-helper, netplexflix, playlisterr, PlexMind). What is worth borrowing. |
| `docs/research/03-data-inventory.md` | What data we hold: users, history sizes, library sizes, media split, the family-viewing device signal, API shapes. |
| `docs/research/04-design-direction.md` | The proposed hybrid design, and the clarified idea of injecting our users into an external CF dataset (MovieLens) — what it buys, its limits, how it fits. |
| (no samples copied) | The live JSON the review was based on (Shortlist inbox, run 13, Pavel's trace, Tautulli users) was **not** copied here because it carries friends' Plex usernames. Re-pull it read-only with the commands in `03-data-inventory.md` into the gitignored `docs/research/samples/`. |

Upstream sources reviewed are all public; re-clone if you need line numbers:
`stevezau/shortlist` (tag `v1.8.0`, engine under `shortlist/engine/`), `giuseppe99barchetta/SuggestArr`,
`sammcj/recommendarr`, `ohmzi/Immaculaterr`, `OrchestratedChaos/curatarr`,
`softerfish/seekandwatch`, `fredrikburmester/streamystats`, `rdpharr/jellyfin-plugin-localrecs`.

---

## 6. The harness (built 2026-09-17) and how to run it

`harness/` is the offline evaluation harness; the roadmap it gates is the last section of
`docs/research/04-design-direction.md`. Python via `uv`; all data lands in the gitignored
`data/` (`harness.db`, `tmdb_cache.db`) and `reports/`.

```
uv run python -m harness pull       # Tautulli history, Seerr library (added dates) + requests
uv run python -m harness resolve    # rating keys -> TMDb ids (Tautulli guids, then TMDb search)
uv run python -m harness split      # engagement table, arrival-time folds, temporal holdout
uv run python -m harness catalogue  # local TMDb catalogue (~40k titles; needs TMDB_API_KEY)
uv run python -m harness eval       # every scorer on every split -> reports/eval-*.md
uv run --group dev pytest           # synthetic tests, no network
```

Secrets: `TAUTULLI_API_KEY` and `SEERR_PICKS_API_KEY` are decrypted in memory from
homelab-stacks' sops files when not in the environment; `TMDB_API_KEY` must be exported.
The clients never call `raise_for_status()` because its message embeds the URL with the key.

Scorer contract (`harness/baselines.py`): a callable `(UserContext, items) -> {key: score}`
over `ctx.candidates`, optional `prepare(contexts, items)` for cross-user state. New scorers
are added to the list passed to `eval.run` and must clear the adoption rule in doc 04.

## 7. The engine (built 2026-09-17, **live since 2026-09-18**)

Deployed as the `recommendarr` service in homelab-stacks' media stack (commits `9f17bd5`,
`740d784`): image `ghcr.io/py-homelab/recommendarr:v0.1.0@sha256:9caa53ff…`, dataset
`ssd-storage/configs/recommendarr` (568:568), `media-internal` only, read-only rootfs.
picks reads it (`PICKS_ENGINE_URL=http://recommendarr:8090`, family titles hidden by
default with a toggle) and falls back to Shortlist if the engine returns nothing; unsetting
that variable is the rollback. picks `events`/`dismissals` rows carry `source='engine:v1'`
for engine-served items. Releases: push a `v*` tag → GHCR image → Renovate opens (does not
automerge) a pin-bump PR in homelab-stacks. Source: `github.com/py-homelab/recommendarr`.

`engine/` is the service picks will read. `docs/results.md` holds every number behind it.

```
uv run python -m engine build   # nightly job: pull, resolve, engagement, catalogue refresh, rank all users (both surfaces)
uv run python -m engine serve   # HTTP on :8090 (+ nightly build thread at ENGINE_BUILD_HOUR)
uv run python -m engine check-groups   # validate ENGINE_IDENTITY_GROUPS, print the groups; exit 0/2/3; no build, no table changed
GET  /healthz
GET  /api/suggestions/<plex_id>?limit=200&family=exclude|include|only     # picks: the missing surface
GET  /v1/info                                                             # Shortlist engine protocol (+ features, v0.4.0)
POST /v1/recommend  {plex_account_id, surface: library|missing, media, limit_per_media, library, exclude, season, seeds, seed_focus, …}
```

The `/v1` endpoints are the engine protocol the Shortlist fork (`py-homelab/shortlist`, branch
`engine-plugin`, `docs/guides/engines.md` there) speaks: `surface=library` serves the
`library_suggestions` table (library minus the person's positives, same blend), `surface=missing`
the `suggestions` table; the request's `library`/`exclude`/`excluded_genres` are applied on top,
the order is final, `kids` and a `reason` ride on every item. `ENGINE_TOKEN` (optional) gates
`/v1/*` with a bearer token. Shortlist's own history is not used for ranking (Tautulli has the
device signal); the trace reports `history_sent` vs `history_known` so a stale side shows.

Row settings (v0.4.0): the library surface ranks each person's WHOLE library (the missing surface
stays at 300), `season` narrows the candidates before the per-media cut, and `seed_focus` re-ranks by
closeness to the request's seeds via the nightly `item_neighbours` table (top 100 library neighbours
per library title: TF-IDF content, averaged with gemini embeddings where both titles have one).
`/v1/info` lists `features: ["season", "seed_focus"]`. A start on a build without neighbours rebuilds.
Children's titles (v0.4.1, `signals.is_kids`): rating first — TV-Y/TV-Y7 always, PG-13/R/NC-17/TV-14/TV-MA
never (TMDB tags King of the Hill "Animation, Family"), G/TV-G and unrated only with a children's genre.
A start on a build made by another engine version rebuilds (`engine_meta.built_by`).
The fork draws rows without replacement from one engine answer, re-weights a row's own `recency`, and
builds rows that name their own sources with Shortlist's built-in engine.

Identity groups (v0.5.0, `engine/identity.py`): `ENGINE_IDENTITY_GROUPS="895220:856697834,856698746"`
(canonical:member,member; `;` between groups) pools several Plex accounts as one household — the owner
account plus the Adults and Kids Plex Home profiles. Applied where engagements are built (plays keep
their account; `engagements`, seeds and Seerr-request seeds are grouped under the canonical id), a
pooled member is not ranked or labelled on its own, and both `/v1/recommend` and `/api/suggestions`
answer a member with the canonical's lists (`trace.answered_as`; `trace.identity_pending` while the
rebuild for a changed map has not landed). On `/v1/recommend` every account in a group gets the same
household counts plus `household.group`, so the counts cannot tell the profiles apart: Shortlist's
per-person household override (Kids=kids, Adults=family) is what does, and `group` exists so the fork
can warn when two grouped accounts are left unpinned (fork py.7). `/api/suggestions` has no such
override — a Kids profile asking it gets the household's list under `family=exclude`, i.e. the
adults' — which only matters if picks.py is ever revived. Unset is a strict no-op; a changed map
rebuilds at start (`engine_meta.identity_groups`); a malformed one stops the service at start, and a
build REFUSES a map whose canonical account Tautulli does not know (one mistyped digit would
otherwise leave the household with no lists and nothing to say why). Ids are ASCII digits above zero.
`python -m engine check-groups` validates the variable and prints the groups without building
(exit 0 ok, 2 malformed, 3 canonical unknown; mount the data directory read-only). `python -m harness
split` and the other offline harness commands build `engagements` UNPOOLED — do not run them against
the engine's live data directory while a map is set.

Household label (v0.3.0): the nightly build writes `households(plex_id, label, kids_share,
kids_titles, window_titles)` from each person's last 12 months of positives (children's titles by
`signals.is_kids`) and every `/v1/recommend` answer carries it as `household`. Labels: `adult`,
`family` (≥15% children's titles from ≥4 titles, ≤80%), `kids` (>80%); <10 titles → adult. Env
overrides: `ENGINE_HOUSEHOLD_WINDOW_DAYS`, `ENGINE_HOUSEHOLD_MIN_TITLES`, `ENGINE_FAMILY_MIN_SHARE`,
`ENGINE_FAMILY_MIN_KIDS_TITLES`, `ENGINE_KIDS_ACCOUNT_MIN_SHARE`. Shortlist re-derives the label
from the counts with its own settings, and a per-person override wins over both.

Shipped ranker (`harness/tune.final_blend` = `harness/blend.py` + `harness/signals.IntentSeeds`):
graph PPR + TF-IDF content per-seed kNN + MovieLens EASE + gemini-embedding-2 content kNN,
per-media weights (movies 1/2/1/0, shows 1/1/1/3), per-media percentiles renormalised over
the components that scored the item; own Seerr requests as seeds; continuation bonus 0.1 (rating
≥ 6, one per window of ten); media-mix interleave to the user's recent movie share. Items
carry `kids` so picks can offer a family lane; the API trusts `plex_id` because it listens
only on an internal network — identity is picks' job. Users with < 3 seeds get a
recent-popularity list. A full build for 15 users is ~25 s and needs no TMDb calls beyond
new releases. Data on disk: `harness.db` (~60 MB), `tmdb_cache.db` (~470 MB, 180-day TTL),
`movielens/ease_lam500.npz` (~300 MB), `embeddings_gemini-embedding-2_768.npz` (~92 MB),
`llm_cache.db`. Optional env `OPENROUTER_API_KEY` lets the nightly build embed new titles
(≈ $0.003 per 100 titles); without it new titles simply lack that component. `Containerfile`
builds the x86_64 image; the data directory is a mounted volume at `RECOMMENDARR_DATA`.

Working style for the engine itself: same as picks (Python, small, SQLite, no daemon that
cannot run on TrueNAS). Numpy/scipy are fine; a trained model must ship as a small artefact.
Keep the homelab-stacks session in the loop only at the deployment boundary.

## 8. The Shortlist fork (`~/Code/shortlist`, `github.com/py-homelab/shortlist`)

Decided 2026-09-18: a thin fork of Shortlist with a pluggable engine rather than reimplementing its
Plex delivery/privacy. Fork `dev` has four merged PRs: #1 engine-plugin (`Recommender` protocol,
builtin moved verbatim, `HttpRecommender`, fallback, `RowSpec.family`), #2 person-picks (person role,
trusted-proxy sign-in, `missing` surface → `user_suggestions`, `/api/me*`, the `/me` page, PWA), #3
retire-inbox (inbox + Radarr/Sonarr routing deleted, `requests.overseerr.*` → `seerr.*`), #4 household
(per-person family households from the engine's counts + `family.*` thresholds + per-person override,
`family=auto`, `auth.admin_hosts`, JWT proxy sign-in). Released as `v1.9.1-py.1` →
`ghcr.io/py-homelab/shortlist:1.9.1-py.1` (app version `1.9.1+py.1`). Later fork releases (all image-only, alembic head 0096): py.2–py.6 (row-setting compatibility, carried-pick family re-check, picks toasts, JWKS guard); **py.7** — a kids household's "auto" rows hold only children's titles (`rows._family_means`, recipe part `means=only`, emptied-row takedown, `household.group` + pooled-profile warning, `/me` narrowed for a kids account); **py.8** — watching-account copy narrowed by Plex `contentRating` for a children's profile (per-show visibility, play log narrowed too, planner-derived never-un-mark refusal). Upstream PRs to
`stevezau/shortlist` `dev` would go in the same order. Its rules: engine never imports server, tests
required and no network, migrations guarded and frozen (`scripts/check_migration_freeze.py --write`),
OpenAPI snapshot + `pnpm -C web gen:api` after any route/version change,
`scripts/build_llms_full.py` after docs. Tooling: `.venv/bin/python`,
`web/node_modules/.bin/{tsc,eslint,vite,vitest}` (pnpm via `npx --yes pnpm@11.17.0`).
