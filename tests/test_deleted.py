"""Deleted songs are never downloaded again: the block list comes from the deletion journal (older records too),
undo and `deletions allow` lift it, and every downloader skips a deleted song with the reason. Offline: temporary
data dir, fake downloads and identification."""
import argparse, json, sys, types

import pytest

from rormpc_tools import deleted, fetch, hits, identity, liveplaylist as lp, mbtag, musicdb, yt_mp3_mb

YT, MBID = "vvvvvvvvvv1", "rec-deleted"
REC = {"id": "20261010-144006-Artist--Song--vvvvvvvvvv1--20090530.mp3",
       "file": "Hits/2000s/Artist--Song--vvvvvvvvvv1--20090530.mp3", "mode": "permanent", "trashed_to": None,
       "history": "delete", "ytid": YT, "mbid": MBID, "artist": "Artist feat. Guest", "title": "Song",
       "queued_at": "2026-10-10T14:40:06", "stickers": {}, "events": [],
       "ops": {"listenbrainz": "done", "youtube": "done", "local": "done"}, "error": None,
       "finished_at": "2026-10-10T14:40:08"}


@pytest.fixture
def journal(env, tmp_path, monkeypatch):
    monkeypatch.setattr(musicdb, "LOCK", tmp_path / "deletions.lock")

    def write(done=(), pending=()):
        musicdb.write_jsonl(musicdb.DONE, list(done))
        musicdb.write_jsonl(musicdb.PENDING, list(pending))
    return write


def test_a_permanent_deletion_blocks_its_video_recording_and_chart_song(journal):
    journal(done=[REC])
    b = deleted.Blocks()
    assert b.video(YT)["id"] == REC["id"]
    assert b.recording(MBID)["id"] == REC["id"]
    assert b.chart_row(None, "Artist", "Song")["id"] == REC["id"]  # main artist: the feat. credit does not matter
    assert b.chart_row(None, "Artist", "Song (Live)") is None  # another version stays wanted
    assert b.video("other") is None and b.recording("other") is None
    assert deleted.chart_key("Artist & X", "Song") == hits.hide_key("Artist & X", "Song")
    assert deleted.reason(b.video(YT)).startswith("deleted on 2026-10-10 (musicdb deletions allow ")


def test_a_trashed_song_blocks_until_undo_removes_its_record(journal):
    journal(pending=[REC | {"mode": "trash", "trashed_to": "/nowhere"}])
    assert deleted.Blocks().video(YT)
    journal()  # what undo leaves: the record is gone
    assert not deleted.Blocks().video(YT)


def test_a_removed_duplicate_blocks_only_its_video(journal):
    journal(done=[REC | {"ops": {"listenbrainz": "kept: another file has the same recording"}}])
    b = deleted.Blocks()
    assert b.video(YT) and not b.recording(MBID) and not b.chart_row(None, "Artist", "Song")


def test_older_records_take_the_video_and_recording_from_the_registry(journal):
    old = {"id": "20261003-205710-Golden Brown.mp3", "file": "Old/Golden Brown.mp3", "queued_at": "2026-10-03T20:57:10"}
    musicdb.write_jsonl(identity.registry_path(), [{"id": "x", "path": None, "paths": [old["file"]], "state": "gone",
                                                    "ytid": "gggggggggg1", "mbid": "rec-old"}])
    identity._cache.clear()
    named = {"id": "n", "file": "a/001--Chan--Title--nnnnnnnnnn1--20200101.mp3", "queued_at": "2026-10-03T12:00:00"}
    journal(done=[old, named])
    b = deleted.Blocks()
    assert b.video("gggggggggg1")["id"] == old["id"] and b.recording("rec-old")["id"] == old["id"]
    assert b.video("nnnnnnnnnn1")["id"] == "n"  # the video id in the file name


def test_allow_and_block_again_through_musicdb_deletions(journal, capsys):
    journal(done=[REC])

    def deletions(action, rid=REC["id"]):
        musicdb.deletions(argparse.Namespace(retry=False, json=False, all=False, action=action, id=rid))
    deletions("allow")
    assert not deleted.Blocks().video(YT)
    musicdb.deletions(argparse.Namespace(retry=False, json=True, all=True, action=None, id=None))
    [row] = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert row["download"] == {"state": "allowed", "ytid": YT, "mbid": MBID, "chart_key": "artist|song"}
    deletions("block")
    assert deleted.Blocks().video(YT)
    with pytest.raises(SystemExit, match="no deletion with id"):
        deletions("allow", "nope")
    assert [e["action"] for e in musicdb.jsonl(deleted.allowed_path())] == ["allow", "block"]


# ------------------------------------------------------------------ hits fetch

@pytest.fixture
def q(journal, tmp_path, monkeypatch):
    state = tmp_path / "state"
    for name, value in {"STATE_DIR": state, "QUEUE": state / "queue.json", "STAGING": state / "staging",
                        "LOCK": state / "queue.lock", "MUSIC": tmp_path / "music"}.items():
        monkeypatch.setattr(fetch, name, value)
    monkeypatch.setattr(fetch.subprocess, "run", lambda *a, **k: None)
    journal(done=[REC])
    return tmp_path


def test_fetch_add_leaves_deleted_chart_songs_out(q, capsys):
    rows = [{"artist": "Artist", "title": "Song", "year": 2007, "mbid": MBID, "file": None},
            {"artist": "Other", "title": "Hit", "year": 2007, "mbid": "rec-2", "file": None}]
    (q / "current.json").write_text(json.dumps({"rows": rows}))
    fetch.add(argparse.Namespace(source=str(q / "current.json"), rank=None, first=None))
    assert [it["title"] for it in fetch.load()["items"]] == ["Hit"]
    assert "1 deleted songs left out" in capsys.readouterr().out


def test_a_queued_deleted_song_is_blocked_before_any_search(q, monkeypatch):
    monkeypatch.setattr(fetch, "candidates", lambda item: pytest.fail("no YouTube search for a deleted song"))
    item = {"key": "k", "artist": "Artist", "title": "Song", "year": 2007, "mbid": None, "state": "queued"}
    with fetch.locked():
        fetch.save({"version": 1, "items": [item]})
    fields = fetch.fetch_one(item)
    assert fields["state"] == "blocked" and fields["deleted"]["id"] == REC["id"]
    fetch.update("k", **fields)
    fetch.decide(argparse.Namespace(keys=["k"]), "retry")  # after `deletions allow` the retry fetches it
    assert fetch.load()["items"][0]["state"] == "queued"


def test_fetch_skips_deleted_videos_and_reviews_a_deleted_recording(q, monkeypatch):
    found = [(3, {"id": YT}, "deleted upload"), (2, {"id": "fresh000001"}, "another upload")]
    monkeypatch.setattr(fetch, "candidates", lambda item: (found, 200, []))
    got = []
    monkeypatch.setattr(fetch.yt_mp3_mb, "download", lambda urls, extra: got.append(urls) or [])
    item = {"key": "k", "artist": "Other", "title": "Hit", "year": 2007, "mbid": "rec-2", "state": "queued"}
    with fetch.locked():
        fetch.save({"version": 1, "items": [item]})
    fetch.fetch_one(item)
    assert got == [["https://www.youtube.com/watch?v=fresh000001"]]
    state, why = fetch.verify(item, {"mbid": MBID, "artist": "Artist", "title": "Song"}, deleted.Blocks())
    assert state == "review" and why.startswith("deleted before: Artist - Song, deleted on 2026-10-10")
    found[1:] = []
    fields = fetch.fetch_one(item)
    assert fields["state"] == "failed" and "1 of them deleted from the library" in fields["error"]


def test_hits_marks_a_missing_deleted_chart_song(journal, monkeypatch, tmp_path, capsys):
    journal(done=[REC])
    monkeypatch.setattr(hits, "library_songs", lambda: {})
    monkeypatch.setattr(hits, "entries", lambda years: {
        "s": {"artist": "Artist", "title": "Song", "year": 2007, "years": [2007], "peak": 90, "points": 90,
              "best": 11, "first": "2006"}})
    monkeypatch.setattr(hits, "mb_song", lambda *a: {"mbid": MBID, "tags": []})
    monkeypatch.setattr(hits, "lb_popularity", lambda mbids, cached_only=False: {})
    out = tmp_path / "current.json"
    monkeypatch.setattr(sys, "argv", ["hits", "--years", "2007", "--json", str(out)])
    hits.main()
    assert "⌫" in capsys.readouterr().out
    [row] = json.loads(out.read_text())["rows"]
    assert row["file"] is None and row["deleted"] == {"id": REC["id"], "deleted_at": REC["queued_at"],
                                                      "file": REC["file"]}


# ------------------------------------------------------------------ yt-mp3-mb, liveplaylist

@pytest.fixture
def ytmb(journal, tmp_path, monkeypatch):
    monkeypatch.setattr(yt_mp3_mb, "MUSIC", tmp_path / "music")
    monkeypatch.setattr(yt_mp3_mb, "LOG", tmp_path / "log.jsonl")
    monkeypatch.setattr(yt_mp3_mb, "replaygain", lambda p: None)
    calls = types.SimpleNamespace(skip=None, mbid={})

    def download(urls, extra, skip_ids=()):
        calls.skip = set(skip_ids)
        infos = []
        for yid in {u.rsplit("=", 1)[1] for u in urls} - set(skip_ids):
            f = tmp_path / f"{yid}.mp3"
            f.write_bytes(b"mp3")
            infos.append({"filepath": str(f), "id": yid, "title": "X - Y", "channel": "X", "duration": 200})
        return infos
    monkeypatch.setattr(yt_mp3_mb, "download", download)
    monkeypatch.setattr(mbtag, "collect", lambda path, yid, *a: {"ytid": yid, "provided": None, "channel": "X",
                                                                 "yt_title": "X - Y", "cands": [("A", "T")]})
    monkeypatch.setattr(mbtag, "resolve", lambda d: {"status": "auto", "artist": "A", "title": "T",
                                                      "mbid": calls.mbid.get(d["ytid"], "rec-ok"), "artist_mbids": [],
                                                      "score": 1, "method": "mb-url", "alternatives": []})
    monkeypatch.setattr(mbtag, "write_tags", lambda *a, **k: None)
    monkeypatch.setattr(mbtag, "cover", lambda *a: None)
    journal(done=[REC])
    return calls


def url(yid):
    return f"https://www.youtube.com/watch?v={yid}"


def test_batch_never_downloads_a_deleted_video(ytmb, tmp_path):
    report = yt_mp3_mb.batch([url(YT), url("fresh000001")], "Out", known=({tmp_path / "music/Out"}, {YT, "fresh000001"}))
    assert YT in ytmb.skip and [f["ytid"] for f in report["files"]] == ["fresh000001"]
    assert [b["ytid"] for b in report["blocked"]] == [YT] and report["blocked"][0]["deleted"]["id"] == REC["id"]
    report = yt_mp3_mb.batch([url(YT)], "Out", known=({tmp_path / "music/Out"}, {YT}), allow_deleted=True)
    assert YT not in ytmb.skip and [f["ytid"] for f in report["files"]] == [YT]


def test_batch_drops_a_download_identified_as_a_deleted_recording(ytmb, tmp_path):
    ytmb.mbid["other000001"] = MBID
    report = yt_mp3_mb.batch([url("other000001")], "Out", known=({tmp_path / "music/Out"}, {"other000001"}))
    assert report["files"] == [] and report["blocked"][0]["ytid"] == "other000001"
    assert not (tmp_path / "other000001.mp3").exists() and not (tmp_path / "music/Out").exists()


def test_liveplaylist_blocks_a_deleted_video_without_downloading(journal, tmp_path, monkeypatch):
    journal(done=[REC])
    monkeypatch.setattr(lp, "in_library", lambda yid: None)
    monkeypatch.setattr(yt_mp3_mb, "batch", lambda *a, **k: pytest.fail("a deleted video is not downloaded"))
    fields = lp.fetch_item({"id": "yt-x", "dir": "Live"}, {"ytid": YT})
    assert fields["job"] == "blocked" and fields["error"].startswith("deleted on 2026-10-10")


def test_liveplaylist_add_marks_a_deleted_video_for_the_first_review(journal, tmp_path, monkeypatch, capsys):
    journal(done=[REC])
    for name, value in {"MUSIC": tmp_path / "music", "PLAYLISTS": tmp_path / "playlists",
                        "DATA": tmp_path / "music-data" / "liveplaylists", "CACHE": tmp_path / "cache"}.items():
        monkeypatch.setattr(lp, name, value)
    monkeypatch.setattr(lp, "listing", lambda url: {"title": "T", "entries": [{"id": YT, "title": "Song"},
                                                                             {"id": "other000001", "title": "Other"}]})
    with pytest.raises(SystemExit):
        lp.main(["add", "https://www.youtube.com/playlist?list=PLtest0000000000000001", "--json"])
    items = lp.load("yt-PLtest0000000000000001")["items"]
    assert items[YT]["deleted"]["id"] == REC["id"] and items["other000001"]["deleted"] is None
