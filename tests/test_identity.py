"""`ENGINE_IDENTITY_GROUPS` — several Plex accounts pooled as one household. Synthetic, no network."""

import time

import pytest

from engine import build, identity, service
from harness import config, db, engagement

OWNER, ADULTS, KIDS, FRIEND = 895220, 856697834, 856698746, 42
GROUPS = f"{OWNER}:{ADULTS},{KIDS}"


class TestParsingTheMap:
    def test_members_point_at_their_canonical_account(self):
        assert identity.parse(GROUPS) == {ADULTS: OWNER, KIDS: OWNER}

    def test_several_groups_and_stray_whitespace(self):
        assert identity.parse(" 1 : 2 , 3 ; 10:11 ;") == {2: 1, 3: 1, 11: 10}

    @pytest.mark.parametrize("raw", [None, "", "  ", ";"])
    def test_unset_or_empty_pools_nobody(self, raw):
        assert identity.parse(raw) == {}

    @pytest.mark.parametrize(
        ("raw", "says"),
        [
            ("1,2,3", "has no ':'"),
            ("1:two", "not a Plex account id"),
            ("one:2", "not a Plex account id"),
            ("-1:2", "not a Plex account id"),
            ("0:5", "not a Plex account id"),
            ("+5:6", "not a Plex account id"),
            ("1_0:2", "not a Plex account id"),  # int() reads this as 10
            ("٣:4", "not a Plex account id"),  # and this as 3
            ("1.0:2", "not a Plex account id"),
            ("1:2,,3", "not a Plex account id"),
            ("1:2,", "not a Plex account id"),
            ("1:", "names no members"),
            ("1:1", "its own member"),
            ("1:2,2", "appears twice"),
            ("1:2;3:2", "appears twice"),
            ("1:2;1:3", "canonical id of two groups"),
            ("1:2;2:3", "canonical id of one group and a member of another"),
        ],
    )
    def test_a_malformed_map_is_refused_and_says_which_part(self, raw, says):
        with pytest.raises(ValueError, match=says):
            identity.parse(raw)

    def test_one_spelling_per_map(self):
        assert identity.normalised(identity.parse("10:12,11; 1:3,2")) == "1:2,3;10:11,12"
        assert identity.normalised({}) == ""

    def test_the_canonical_is_in_its_own_group_and_a_stranger_is_in_none(self, monkeypatch):
        monkeypatch.setenv(identity.ENV, GROUPS)

        assert [identity.group_of(i) for i in (OWNER, ADULTS, KIDS, FRIEND)] == [OWNER, OWNER, OWNER, None]
        assert [identity.canonical(i) for i in (OWNER, KIDS, FRIEND)] == [OWNER, OWNER, FRIEND]


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "harness.db")
    con = db.connect()
    con.execute("INSERT INTO titles VALUES (500, 'show', 'Bluey', 2018, 5000, 'tautulli')")
    con.execute("INSERT INTO titles VALUES (600, 'movie', 'Heat', 1995, 6000, 'tautulli')")
    for user in (OWNER, ADULTS, KIDS, FRIEND):
        con.execute("INSERT INTO users (user_id, username) VALUES (?, ?)", (user, f"u{user}"))
    yield con
    con.close()


def _episode(con, user: int, episode_key: int, when: int = 1_700_000_000) -> None:
    con.execute(
        "INSERT INTO plays (user_id, date, media_type, rating_key, grandparent_rating_key, percent_complete, "
        "watched_status, player, platform, product) VALUES (?, ?, 'episode', ?, 500, 100, 1, 'BRAVIA', 'Android', 'Plex')",
        (user, when, episode_key),
    )


class TestPoolingEngagements:
    def test_episodes_on_different_profiles_add_up_to_one_persons_show(self, con):
        """The point of pooling. One episode each on three accounts is nobody's positive on its own
        (a show needs three), and exactly one household's positive together."""
        for user, key in ((OWNER, 501), (ADULTS, 502), (KIDS, 503)):
            _episode(con, user, key)

        engagement.run(con, identity.parse(GROUPS))

        rows = con.execute("SELECT user_id, distinct_episodes, label FROM engagements").fetchall()
        assert [tuple(r) for r in rows] == [(OWNER, 3, "positive")]

    def test_the_same_episode_on_two_profiles_is_still_one_episode(self, con):
        for user in (ADULTS, KIDS):
            _episode(con, user, 501)

        engagement.run(con, identity.parse(GROUPS))

        assert con.execute("SELECT distinct_episodes FROM engagements").fetchone()[0] == 1

    def test_someone_outside_the_group_is_left_alone(self, con):
        _episode(con, FRIEND, 501)
        _episode(con, KIDS, 502)

        engagement.run(con, identity.parse(GROUPS))

        assert {r[0] for r in con.execute("SELECT user_id FROM engagements")} == {FRIEND, OWNER}

    def test_no_map_is_exactly_what_it_was(self, con):
        for user, key in ((OWNER, 501), (ADULTS, 502), (KIDS, 503)):
            _episode(con, user, key)

        engagement.run(con)

        assert {r[0] for r in con.execute("SELECT user_id FROM engagements")} == {OWNER, ADULTS, KIDS}

    def test_the_plays_themselves_keep_the_account_that_made_them(self, con):
        """Pooling is a view over plays, not a rewrite of them — so removing the map regroups on the
        next build, and "who actually watched this" stays answerable."""
        _episode(con, KIDS, 501)

        engagement.run(con, identity.parse(GROUPS))
        engagement.run(con)

        assert [r[0] for r in con.execute("SELECT user_id FROM plays")] == [KIDS]
        assert [r[0] for r in con.execute("SELECT user_id FROM engagements")] == [KIDS]

    def test_a_members_own_requests_seed_the_household(self):
        requests = {KIDS: [((1, "movie"), 5)], OWNER: [((2, "movie"), 6)], FRIEND: [((3, "show"), 7)]}

        pooled = build.pooled_requests(requests, identity.parse(GROUPS))

        assert sorted(pooled[OWNER]) == [((1, "movie"), 5), ((2, "movie"), 6)]
        assert pooled[FRIEND] == [((3, "show"), 7)]
        assert KIDS not in pooled


class TestWhoGetsAList:
    def test_a_pooled_member_is_not_ranked_as_a_person_of_its_own(self, con, monkeypatch):
        """It would get a popularity list for "no history", and the cross-user state would count one
        household as three people."""
        monkeypatch.setenv(identity.ENV, GROUPS)
        con.executescript(build.SCHEMA)
        from harness import catalogue

        con.executescript(catalogue.SCHEMA)
        engagement.run(con, identity.members())

        assert set(build.contexts_now(con, {}, "library")) == {OWNER, FRIEND}

    def test_a_pooled_member_gets_no_household_row_of_its_own(self, con, monkeypatch):
        monkeypatch.setenv(identity.ENV, GROUPS)
        con.executescript(build.HOUSEHOLD_SCHEMA)
        engagement.run(con, identity.members())

        assert set(build.write_households(con, {}, int(time.time()))) == {OWNER, FRIEND}


class TestIdsTautulliHasNeverReported:
    """One mistyped digit in the CANONICAL id is not an error anywhere downstream — it is a household
    with no lists, because the canonical is never ranked and its members are skipped for being pooled."""

    def test_a_build_refuses_a_canonical_tautulli_does_not_know(self, con, monkeypatch):
        monkeypatch.setenv(identity.ENV, f"{OWNER + 1}:{ADULTS},{KIDS}")

        with pytest.raises(ValueError, match=f"canonical account {OWNER + 1} is not a user Tautulli knows"):
            build.check_identity(con)

    def test_a_member_nobody_has_watched_on_yet_is_only_worth_a_line(self, con, monkeypatch, capsys):
        monkeypatch.setenv(identity.ENV, f"{OWNER}:{ADULTS},999")

        build.check_identity(con)

        assert "member 999 has no plays yet" in capsys.readouterr().out

    def test_no_map_checks_nothing(self, con, monkeypatch):
        monkeypatch.delenv(identity.ENV, raising=False)

        build.check_identity(con)

    def test_the_check_command_tells_the_two_apart(self, con, monkeypatch):
        monkeypatch.setenv(identity.ENV, f"{OWNER + 1}:999")

        text, warnings = identity.check(con)

        assert text == f"group {OWNER + 1}: 999"
        assert [w.split(" is not in")[0] for w in warnings] == [f"{OWNER + 1} (canonical)", "999 (member)"]
        assert "REFUSES" in warnings[0] and "normal" in warnings[1]


class TestPooledHouseholdCounts:
    def test_the_households_counts_are_the_profiles_added_together(self, con, monkeypatch):
        """Children's titles watched on the Kids profile and grown-up ones on the Adults profile are
        ONE household's 12 months — which is why the counts cannot tell the profiles apart."""
        from types import SimpleNamespace

        monkeypatch.setenv(identity.ENV, GROUPS)
        con.executescript(build.HOUSEHOLD_SCHEMA)
        con.executescript(engagement.SCHEMA)
        now = int(time.time())
        items = {}
        for n in range(12):
            kids_title = n < 6
            items[(n, "movie")] = SimpleNamespace(certification="TV-Y" if kids_title else "R", genres=[])
            con.execute(
                "INSERT INTO engagements VALUES (?, ?, 'movie', ?, ?, 1, 0, 100, 1.0, 1.0, 'positive')",
                (OWNER, n, now, now),
            )

        build.write_households(con, items, now)

        row = con.execute("SELECT * FROM households WHERE plex_id = ?", (OWNER,)).fetchone()
        assert (row["label"], row["kids_titles"], row["window_titles"]) == ("family", 6, 12)


def test_two_profiles_asking_for_one_title_is_one_seed_at_its_earliest():
    requests = {KIDS: [((1, "show"), 50)], ADULTS: [((1, "show"), 20)], OWNER: [((1, "show"), 90), ((2, "movie"), 5)]}

    pooled = build.pooled_requests(requests, identity.parse(GROUPS))

    assert sorted(pooled[OWNER]) == [((1, "show"), 20), ((2, "movie"), 5)]
