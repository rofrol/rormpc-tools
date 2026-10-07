"""hits --source mine (my charts by listening year, the shuffle's own picks left out) and library."""
import argparse

from rormpc_tools import hits, musicdb


def args(**kw):
    a = argparse.Namespace(genre="", top=None, n=100, sort="plays")
    a.__dict__.update(kw)
    return a


def song(f):
    return {"artist": f.upper(), "title": f, "file": f, "year": 2010, "years": [2010], "mbid": None,
            "artist_mbid": None, "tags": [], "listens": 0, "hidden": False, "liked": f == "b"}


def test_mine_ranks_by_plays_in_the_listening_years_without_the_shuffles_picks(monkeypatch):
    stamps = {"a": ["2016-03-01T10:00:00", "2016-04-01T10:00:00", "2017-01-01T10:00:00"],
              "b": ["2016-05-01T10:00:00", "2016-06-01T10:00:00", "2016-07-01T10:00:00"]}
    monkeypatch.setattr(musicdb, "counted", lambda c, lib, st=None: st.update(stamps))
    monkeypatch.setattr(musicdb, "db", lambda: None)
    pick = musicdb.ts_epoch("2016-07-01T10:00:00") - 10  # the shuffle started b's last play
    monkeypatch.setattr(musicdb, "auto_starts", lambda: {"b": [pick]})
    monkeypatch.setattr(hits, "library_songs", lambda: {"a": song("a"), "b": song("b")})
    a = args()
    rows = hits.mine_rows([2016], a, None)
    assert [(r["file"], r["points"]) for r in rows] == [("a", 2), ("b", 2)]  # b's auto play does not count
    assert a.mine_plays == 4
    rows = hits.mine_rows([], args(), None)  # all years
    assert [(r["file"], r["points"]) for r in rows] == [("a", 3), ("b", 2)]


def test_library_ranks_every_song_by_plays(monkeypatch):
    monkeypatch.setattr(hits, "library_songs", lambda: {"a": song("a"), "b": song("b"), "c": song("c")})
    rows = hits.likes_rows([], args(), None, {"a": 5, "c": 9}, {}, only_liked=False)
    assert [r["file"] for r in rows] == ["c", "a", "b"]
    liked = hits.likes_rows([], args(), None, {"a": 5, "c": 9}, {})
    assert [r["file"] for r in liked] == ["b"]


def test_race_frames_per_year_without_the_shuffles_picks():
    from rormpc_tools import chart
    ev = lambda ts, t: {"source": "spotify", "ts": ts, "artist": "A", "title": t}
    events = [ev("2015-01-01T10:00:00", "x"), ev("2015-02-01T10:00:00", "x"), ev("2015-03-01T10:00:00", "y"),
              ev("2017-05-01T10:00:00", "y")]
    start = musicdb.ts_epoch("2017-05-01T10:00:00") - 30
    race = chart.compute_race(events, auto={musicdb.name_key("A", "y"): [start]})
    assert [f["year"] for f in race["frames"]] == ["2015", "2016", "2017"]  # empty years kept, as gaps
    assert race["frames"][0]["top"][0] == {"name": "A - x", "plays": 2} and race["frames"][0]["thin"]
    assert race["frames"][2]["top"] == [] and race["left_out"] == 1  # 2017's only play was the shuffle's


def test_my_playlists_merges_playlists_and_leaves_out_generated_ones(env):
    m = env()
    m.playlists = {"Road trip": ["a", "b", "b"], "Evening": ["b", "c", "http://radio.example/stream"],
                   "Hits 1980s top100": ["d"], "Skipped": ["e"], "LB Weekly Jams": ["f"], "Folder yt": ["g"],
                   "My playlists all years top100 · by plays": ["h"], "Tag calm": ["c"]}
    files, skipped = hits.my_playlists()
    assert files == {"a": ["Road trip"], "b": ["Evening", "Road trip"], "c": ["Evening", "Tag calm"],
                     "http://radio.example/stream": ["Evening"]}
    assert sorted(skipped) == ["Folder yt", "Hits 1980s top100", "LB Weekly Jams",
                               "My playlists all years top100 · by plays", "Skipped"]
    assert hits.playlists_reason(["A", "B", "C", "D", "E"]) == "on A, B, C +2"


def test_my_playlists_follows_merged_files(env):
    (musicdb.DATA / "aliases.jsonl").write_text('{"old": "old.mp3", "new": "a"}\n')
    env().playlists = {"Mix": ["old.mp3", "a"]}
    assert hits.my_playlists()[0] == {"a": ["Mix"]}  # one entry, under the current path


def test_playlists_source_ranks_owned_playlist_songs_by_plays_with_their_playlists_as_reason(monkeypatch):
    monkeypatch.setattr(hits, "library_songs", lambda: {f: song(f) for f in "abcd"})
    only = {"a": "on Road trip", "c": "on Evening, Road trip", "zz-not-in-library": "on Evening"}
    rows = hits.likes_rows([], args(), None, {"a": 5, "b": 50, "c": 9}, {}, only_liked=False, only=only)
    assert [(r["file"], r["reason"]) for r in rows] == [("c", "on Evening, Road trip"), ("a", "on Road trip")]
    top = hits.likes_rows([], args(top="1-50"), None, {"a": 5, "c": 9}, {}, only_liked=False, only=only)
    assert [r["file"] for r in top] == ["c"]


def test_playlists_note_names_what_was_left_out():
    a = args(playlists_used=2, playlists_skipped=["Hits 1980s top100", "Hits 2000s top100", "Skipped"])
    assert hits.playlists_note(a) == ("rank by your plays among the songs of your 2 playlists; "
                                      "generated ones left out: Hits, Skipped")
    assert hits.playlists_note(args(playlists_used=1)) == "rank by your plays among the songs of your 1 playlist"
