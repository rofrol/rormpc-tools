"""hits' library index: kept across runs while MPD's database, the play history and the logs it is read through are
unchanged, rebuilt when any of them changes or the file is corrupt or old; likes are read on every run; songs that
tie keep one order whatever order MPD lists them in."""
import json, sys, types

import pytest

from rormpc_tools import banned, hits, musicdb

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


@pytest.mark.parametrize("change", ["mpd", "play", "banned", "deletions", "aliases", "versions", "registry"])
def test_the_index_follows_what_it_is_read_from(idx, monkeypatch, tmp_path, change):
    files(LIBRARY, monkeypatch, tmp_path)
    reads = idx.reads
    if change == "mpd":
        idx.db_update += 1
    elif change == "play":
        play("lib/c.mp3", "2026-02-01T10:00:00")
    elif change == "banned":
        banned.log_path().write_text("")
    elif change == "deletions":
        musicdb.PENDING.parent.mkdir(parents=True, exist_ok=True)
        musicdb.PENDING.write_text("")
    else:
        name = {"aliases": "aliases.jsonl", "versions": "versions.jsonl", "registry": "songs.jsonl"}[change]
        (musicdb.DATA / name).write_text("")
    rows = files(LIBRARY, monkeypatch, tmp_path)
    assert idx.reads > reads
    if change == "play":
        assert rows[0] == "lib/c.mp3"  # the new play counts at once


def test_likes_are_read_on_every_run(idx, monkeypatch, tmp_path):
    assert files(["--set=+likes"], monkeypatch, tmp_path) == []
    reads = idx.reads
    idx.sticker_set("song", "lib/a.mp3", "like", "2")  # a like changes no db_update
    assert files(["--set=+likes"], monkeypatch, tmp_path) == ["lib/a.mp3"]
    assert idx.reads == reads


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
