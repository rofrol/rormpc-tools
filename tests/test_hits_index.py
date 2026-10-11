"""hits' library index: kept across runs while MPD's database and the logs it is read through are unchanged, rebuilt
when any of them changes or the file is corrupt or old; a new play recounts the plays without reading MPD or matching
known plays again; likes are read on every run; the chart matches are kept per list of years; a cached run gives
the same result as an uncached one; songs that tie keep one order whatever order MPD lists them in."""
import datetime as dt, json, sys, types

import pytest

from rormpc_tools import banned, hits, mbtag, musicdb

SONGS = [{"file": f"lib/{k}.mp3", "artist": f"Artist {k.upper()}", "title": f"Song {k}", "date": "1987"}
         for k in "abc"]
TWINS = [{"file": "x/twin.mp3", "artist": "Twin", "title": "Same", "date": "1987"},
         {"file": "y/twin.mp3", "artist": "Twin", "title": "Same", "date": "1987"}]
LIBRARY = ["--rank", "plays", "--years-of", "release"]  # every library song by my plays


@pytest.fixture
def idx(env, tmp_path, monkeypatch):
    """A FakeMPD that counts its library reads (hits.library_songs' own client is the same fake), the index on,
    the logs in the test's data dir."""
    m = env(SONGS + TWINS)
    m.connect = lambda *_: None
    m.reads = 0
    listallinfo = m.listallinfo

    def counted_listallinfo():
        m.reads += 1
        return listallinfo()
    m.listallinfo = counted_listallinfo
    monkeypatch.setitem(sys.modules, "mpd", types.SimpleNamespace(MPDClient=lambda: m))
    monkeypatch.setattr(hits, "index_cache", lambda: tmp_path / "index" / "library-index.json")
    monkeypatch.setattr(hits, "HIDDEN", tmp_path / "music-data" / "hits-hidden.jsonl")
    monkeypatch.setattr(banned, "DATA", musicdb.DATA)
    musicdb.db().close()  # the play DB exists, as it does for every run but a first one ever
    return m


def play(f, ts):
    musicdb.add_events([("local", ts, None, None, None, None, None, None, json.dumps({"file": f}))])


def files(argv, monkeypatch, tmp_path):
    out = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["hits", *argv, "--json", str(out)])
    hits.main()
    return [r["file"] for r in json.loads(out.read_text())["rows"]]


def test_a_second_run_reads_the_index_not_mpd(idx, monkeypatch, tmp_path):
    play("lib/b.mp3", "2026-02-01T10:00:00")
    first = files(LIBRARY, monkeypatch, tmp_path)
    assert first[0] == "lib/b.mp3" and idx.reads > 0
    reads = idx.reads
    assert files(LIBRARY, monkeypatch, tmp_path) == first
    assert idx.reads == reads


@pytest.mark.parametrize("change", ["mpd", "banned", "deletions", "aliases", "versions", "registry"])
def test_the_index_follows_what_it_is_read_from(idx, monkeypatch, tmp_path, change):
    files(LIBRARY, monkeypatch, tmp_path)
    reads = idx.reads
    if change == "mpd":
        idx.db_update += 1
    elif change == "banned":
        banned.log_path().write_text("")
    elif change == "deletions":
        musicdb.PENDING.parent.mkdir(parents=True, exist_ok=True)
        musicdb.PENDING.write_text("")
    else:
        name = {"aliases": "aliases.jsonl", "versions": "versions.jsonl", "registry": "songs.jsonl"}[change]
        (musicdb.DATA / name).write_text("")
    files(LIBRARY, monkeypatch, tmp_path)
    assert idx.reads > reads


def test_a_new_play_counts_at_once_without_reading_mpd_or_matching_known_plays(idx, monkeypatch, tmp_path):
    play("lib/b.mp3", "2026-02-01T10:00:00")
    files(LIBRARY, monkeypatch, tmp_path)
    reads, matched = idx.reads, []
    event_file = musicdb.event_file
    monkeypatch.setattr(musicdb, "event_file", lambda *e: matched.append(e[2:]) or event_file(*e))
    play("lib/c.mp3", "2026-02-02T10:00:00")
    play("lib/c.mp3", "2026-02-03T10:00:00")
    assert files(LIBRARY, monkeypatch, tmp_path)[0] == "lib/c.mp3"  # 2 plays
    assert idx.reads == reads
    assert len(matched) == 1  # c's local plays are one kind of event; b's was matched by the first run
    play("lib/b.mp3", "2026-02-04T10:00:00")
    play("lib/b.mp3", "2026-02-05T10:00:00")
    assert files(LIBRARY, monkeypatch, tmp_path)[0] == "lib/b.mp3"  # 3 plays
    assert idx.reads == reads and len(matched) == 1


def test_a_library_change_matches_the_plays_again(idx, monkeypatch, tmp_path):
    """The event memo holds for one library part: a file renamed in MPD gets its plays through the alias."""
    play("lib/a.mp3", "2026-02-01T10:00:00")
    assert files(LIBRARY, monkeypatch, tmp_path)[0] == "lib/a.mp3"
    idx.songs[0]["file"] = "lib/a2.mp3"
    idx.db_update += 1
    (musicdb.DATA / "aliases.jsonl").write_text(json.dumps({"old": "lib/a.mp3", "new": "lib/a2.mp3"}) + "\n")
    assert files(LIBRARY, monkeypatch, tmp_path)[0] == "lib/a2.mp3"


def test_likes_are_read_on_every_run(idx, monkeypatch, tmp_path):
    assert files(["--set=+likes"], monkeypatch, tmp_path) == []
    reads = idx.reads
    idx.sticker_set("song", "lib/a.mp3", "like", "2")  # a like changes no db_update
    assert files(["--set=+likes"], monkeypatch, tmp_path) == ["lib/a.mp3"]
    assert idx.reads == reads


@pytest.mark.parametrize("damage", ["torn", "old key", "missing part", "wrong types"])
def test_corrupt_play_counts_are_counted_again(idx, monkeypatch, tmp_path, damage):
    play("lib/b.mp3", "2026-02-01T10:00:00")
    first = files(LIBRARY, monkeypatch, tmp_path)
    path = hits.plays_cache()
    saved = json.loads(path.read_text())
    if damage == "torn":
        path.write_text(path.read_text()[:60])
    elif damage == "old key":
        path.write_text(json.dumps(dict(saved, key="counts of another history", plays={})))
    elif damage == "missing part":
        del saved["last"]
        path.write_text(json.dumps(saved))
    else:
        path.write_text(json.dumps(dict(saved, plays=["lib/a.mp3"], memo=[["local", 1], 7])))
    reads = idx.reads
    assert files(LIBRARY, monkeypatch, tmp_path) == first
    assert idx.reads == reads  # the library part still holds
    assert json.loads(path.read_text())["plays"] == {"lib/b.mp3": 1}  # written again, whole


@pytest.mark.parametrize("damage", ["torn", "old key", "missing part"])
def test_a_corrupt_or_old_index_is_rebuilt(idx, monkeypatch, tmp_path, damage):
    first = files(LIBRARY, monkeypatch, tmp_path)
    path = hits.index_cache()
    saved = json.loads(path.read_text())
    if damage == "torn":
        path.write_text(path.read_text()[:100])
    elif damage == "old key":
        path.write_text(json.dumps(dict(saved, key="an index of another version", songs={})))
    else:
        del saved["songs"]
        path.write_text(json.dumps(saved))
    reads = idx.reads
    assert files(LIBRARY, monkeypatch, tmp_path) == first
    assert idx.reads > reads
    assert json.loads(path.read_text())["songs"]  # written again, whole


@pytest.mark.parametrize("argv", [LIBRARY, ["--rank", "none"]], ids=["by plays", "unranked"])
def test_ties_keep_one_order_whatever_order_mpd_lists(idx, monkeypatch, tmp_path, argv):
    first = files(argv, monkeypatch, tmp_path)
    assert files(argv, monkeypatch, tmp_path) == first  # the index's run
    assert first.index("x/twin.mp3") + 1 == first.index("y/twin.mp3")  # tied: by path
    idx.songs.reverse()
    idx.db_update += 1
    assert files(argv, monkeypatch, tmp_path) == first


CHART = [[1, "Song a", "Artist A"], [2, "Gone", "Missing"], [3, "Same", "Twin"], [4, "Song c", "Artist C feat. X"]]


@pytest.fixture
def charts(idx, tmp_path, monkeypatch):
    """Billboard 1987 in a cache of the test's own: chart page and MusicBrainz matches stored (no request),
    ListenBrainz listens fixed; returns the chart page's path."""
    monkeypatch.setattr(hits, "CACHE", tmp_path / "hits")
    monkeypatch.setattr(hits, "_cache_db", None)
    monkeypatch.setattr(hits, "SEED", tmp_path / "no-seed.jsonl.gz")
    monkeypatch.setattr(hits, "lb_popularity", lambda mbids, cached_only=False: {"mb-gone": 5})
    monkeypatch.setattr(mbtag, "http", lambda url, **kw: pytest.fail(f"a request: {url}"))
    (tmp_path / "hits").mkdir()
    for y in range(hits.FIRST_YEAR, dt.date.today().year):  # every other year's page empty
        (tmp_path / "hits" / f"chart-{y}.json").write_text("[]")
    page = tmp_path / "hits" / "chart-1987.json"
    page.write_text(json.dumps(CHART))
    for _, title, artist in CHART:
        hits.cached(f"m{hits.MATCH_VERSION}-{artist}-{title}-1987", lambda t=title: {"mbid": f"mb-{t.lower()}"})
    return page


BILLBOARD = ["1980s", "--set", "+billboard"]
LISTENED = ["--rank", "plays", "--years-of", "listened"]  # my plays by listening year: the play-history split


def result(argv, monkeypatch, tmp_path):
    out = tmp_path / "result.json"
    monkeypatch.setattr(sys, "argv", ["hits", *argv, "--json", str(out)])
    hits.main()
    data = json.loads(out.read_text())
    del data["generated_at"]
    return data


@pytest.mark.parametrize("argv", [BILLBOARD, LIBRARY, LISTENED, ["--set", "+billboard", "--rank", "plays"]],
                         ids=["billboard", "by plays", "listened", "billboard by plays"])
def test_a_cached_run_gives_the_uncached_result(charts, monkeypatch, tmp_path, argv):
    play("lib/c.mp3", "2026-02-01T10:00:00")
    play("x/twin.mp3", "2026-02-02T10:00:00")
    cache = hits.index_cache

    def both():
        monkeypatch.setattr(hits, "index_cache", lambda: None)
        uncached = result(argv, monkeypatch, tmp_path)
        monkeypatch.setattr(hits, "index_cache", cache)
        assert result(argv, monkeypatch, tmp_path) == uncached
        return uncached
    first = both()  # cold
    assert first["rows"]
    assert both() == first  # warm
    play("lib/a.mp3", "2026-03-01T10:00:00")
    assert both() != first  # warm after a new play: the play counts


def test_chart_matches_are_kept_and_follow_what_they_are_read_from(idx, charts, monkeypatch, tmp_path):
    files(BILLBOARD, monkeypatch, tmp_path)
    matched = []
    match = musicdb.match
    monkeypatch.setattr(musicdb, "match", lambda *x, **kw: matched.append(x) or match(*x, **kw))
    first = files(BILLBOARD, monkeypatch, tmp_path)
    assert first[:3] == ["lib/a.mp3", None, "x/twin.mp3"] and not matched
    play("lib/a.mp3", "2026-02-01T10:00:00")  # a play is not in the chart matches' key
    assert files(BILLBOARD, monkeypatch, tmp_path) == first and not matched
    charts.write_text(json.dumps([[1, "Gone", "Missing"], *CHART[2:]]))  # the chart page
    assert files(BILLBOARD, monkeypatch, tmp_path)[:2] == [None, "x/twin.mp3"] and matched
    matched.clear()
    hits.cached("m1-unrelated", lambda: {})  # a MusicBrainz match added
    files(BILLBOARD, monkeypatch, tmp_path)
    assert matched
    matched.clear()
    idx.songs.append({"file": "new/gone.mp3", "artist": "Missing", "title": "Gone", "date": "1987"})
    idx.db_update += 1  # the library
    assert files(BILLBOARD, monkeypatch, tmp_path)[0] == "new/gone.mp3" and matched


def test_corrupt_chart_matches_are_computed_again(charts, monkeypatch, tmp_path):
    first = files(BILLBOARD, monkeypatch, tmp_path)
    (path,) = hits.chart_cache_dir().glob("*.json")
    for damage in ["{", json.dumps({"key": json.loads(path.read_text())["key"], "rows": [[1, 2]]})]:
        path.write_text(damage)
        assert files(BILLBOARD, monkeypatch, tmp_path) == first
        assert len(json.loads(path.read_text())["rows"]) == len(CHART)
