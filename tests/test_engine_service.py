"""The engine protocol Shortlist speaks (`/v1/info`, `/v1/recommend`), served from the nightly
tables: filters honoured, order preserved, the token enforced. Synthetic database, no network."""

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from engine import build, service
from harness import catalogue, config, db, engagement


@pytest.fixture
def nightly_db(tmp_path, monkeypatch):
    """A built engine: one user with three library titles and two missing ones ranked."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "harness.db")
    con = db.connect()
    con.executescript(catalogue.SCHEMA)
    con.executescript(engagement.SCHEMA)
    con.executescript(build.SCHEMA)
    items = [
        (10, "movie", "Ten", [18], "Animation"),
        (20, "movie", "Twenty", [16, 10751], None),
        (30, "show", "Thirty", [10765], None),
        (40, "movie", "Forty", [27], None),
        (50, "show", "Fifty", [18], None),
    ]
    for tmdb_id, media, title, genres, _ in items:
        con.execute(
            "INSERT INTO items (tmdb_id, media_type, in_catalogue, title, genres, keywords, cast, crew, "
            "recommendations, similar, vote_average, vote_count, poster_path, overview) "
            "VALUES (?,?,1,?,?,'[]','[]','[]','[]','[]',7.5,1000,'/p.jpg','about it')",
            (tmdb_id, media, title, json.dumps(genres)),
        )
    why = json.dumps([{"seed": "Fargo", "seed_tmdb_id": 900, "media_type": "movie", "kind": "similar"}])
    nxt = json.dumps([{"seed": "Fargo", "seed_tmdb_id": 900, "media_type": "movie", "kind": "next_in_series"}])
    rows = [  # (surface table, rank, tmdb, media, title, kids, why)
        ("library_suggestions", 0, 20, "movie", "Twenty", 1, why),
        ("library_suggestions", 1, 10, "movie", "Ten", 0, nxt),
        ("library_suggestions", 2, 30, "show", "Thirty", 0, "[]"),
        ("suggestions", 0, 40, "movie", "Forty", 0, why),
        ("suggestions", 1, 50, "show", "Fifty", 0, why),
    ]
    for table, rank, tmdb_id, media, title, kids, w in rows:
        con.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?,2020,7.5,1000,'/p.jpg','about it',?,?,?,1000)",
            (100, rank, tmdb_id, media, title, kids, w, 300 - rank),
        )
    con.execute("INSERT INTO builds VALUES (1000, 1, 1.0, 0)")
    con.execute("INSERT INTO builds VALUES (?, 1, 1.0, 0)", (int(__import__("time").time()),))  # fresh
    con.executescript(build.HOUSEHOLD_SCHEMA)
    con.execute("INSERT INTO households VALUES (100, 'family', 0.35, 66, 187, 1000)")
    con.execute(
        "INSERT INTO engagements VALUES (100, 900, 'movie', 1, 1, 1, 0, 100, 1.0, 0.0, 'positive')"
    )
    con.commit()
    con.close()
    return tmp_path


def test_library_surface_keeps_the_nightly_order_and_shortlists_filters(nightly_db):
    out = service.recommend({
        "plex_account_id": 100,
        "surface": "library",
        "media": ["movie", "show"],
        "limit_per_media": 80,
        "library": {"movie": [10, 20], "show": [30]},
        "exclude": [],
        "excluded_genres": [],
        "history": [{"tmdb_id": 900}],
    })
    assert out["ordered"] is True
    assert out["engine"] == {"name": "recommendarr", "version": service.VERSION}
    assert [i["tmdb_id"] for i in out["items"]] == [20, 10, 30]
    first = out["items"][0]
    assert first["kids"] is True and first["genres"] == ["Animation", "Family"]
    assert first["reason"] == "Because you watched Fargo"
    assert first["seed"] == {"tmdb_id": 900, "title": "Fargo", "media_type": "movie"}
    assert out["items"][1]["reason"] == "Next after Fargo"
    assert out["items"][2]["seed"] is None and out["items"][2]["reason"] is None
    assert out["trace"]["history_sent"] == 1 and out["trace"]["history_known"] == 1
    assert out["trace"]["built_at"] > 1000


def test_the_requests_library_exclusions_genres_media_and_limit_are_honoured(nightly_db):
    out = service.recommend({
        "plex_account_id": 100,
        "surface": "library",
        "media": ["movie"],
        "limit_per_media": 1,
        "library": {"movie": [10, 20, 999]},
        "exclude": [[20, "movie"]],
        "excluded_genres": ["Horror"],
    })
    assert [i["tmdb_id"] for i in out["items"]] == [10]
    assert out["trace"]["dropped"] == {"excluded": 1, "other_media": 1}
    out = service.recommend({"plex_account_id": 100, "surface": "library", "excluded_genres": ["Drama"]})
    assert [i["tmdb_id"] for i in out["items"]] == [20, 30]
    assert out["trace"]["dropped"] == {"excluded_genre": 1}


def test_missing_surface_excludes_what_the_library_holds(nightly_db):
    out = service.recommend({"plex_account_id": 100, "surface": "missing", "library": {"movie": [40], "show": []}})
    assert [i["tmdb_id"] for i in out["items"]] == [50]
    assert out["trace"]["dropped"] == {"in_library": 1}


def test_an_unknown_person_gets_an_empty_answer_not_an_error(nightly_db):
    out = service.recommend({"plex_account_id": 7})
    assert out["items"] == [] and out["trace"]["ranked"] == 0


def test_an_unknown_surface_is_refused(nightly_db):
    with pytest.raises(ValueError):
        service.recommend({"plex_account_id": 100, "surface": "moon"})


@pytest.fixture
def server(nightly_db, monkeypatch):
    monkeypatch.setattr(service, "TOKEN", "s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def _call(url, path, body=None, token=None):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_protocol_over_http_with_the_token(server):
    status, info = _call(server, "/v1/info", token="s3cret")
    assert status == 200
    assert info["name"] == "recommendarr" and info["serves_cold"] is True and info["ready"] is True
    assert info["surfaces"] == ["library", "missing"]
    status, out = _call(server, "/v1/recommend", {"plex_account_id": 100, "surface": "library"}, token="s3cret")
    assert status == 200 and [i["tmdb_id"] for i in out["items"]] == [20, 10, 30]


def test_a_missing_or_wrong_token_is_401_and_the_picks_api_needs_none(server):
    assert _call(server, "/v1/info")[0] == 401
    assert _call(server, "/v1/info", token="nope")[0] == 401
    assert _call(server, "/v1/recommend", {"plex_account_id": 100})[0] == 401
    status, out = _call(server, "/api/suggestions/100")
    assert status == 200 and [i["tmdb_id"] for i in out["items"]] == [40, 50]
    assert _call(server, "/healthz")[0] == 200


def test_bad_bodies_are_400(server):
    assert _call(server, "/v1/recommend", {"nope": 1}, token="s3cret")[0] == 400
    assert _call(server, "/v1/recommend", [1, 2], token="s3cret")[0] == 400
    assert _call(server, "/v1/other", {}, token="s3cret")[0] == 404


def test_the_cold_fallback_leads_with_recent_titles_then_fills_by_votes():
    from harness.data import Item, UserContext

    def item(tid, date, votes):
        key = (tid, "movie")
        return key, Item(key, f"t{tid}", 2020, date, [], [], [], [], "en", "PG", None, 100, None, 7.0, votes, 1.0, [], [], True)

    items = dict([item(1, "2000-01-01", 50000), item(2, "2026-01-01", 10), item(3, "2026-02-01", 20), item(4, None, 99999)])
    ctx = UserContext(user_id=1, cutoff=0, seeds=[], negatives=set(), candidates=set(items), household_seeds={}, household_requests={})
    assert build.popularity_fallback(ctx, items) == [(3, "movie"), (2, "movie"), (4, "movie"), (1, "movie")]


def test_the_household_label_rides_with_every_answer(nightly_db):
    out = service.recommend({"plex_account_id": 100, "surface": "library"})
    assert out["household"] == {"label": "family", "kids_share": 0.35, "kids_titles": 66, "window_titles": 187, "window_days": 365}
    assert service.recommend({"plex_account_id": 7})["household"] is None


def test_household_thresholds():
    lab = build.household_label
    assert lab(5, 5) == "adult"          # too few titles to judge
    assert lab(10, 9) == "kids"          # 90%: a child's own account
    assert lab(187, 66) == "family"      # 35%
    assert lab(65, 32) == "family"       # 49%
    assert lab(26, 4) == "family"        # 15.4%, 4 titles: over the line
    assert lab(27, 4) == "adult"         # 14.8%: just under it (a real account on 2026-09-19)
    assert lab(305, 23) == "adult"       # 8%: an adult who likes some animation
    assert lab(20, 3) == "adult"         # 15% but only 3 titles


def test_a_build_without_household_labels_is_rebuilt_on_start(nightly_db, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "run_build", lambda reason: calls.append(reason))
    monkeypatch.setattr(service, "last_build", lambda: 10**10)  # fresh
    monkeypatch.setattr(service.time, "sleep", lambda s: (_ for _ in ()).throw(SystemExit))
    import sqlite3 as _sq
    con = _sq.connect(nightly_db / "harness.db")
    con.execute("DELETE FROM households")
    con.commit()
    con.close()
    try:
        service.nightly()
    except SystemExit:
        pass
    assert calls == ["last build has no household labels"]


def test_an_unwritable_temp_dir_is_replaced_by_one_under_the_data_dir(tmp_path):
    """A read-only container: SQLITE_TMPDIR/TMPDIR unwritable. The engine must still spill big sorts."""
    import subprocess
    import sys

    code = (
        "import os; from harness import config, db; "
        "print(os.environ['SQLITE_TMPDIR']); print(db.check_temp_dir())"
    )
    env = {**__import__("os").environ, "RECOMMENDARR_DATA": str(tmp_path), "SQLITE_TMPDIR": "/proc", "TMPDIR": "/proc"}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout.split("\n")
    assert out[0] == str(tmp_path / "tmp")
    assert out[1] == "None"


def _age_builds(db_dir):
    import sqlite3 as _sq

    con = _sq.connect(db_dir / "harness.db")
    con.execute("DELETE FROM builds WHERE built_at > 1000")
    con.commit()
    con.close()


def test_a_failed_build_is_recorded_and_reported(nightly_db, monkeypatch, capsys):
    _age_builds(nightly_db)
    def boom(con):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(service.build, "build", boom)
    service.run_build("test")
    status = service.build_status()
    assert status["last_build_ok"] is False and status["last_build_error"] == "RuntimeError: disk I/O error"
    assert "Traceback" in capsys.readouterr().err
    # The previous build (built_at 1000, decades ago) is stale: not ready, 503 everywhere it matters.
    assert status["stale"] is True and status["ready"] is False


def test_stale_lists_fail_healthz_and_refuse_recommend_but_picks_still_served(server, nightly_db):
    _age_builds(nightly_db)
    status, body = _call(server, "/healthz")
    assert status == 503 and body["stale"] is True and body["built_at"] == 1000
    status, body = _call(server, "/v1/recommend", {"plex_account_id": 100}, token="s3cret")
    assert status == 503 and "stale" in body["error"]
    assert _call(server, "/api/suggestions/100")[0] == 200  # picks keeps its last list


def test_fresh_lists_are_healthy(server):
    status, body = _call(server, "/healthz")
    assert status == 200 and body["ready"] is True and body["stale"] is False
    assert _call(server, "/v1/recommend", {"plex_account_id": 100}, token="s3cret")[0] == 200
    status, info = _call(server, "/v1/info", token="s3cret")
    assert info["ready"] is True and "last_build_error" in info
