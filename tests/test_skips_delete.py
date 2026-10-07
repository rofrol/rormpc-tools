"""Skips from the scrobbler's log into the "Skipped" playlist, and delete / undo through the deletion journal."""
import argparse, io, json, urllib.error, urllib.request

import pytest

from rormpc_tools import musicdb
from conftest import FakeMPD

YT = "dQw4w9WgXcQ"
RICK = f"yt/001--Rick_Astley--{YT}--20091025.mp3"
AHA = "cd/02 Take On Me.flac"
SONGS = [{"file": RICK, "artist": "Rick Astley", "title": "Never Gonna Give You Up", "duration": "213",
          "musicbrainz_trackid": "mb-rick"},
         {"file": AHA, "artist": "a-ha", "title": "Take On Me", "duration": "225", "musicbrainz_trackid": "mb-aha"}]


def at(ts):
    return musicdb.epoch_of(ts)


def test_skips_since_the_last_play_fill_the_skipped_playlist(env, monkeypatch):
    m = env(SONGS)
    monkeypatch.setattr(musicdb, "push_feedback", lambda c, scores: 0)
    musicdb.LISTENS_LOG.write_text("".join(json.dumps({"ts": at(ts), "file": f}) + "\n" for ts, f in [
        ("2026-09-20T10:00:00", RICK), ("2026-09-25T10:00:00", AHA)]))
    musicdb.SKIPS_LOG.write_text("".join(json.dumps({"ts": at(ts), "file": f, "position_s": 5, "duration_s": 200,
                                                     "run_s": 5}) + "\n" for ts, f in [
        ("2026-09-21T10:00:00", RICK), ("2026-09-22T10:00:00", RICK),  # two since its last play: listed
        ("2026-09-24T10:00:00", AHA), ("2026-09-24T11:00:00", AHA),  # played again after: forgiven
        ("2026-09-26T10:00:00", AHA)]))  # one since its last play: not yet
    musicdb.import_local(None)
    musicdb.import_skips(None)
    musicdb.import_skips(None)  # the hourly re-import adds nothing
    assert musicdb.db().execute("SELECT count(*) FROM skips").fetchone()[0] == 5
    musicdb.sync(None)
    assert (musicdb.PLAYLISTS / "Skipped.m3u").read_text() == RICK + "\n"
    assert m.stickers[RICK]["skips"].strip() == "2" and m.stickers[AHA]["skips"].strip() == "1"
    musicdb.LISTENS_LOG.write_text(musicdb.LISTENS_LOG.read_text()
                                   + json.dumps({"ts": at("2026-09-27T10:00:00"), "file": RICK}) + "\n")
    musicdb.import_local(None)
    musicdb.sync(None)
    assert (musicdb.PLAYLISTS / "Skipped.m3u").read_text() == ""  # a play clears the review list


class PlayerMPD(FakeMPD):
    def __init__(self, songs):
        super().__init__(songs)
        self.playing = None

    def currentsong(self):
        return {"file": self.playing} if self.playing else {}

    def next(self):
        self.playing = None


@pytest.fixture
def lib(env, monkeypatch, tmp_path):
    """A music dir with RICK, a Trash in a throwaway home, no mpc and no ListenBrainz."""
    m = PlayerMPD(SONGS)
    monkeypatch.setattr(musicdb, "mpd", lambda: m)
    music = tmp_path / "music"
    (music / RICK).parent.mkdir(parents=True)
    (music / RICK).write_bytes(b"ID3 not really")
    (tmp_path / "home" / ".Trash").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(musicdb, "MUSIC", music)
    monkeypatch.setattr(musicdb, "LOCK", tmp_path / "deletions.lock")
    monkeypatch.setattr(musicdb, "export", lambda a: None)
    monkeypatch.setattr(musicdb.subprocess, "run", lambda cmd, **k: None)  # mpc update
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "test-token")
    m.stickers[RICK] = {"like": "2", "plays": "  1"}
    musicdb.add_events([("local", "2026-09-26T10:00:00", YT, "mb-rick", None, None, None, None,
                         json.dumps({"file": RICK})),
                        ("lb", "2026-09-26T10:00:00", None, "mb-rick", None, "Rick Astley",
                         "Never Gonna Give You Up", None, json.dumps({"msid": "msid-1"}))])
    return music, m


def delete(**k):
    musicdb.delete(argparse.Namespace(**{"files": [RICK], "preview": False, "youtube": False, "permanent": False,
                                         "listenbrainz": False} | k))


def test_trash_keeps_history_and_undo_restores_file_and_stickers(lib, monkeypatch):
    music, m = lib
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("history is kept: no ListenBrainz"))
    m.playing = RICK
    delete()
    assert m.playing is None and not (music / RICK).exists()
    [rec] = musicdb.jsonl(musicdb.PENDING)
    assert (rec["file"], rec["mode"], rec["history"], rec["stickers"]) == (RICK, "trash", "keep", m.stickers[RICK])
    assert rec["trashed_to"].startswith(str(music.parent / "home" / ".Trash"))
    assert len(rec["events"]) == 2 and musicdb.db().execute("SELECT count(*) FROM events").fetchone()[0] == 2
    m.stickers.pop(RICK)  # MPD dropped them with the file
    musicdb.undo(argparse.Namespace(id=None))
    assert (music / RICK).read_bytes() == b"ID3 not really"
    assert m.stickers[RICK] == {"like": "2", "plays": "  1"}
    assert musicdb.jsonl(musicdb.PENDING) == []
    with pytest.raises(SystemExit, match="Nothing to undo"):
        musicdb.undo(argparse.Namespace(id=None))


def test_history_delete_failure_is_journaled_and_retried(lib, monkeypatch):
    music, m = lib
    sent = []

    def down(req, timeout=None):
        raise urllib.error.URLError("ListenBrainz is down")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(SystemExit, match="failed, retried hourly"):
        delete(listenbrainz=True)
    [rec] = musicdb.jsonl(musicdb.PENDING)
    assert rec["ops"]["listenbrainz"].startswith("failed") and rec["ops"]["local"] == "done"
    assert rec["error"] and musicdb.db().execute("SELECT count(*) FROM events").fetchone()[0] == 0
    musicdb.add_events([tuple(e[k] for k in musicdb.EVENT_COLS) for e in rec["events"]])
    assert musicdb.db().execute("SELECT count(*) FROM events").fetchone()[0] == 0  # tombstones: re-imports skip

    def up(req, timeout=None):
        sent.append((req.full_url, json.loads(req.data)))
        return io.BytesIO(b"{}")
    monkeypatch.setattr(urllib.request, "urlopen", up)
    monkeypatch.setattr(musicdb, "notify", lambda *a: None)
    musicdb.deletions(argparse.Namespace(retry=True, json=False))
    assert sent == [("https://api.listenbrainz.org/1/delete-listen",
                     {"listened_at": int(at("2026-09-26T10:00:00")), "recording_msid": "msid-1"})]
    [rec] = musicdb.jsonl(musicdb.PENDING)  # trashed: stays restorable
    assert rec["ops"]["listenbrainz"] == "done" and rec["error"] is None and rec["lb_deleted"]
    musicdb.deletions(argparse.Namespace(retry=True, json=False))
    assert len(sent) == 1  # nothing failed any more: no second delete
    musicdb.undo(argparse.Namespace(id=rec["id"]))
    assert (music / RICK).exists() and musicdb.jsonl(musicdb.PENDING) == []
    assert musicdb.db().execute("SELECT count(*) FROM events").fetchone()[0] == 0  # deleted listens stay deleted


def test_permanent_delete_goes_to_done(lib, monkeypatch):
    music, m = lib
    delete(permanent=True)
    assert not (music / RICK).exists() and musicdb.jsonl(musicdb.PENDING) == []
    [rec] = musicdb.jsonl(musicdb.DONE)
    assert rec["mode"] == "permanent" and rec["trashed_to"] is None and rec["finished_at"]
    with pytest.raises(SystemExit, match="Nothing to undo"):
        musicdb.undo(argparse.Namespace(id=None))
