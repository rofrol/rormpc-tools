"""musicdb restore: a deleted song back with its file, identity, stickers, history, ListenBrainz listens and YouTube
playlist entries; a dry run changes nothing, a rerun continues, a listen is never submitted twice on its own.
Offline: temporary data dir, fake MPD, fake download, ListenBrainz and YouTube mocked."""
import argparse, io, json, urllib.request

import pytest

from rormpc_tools import deleted, identity, musicdb, restore, yt_playlist
from test_skips_delete import RICK, YT, PlayerMPD, SONGS, at

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def gone(env, monkeypatch, tmp_path):
    """RICK deleted permanently with its history: 1 local play, 1 ListenBrainz listen (deleted there), 1 YouTube
    playlist. Returns (music dir, fake MPD, state): state["listed"] is what ListenBrainz lists per second,
    state["sent"] the submissions, state["yt"] the YouTube calls."""
    m = PlayerMPD(SONGS)
    monkeypatch.setattr(musicdb, "mpd", lambda: m)
    music = tmp_path / "music"
    (music / RICK).parent.mkdir(parents=True)
    (music / RICK).write_bytes(b"ID3 original")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(musicdb, "MUSIC", music)
    monkeypatch.setattr(musicdb, "LOCK", tmp_path / "deletions.lock")
    monkeypatch.setattr(musicdb, "export", lambda a: None)
    monkeypatch.setattr(musicdb.subprocess, "run", lambda cmd, **k: None)  # mpc update
    monkeypatch.setattr(musicdb, "youtube_remove", lambda ytid: ["PL1"])
    monkeypatch.setattr(restore.settings, "XDG_CACHE", tmp_path / "cache")
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "test-token")
    musicdb.write_jsonl(identity.registry_path(), [{"id": SID, "path": RICK, "paths": [RICK], "state": "live",
                                                    "ytid": YT, "mbid": "mb-rick", "md5": "old-md5"}])
    identity._cache.clear()
    m.stickers[RICK] = {"like": "2", "plays": "    1", "playCount": "1"}
    musicdb.add_events([("local", "2026-09-26T10:00:00", YT, "mb-rick", None, None, None, None,
                         json.dumps({"file": RICK})),
                        ("lb", "2026-09-26T10:00:00", None, "mb-rick", None, "Rick Astley",
                         "Never Gonna Give You Up", 213000, json.dumps({"msid": "msid-old"}))])
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(b"{}"))  # the delete
    musicdb.delete(argparse.Namespace(files=[RICK], preview=False, youtube=False, permanent=True, listenbrainz=True))
    assert not (music / RICK).exists() and musicdb.db().execute("SELECT count(*) FROM events").fetchone()[0] == 0
    m.stickers.pop(RICK)
    state = {"listed": {}, "sent": [], "yt": [], "fail_submit": False}

    def urlopen(req, timeout=None):
        if state["fail_submit"]:
            raise TimeoutError("no answer")
        state["sent"].append(json.loads(req.data))
        return io.BytesIO(b'{"status": "ok"}')
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(restore, "lb_listens_at", lambda epoch: state["listed"].get(epoch, []))

    def yt(*args):
        state["yt"].append(args)
        return {"video": args[1], "items": {args[3]: ["item-1"]}, "added": [args[3]], "present": []}
    monkeypatch.setattr(restore, "yt", yt)

    def download(r):  # yt-mp3-mb's result: a tagged file named after the video in the staging dir
        d = restore.staging(r["id"])
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"Rick_Astley--Never_Gonna_Give_You_Up--{YT}--20091025.mp3"
        f.write_bytes(b"ID3 downloaded again")
        return f
    monkeypatch.setattr(restore, "download", download)
    monkeypatch.setattr(restore, "length_of", lambda p: 212.4)
    monkeypatch.setattr(restore, "recording_of", lambda p: "mb-rick")
    monkeypatch.setattr(identity, "write_tags", lambda rel, sid, yt: state.setdefault("tagged", []).append((sid, yt)))
    from rormpc_tools import dedupe
    monkeypatch.setattr(dedupe, "audio_hash", lambda p: "new-md5")
    [rec] = musicdb.jsonl(musicdb.DONE)
    return music, m, state, rec["id"]


def restore_cmd(capsys, rid, *flags):
    capsys.readouterr()
    restore.main([rid, "--json", *flags])
    out = capsys.readouterr().out.splitlines()
    return json.loads(out[0]), out[-1]


def steps(plan, name):
    return [s["state"] for s in plan["steps"] if s["step"] == name]


OLD = {"listened_at": int(at("2026-09-26T10:00:00")), "recording_msid": "msid-old",
       "track_metadata": {"artist_name": "Rick Astley", "track_name": "Never Gonna Give You Up"}}
NEW = OLD | {"recording_msid": "msid-new"}


def test_the_dry_run_plans_everything_and_changes_nothing(gone, capsys):
    music, m, state, rid = gone
    before = (musicdb.jsonl(musicdb.DONE), musicdb.jsonl(identity.registry_path()))
    state["listed"][OLD["listened_at"]] = [OLD]  # ListenBrainz has not processed the deletion yet
    plan, last = restore_cmd(capsys, rid)
    assert plan["version"] == restore.PLAN_VERSION and plan["can_restore"] and not plan["restored"]
    assert steps(plan, "file") == ["will"] and f"youtu.be/{YT}" in plan["steps"][1]["text"]
    assert "213.0 s ± 2 s" in plan["steps"][1]["text"] and "recording mb-rick" in plan["steps"][1]["text"]
    assert steps(plan, "listenbrainz") == ["waiting"] and steps(plan, "youtube") == ["will"]
    assert steps(plan, "stickers") == ["will"] and steps(plan, "download") == ["will"]
    assert last.endswith("ready (dry run, nothing changed)")
    assert not (music / RICK).exists() and RICK not in m.stickers and state["sent"] == [] and state["yt"] == []
    assert (musicdb.jsonl(musicdb.DONE), musicdb.jsonl(identity.registry_path())) == before
    assert not restore.restored_path().exists() and deleted.Blocks().video(YT)


def test_restore_brings_everything_back_and_submits_a_listen_once(gone, capsys):
    music, m, state, rid = gone
    state["listed"][OLD["listened_at"]] = [OLD]
    plan, last = restore_cmd(capsys, rid, "--yes")
    assert (music / RICK).read_bytes() == b"ID3 downloaded again"
    assert not list(restore.staging(rid).glob("*.mp3"))  # moved, not copied
    assert state["tagged"] == [(SID, YT)]  # the old song id goes back into the file
    row = identity.resolve(RICK)
    assert (row["id"], row["state"], row["path"], row["md5"]) == (SID, "live", RICK, "new-md5")
    assert m.stickers[RICK] == {"like": "2", "plays": "    1", "playCount": "1"}
    c = musicdb.db()
    assert c.execute("SELECT source, ts FROM events").fetchall() == [("local", "2026-09-26T10:00:00")]
    assert c.execute("SELECT count(*) FROM tombstones").fetchone()[0] == 0
    assert state["sent"] == []  # the deleted listen is still listed: never submitted over a pending deletion
    assert steps(plan, "listenbrainz") == ["waiting"] and steps(plan, "youtube") == ["done"]
    assert state["yt"] == [("add", YT, "--playlist", "PL1")]
    assert not deleted.Blocks().video(YT)
    assert "still to do (retried hourly): listenbrainz waiting" in last
    rec = restore.records()[rid]
    assert rec["ops"]["file"] == "done: downloaded again, another encode of the video (audio differs from the deleted file)"
    assert not rec.get("finished_at") and restore.unfinished(rec)

    state["listed"][OLD["listened_at"]] = []  # the deletion went through: submit once, with the original time
    restore.retry()
    [sent] = state["sent"]
    assert sent["listen_type"] == "import"
    assert sent["payload"][0]["listened_at"] == OLD["listened_at"]
    assert sent["payload"][0]["track_metadata"]["additional_info"]["recording_mbid"] == "mb-rick"
    restore.retry()
    assert len(state["sent"]) == 1  # submitted, not listed yet: never sent again on its own

    state["listed"][OLD["listened_at"]] = [NEW]
    restore.retry()
    assert len(state["sent"]) == 1
    assert c.execute("SELECT source, extra FROM events WHERE source = 'lb'").fetchall() == [
        ("lb", json.dumps({"msid": "msid-new"}))]  # the msid a later delete needs
    rec = restore.records()[rid]
    assert rec["finished_at"] and rec["ops"]["listenbrainz"] == "done"
    musicdb.deletions(argparse.Namespace(retry=False, json=True, all=True, action=None, id=None))
    [row] = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert row["restore"] == "restored" and row["download"]["state"] == "allowed"
    plan, last = restore_cmd(capsys, rid, "--yes")  # idempotent
    assert len(state["sent"]) == 1 and len(state["yt"]) == 1 and last == "Restored: Rick Astley - Never Gonna Give You Up"
    assert all(s["state"] in ("done", "skip") for s in plan["steps"])


def test_an_unconfirmed_submission_is_sent_again_only_on_request(gone, capsys):
    _, _, state, rid = gone
    state["fail_submit"] = True
    restore_cmd(capsys, rid, "--yes")
    assert restore.records()[rid]["lb"]["2026-09-26T10:00:00 msid-old"]["state"] == "submitting"
    state["fail_submit"] = False
    restore.retry()
    assert state["sent"] == []
    plan, _ = restore_cmd(capsys, rid)
    assert steps(plan, "listenbrainz") == ["unknown"] and "--lb-resubmit" in plan["steps"][5]["text"]
    restore_cmd(capsys, rid, "--yes", "--lb-resubmit")
    assert len(state["sent"]) == 1


def test_a_download_that_is_not_the_deleted_song_stays_staged(gone, capsys, monkeypatch):
    music, m, state, rid = gone
    monkeypatch.setattr(restore, "length_of", lambda p: 180.0)
    with pytest.raises(SystemExit, match="Not restored: .*length 180.0 s, the deleted song had 213.0 s"):
        restore_cmd(capsys, rid, "--yes")
    assert not (music / RICK).exists() and list(restore.staging(rid).glob("*.mp3"))
    assert RICK not in m.stickers and musicdb.db().execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert deleted.Blocks().video(YT)  # still blocked: nothing was restored
    monkeypatch.setattr(restore, "length_of", lambda p: 213.5)
    monkeypatch.setattr(restore, "recording_of", lambda p: "mb-other")
    with pytest.raises(SystemExit, match="identified as recording mb-other"):
        restore_cmd(capsys, rid, "--yes")
    written = []
    monkeypatch.setattr(restore, "recording_of", lambda p: None)  # the tagger left it for review
    monkeypatch.setattr(restore.mbtag, "write_tags", lambda p, row: written.append(row))
    restore_cmd(capsys, rid, "--yes")
    assert (music / RICK).exists() and [w["mbid"] for w in written] == ["mb-rick"]
    assert "the deleted song's recording, artist and title written" in restore.records()[rid]["ops"]["file"]


def test_another_file_at_the_path_is_never_overwritten(gone, capsys):
    music, _, _, rid = gone
    (music / RICK).write_bytes(b"someone else")
    plan, last = restore_cmd(capsys, rid)
    assert steps(plan, "file") == ["cannot"] and not plan["can_restore"] and "blocked: another file" in last
    with pytest.raises(SystemExit, match="Not restored"):
        restore_cmd(capsys, rid, "--yes")
    assert (music / RICK).read_bytes() == b"someone else"


def test_a_trashed_song_with_a_failed_history_step_comes_back_from_the_trash(gone, capsys, tmp_path):
    music, m, state, rid = gone
    # the same song trashed instead, its ListenBrainz step failed (pending, retried hourly) after nothing was deleted
    rec = musicdb.jsonl(musicdb.DONE)[0]
    trashed = tmp_path / "home" / ".Trash" / "rick.mp3"
    trashed.parent.mkdir(parents=True)
    trashed.write_bytes(b"ID3 from the trash")
    rec |= {"id": "t1", "mode": "trash", "trashed_to": str(trashed), "lb_deleted": [], "youtube_removed": [],
            "ops": {"listenbrainz": "failed: 503", "local": "done"}, "error": "ListenBrainz: 503"}
    musicdb.write_jsonl(musicdb.DONE, [])
    musicdb.write_jsonl(musicdb.PENDING, [rec])
    plan, last = restore_cmd(capsys, "t1", "--yes")
    assert (music / RICK).read_bytes() == b"ID3 from the trash" and last == "Restored: Rick Astley - Never Gonna Give You Up"
    assert musicdb.jsonl(musicdb.PENDING) == []  # the hourly retry can no longer delete its listens
    assert [r["id"] for r in musicdb.jsonl(musicdb.DONE)] == ["t1"]
    assert sorted(musicdb.db().execute("SELECT source FROM events").fetchall()) == [("lb",), ("local",)]
    assert state["sent"] == [] and state["yt"] == []


class FakeYouTube:
    def __init__(self, items):
        self.items, self.inserted = items, []

    def playlistItems(self):
        return self

    def list(self, playlistId, videoId, pageToken=None, **k):
        return types_ns({"items": [{"id": i} for i in self.items.get((playlistId, videoId), [])]})

    def insert(self, part, body):
        s = body["snippet"]
        self.inserted.append((s["playlistId"], s["resourceId"]["videoId"]))
        return types_ns({"id": "new-item"})


def types_ns(result):
    return argparse.Namespace(execute=lambda: result)


def test_yt_playlist_add_skips_playlists_that_hold_the_video():
    yt = FakeYouTube({("PL1", "v"): ["old-item"]})
    out = yt_playlist.add(yt, "v", ["PL1", "PL2"])
    assert yt.inserted == [("PL2", "v")]
    assert out == {"video": "v", "items": {"PL1": ["old-item"], "PL2": ["new-item"]}, "added": ["PL2"], "present": ["PL1"]}
