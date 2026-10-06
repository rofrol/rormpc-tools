"""Play counting: every play counted once, on the right file, and the history survives a cache rebuild."""
import json, os, pathlib, time, zoneinfo

import pytest

from rormpc_tools import doctor, musicdb

YT = "dQw4w9WgXcQ"
RICK = f"yt/001--Rick_Astley--{YT}--20091025.mp3"
SONGS = [
    {"file": RICK, "artist": "Rick Astley", "title": "Never Gonna Give You Up", "duration": "213",
     "musicbrainz_trackid": "mb-rick"},
    {"file": "cd/02 Take On Me.flac", "artist": "a-ha", "title": "Take On Me", "duration": "225",
     "musicbrainz_trackid": "mb-aha"},
]


def ev(source, ts, ytid=None, mbid=None, artist=None, title=None, extra=None, ms=None, uri=None):
    return (source, ts, ytid, mbid, uri, artist, title, ms, extra)


def counts(c=None):
    plays, last, *_ = musicdb.counted(c or musicdb.db(), musicdb.library())
    return dict(plays)


def local(ts, file, ytid=None, mbid=None):
    return ev("local", ts, ytid, mbid, extra=json.dumps({"file": file}))


def test_reimport_is_idempotent_and_fills_msid(env):
    env(SONGS)
    row = ev("lb", "2026-09-26T02:16:18", mbid="mb-aha", artist="a-ha", title="Take On Me", ms=225000)
    musicdb.add_events([row])
    musicdb.add_events([row[:8] + (json.dumps({"msid": "m1"}),)])
    musicdb.add_events([row])
    rows = musicdb.db().execute("SELECT extra FROM events").fetchall()
    assert rows == [(json.dumps({"msid": "m1"}),)]


def test_local_listen_and_its_listenbrainz_copy_count_once(env):
    env(SONGS)
    musicdb.add_events([local("2026-09-26T10:00:00", RICK, YT, "mb-rick"),
                        ev("lb", "2026-09-26T10:00:00", mbid="mb-rick", artist="Rick Astley",
                           title="Never Gonna Give You Up", extra=json.dumps({"msid": "m1"}))])
    assert counts() == {RICK: 1}


def test_listenbrainz_only_listen_counts(env):
    env(SONGS)
    musicdb.add_events([ev("lb", "2026-09-26T10:00:00", mbid="mb-aha", artist="a-ha", title="Take On Me")])
    assert counts() == {"cd/02 Take On Me.flac": 1}


def test_tombstoned_events_never_come_back(env):
    env(SONGS)
    row = ev("lb", "2026-09-26T10:00:00", mbid="mb-aha", artist="a-ha", title="Take On Me")
    c = musicdb.db()
    c.execute("INSERT INTO tombstones VALUES (?,?,?,?,?,?,?,?,?,?)",
              tuple("" if v is None else v for v in row) + ("2026-10-01",))
    c.commit()
    musicdb.add_events([row])
    assert counts() == {}


def test_ambiguous_name_is_not_credited_to_either_copy(env):
    env([{"file": "a/Take On Me.mp3", "artist": "a-ha", "title": "Take On Me"},
         {"file": "b/Take On Me (Remastered).mp3", "artist": "a-ha", "title": "Take On Me (Remastered)"}])
    musicdb.add_events([ev("spotify", "2026-09-26T10:00:00", artist="a-ha", title="Take On Me")])
    plays, _, _, unmatched, _ = musicdb.counted(musicdb.db(), musicdb.library())
    assert not plays and sum(unmatched.values()) == 1


def test_name_key_folds_featuring_brackets_case_and_unicode_forms():
    k = musicdb.name_key
    assert k("Felix Jaehn feat. Jasmine Thompson", "Ain't Nobody (Loves Me Better)") == k("Felix Jaehn", "AIN'T NOBODY")
    assert k("Beyoncé", "Halo") == k("Beyoncé", "Halo")  # NFD vs NFC


def test_cache_rebuilt_from_jsonl_counts_the_same(env, tmp_path):
    env(SONGS)
    (musicdb.DATA / ".git").mkdir()  # export writes the JSONL only into a git data repo
    musicdb.add_events([local("2026-09-26T10:00:00", RICK, YT, "mb-rick"),
                        ev("lb", "2026-09-26T10:00:00", mbid="mb-rick", extra=json.dumps({"msid": "m1"})),
                        ev("lb", "2026-09-27T10:00:00", mbid="mb-aha", artist="a-ha", title="Take On Me", ms=225000)])
    before = counts()
    musicdb.export(None)
    musicdb.DB.unlink()
    assert counts() == before == {RICK: 1, "cd/02 Take On Me.flac": 1}


def test_renamed_file_keeps_its_local_plays_through_the_ytid(env):
    """A local listen names the file it was played from; after a rename it must still count via the video id."""
    env([{**SONGS[0], "file": f"Rick Astley - Never Gonna Give You Up--{YT}--20091025.mp3"}])
    musicdb.add_events([local("2026-09-26T10:00:00", RICK, YT, "mb-rick")])
    assert counts() == {f"Rick Astley - Never Gonna Give You Up--{YT}--20091025.mp3": 1}


def test_same_video_in_two_folders_is_reported(env):
    """Two copies of one video: `library()` credits every play to one copy and the other shows 0. Not lost, but
    misleading, so doctor must list it."""
    other = "other/" + RICK.split("/")[1]
    env([{**SONGS[0], "file": RICK}, {**SONGS[0], "file": other}])
    musicdb.add_events([local("2026-09-26T10:00:00", RICK, YT, "mb-rick"),
                        local("2026-09-27T10:00:00", other, YT, "mb-rick")])
    assert sum(counts().values()) == 2  # no play lost
    shared = {d["id"]: d["files"] for d in doctor.check()["shared-ids"]}
    assert shared == {f"yt:{YT}": sorted([RICK, other]), "mb:mb-rick": sorted([RICK, other])}


def test_listenbrainz_listen_reread_with_its_mapping_stays_one_event(env):
    """LB maps a listen to a recording later: the overlap re-read brings it back with an MBID and msid. Seen in
    real data (8 listens stored twice)."""
    env(SONGS)
    bare = ev("lb", "2026-09-26T03:44:03", artist="a-ha", title="Take On Me", ms=225000)
    mapped = ev("lb", "2026-09-26T03:44:03", mbid="mb-aha", artist="a-ha", title="Take On Me",
                extra=json.dumps({"msid": "m1"}))
    musicdb.add_events([bare]); musicdb.add_events([mapped]); musicdb.add_events([bare])
    rows = musicdb.db().execute("SELECT mbid, extra, ms_played FROM events").fetchall()
    assert rows == [("mb-aha", json.dumps({"msid": "m1"}), 225000)]
    assert counts() == {"cd/02 Take On Me.flac": 1}
    assert doctor.check()["lb-duplicates"] == []


def test_tombstoned_listenbrainz_listen_stays_dead_after_its_mapping_changes(env):
    env(SONGS)
    c = musicdb.db()
    c.execute("INSERT INTO tombstones VALUES (?,?,?,?,?,?,?,?,?,?)",
              ("lb", "2026-09-26T10:00:00", "", "", "", "a-ha", "Take On Me", "", "", "2026-10-01"))
    c.commit()
    musicdb.add_events([ev("lb", "2026-09-26T10:00:00", mbid="mb-aha", artist="a-ha", title="Take On Me")])
    assert counts() == {}


def test_doctor_accounts_for_every_event_and_names_stale_paths(env):
    env(SONGS)
    musicdb.add_events([local("2026-09-26T10:00:00", RICK, YT, "mb-rick"),
                        ev("lb", "2026-09-26T10:00:00", mbid="mb-rick", extra=json.dumps({"msid": "m1"})),
                        local("2026-09-26T11:00:00", "gone/Unknown.mp3"),
                        ev("spotify", "2026-09-26T12:00:00", artist="Nobody", title="Nothing")])
    musicdb.PLAYLISTS.mkdir()
    (musicdb.PLAYLISTS / "Mine.m3u").write_text(RICK + "\nold/path.mp3\n")
    out = doctor.check()
    assert out["accounting"] == {"events": 4, "credited": 1, "unmatched": 2, "lb-copy-of-local": 1, "ok": True}
    assert out["local-orphans"] == [{"ts": "2026-09-26T11:00:00", "file": "gone/Unknown.mp3"}]
    assert out["stale-paths"] == [{"where": "playlist Mine", "file": "old/path.mp3"}]


def test_sync_snapshots_likes_with_the_song_identity(env, monkeypatch):
    m = env(SONGS)
    monkeypatch.setattr(musicdb, "push_feedback", lambda c, scores: 0)
    m.stickers[RICK] = {"like": "2"}
    musicdb.sync(None)
    likes = [json.loads(l) for l in (musicdb.DATA / "likes.jsonl").read_text().splitlines()]
    assert likes == [{"key": f"yt:{YT}", "file": RICK, "ytid": YT, "mbid": "mb-rick", "artist": "Rick Astley",
                      "title": "Never Gonna Give You Up", "like": "2"}]
    weights = json.loads((pathlib.Path(os.environ["XDG_STATE_HOME"]) / "rormpc/weights.json").read_text())["files"]
    assert weights[RICK]["w"] == 2  # never played, liked: doubled


@pytest.mark.parametrize("system_tz", ["Europe/Warsaw", "America/New_York"])
def test_reimport_in_another_system_zone_does_not_double_count(env, monkeypatch, system_tz):
    """Timestamps are naive local time compared as text: with history_timezone set, a listen imported again
    after the computer changed zones is the same event, and a deletion names the same second."""
    env(SONGS)
    monkeypatch.setattr(musicdb, "TZ", zoneinfo.ZoneInfo("Europe/Warsaw"))
    epoch = 1790000000
    try:
        for zone in ("Europe/Warsaw", system_tz):
            os.environ["TZ"] = zone; time.tzset()
            musicdb.add_events([ev("lb", musicdb.local_ts(epoch), mbid="mb-aha", artist="a-ha", title="Take On Me")])
            assert musicdb.epoch_of(musicdb.local_ts(epoch)) == epoch
    finally:
        os.environ.pop("TZ"); time.tzset()
    assert counts() == {"cd/02 Take On Me.flac": 1}


def test_listenbrainz_failure_in_sync_keeps_the_local_results(env, monkeypatch):
    m = env(SONGS)
    def down(c, scores):
        raise TimeoutError("The read operation timed out")
    monkeypatch.setattr(musicdb, "push_feedback", down)
    musicdb.add_events([local("2026-09-26T10:00:00", RICK, YT, "mb-rick")])
    with pytest.raises(TimeoutError):
        musicdb.sync(None)
    assert m.stickers[RICK]["playCount"] == "1"
    assert (musicdb.PLAYLISTS / "Not finished.m3u").exists() and (musicdb.PLAYLISTS / "Skipped.m3u").exists()


def test_update_backs_off_listenbrainz_and_keeps_the_local_steps(env, monkeypatch, tmp_path):
    env(SONGS)
    from rormpc_tools import doctor
    monkeypatch.setattr(musicdb, "LB_BACKOFF", tmp_path / "lb-backoff.json")
    monkeypatch.setattr(doctor, "SUMMARY", tmp_path / "doctor.json")
    calls = []
    def lb_down(_a):
        calls.append("import_lb"); raise TimeoutError("The read operation timed out")
    monkeypatch.setattr(musicdb, "import_lb", lb_down)
    monkeypatch.setattr(musicdb, "lb_playlists", lambda a: calls.append("lb_playlists"))
    monkeypatch.setattr(musicdb, "push_feedback", lambda c, s: calls.append("push") or 0)
    monkeypatch.setattr(musicdb, "youtube_index_daily", lambda: None)
    monkeypatch.setattr(musicdb, "deletions", lambda a: None)
    from rormpc_tools import identity
    monkeypatch.setattr(identity, "sync", lambda *a, **k: {"new": [], "renamed": [], "gone": [], "tagged": [], "conflicts": []})
    musicdb.LISTENS_LOG.write_text(json.dumps({"ts": 1790000000, "file": RICK, "mbid": "mb-rick"}) + "\n")
    with pytest.raises(SystemExit):
        musicdb.update(None)
    assert calls == ["import_lb", "push", "lb_playlists"]
    assert json.loads((tmp_path / "lb-backoff.json").read_text())["failures"] == 1
    calls.clear()
    musicdb.update(None)  # within the hour: ListenBrainz is skipped, local listens still count
    assert calls == [] and counts() == {RICK: 1}
    musicdb.lb_backoff_record(True)
    assert json.loads((tmp_path / "lb-backoff.json").read_text()) == {"failures": 0, "next": 0}
