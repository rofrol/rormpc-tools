"""musicdb dedupe on real (generated) MP3s: survivor gets the state, copies leave the library, nothing is lost."""
import json, shutil, subprocess

import pytest
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TXXX

from rormpc_tools import dedupe, doctor, lyrics, musicdb

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
YT = "dQw4w9WgXcQ"
A = f"Mix/001--Rick--Rick_Astley_-_Never--{YT}--20091025.mp3"
B = f"Love/014--Rick--Rick_Astley_-_Never--{YT}--20091025.mp3"
OTHER = "Mix/002 Other.mp3"


def mp3(path, freq):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-y", str(path)],
                   check=True)
    t = ID3(); t.add(TPE1(text="Rick Astley")); t.add(TIT2(text="Never Gonna Give You Up")); t.save(path)


@pytest.fixture
def lib(env, tmp_path, monkeypatch):
    music = tmp_path / "music"
    mp3(music / A, 440)
    (music / B).parent.mkdir(parents=True)
    shutil.copy(music / A, music / B)
    t = ID3(music / B); t.add(APIC(data=b"cover", mime="image/jpeg")); t.add(TXXX(desc="MusicBrainz Album Id", text="alb")); t.save()
    mp3(music / OTHER, 880)
    monkeypatch.setattr(musicdb, "MUSIC", music)
    monkeypatch.setattr(dedupe, "QUARANTINE", tmp_path / "quarantine")
    monkeypatch.setattr(dedupe, "CACHE", tmp_path / "hash-cache.json")
    monkeypatch.setattr(lyrics, "LYRICS", tmp_path / "lyrics")
    monkeypatch.setattr(lyrics, "INDEX", tmp_path / "lyrics" / "index.json")
    monkeypatch.setattr(dedupe, "mpd_update", lambda: None)
    monkeypatch.setattr(musicdb, "push_feedback", lambda c, scores: 0)
    song = {"artist": "Rick Astley", "title": "Never Gonna Give You Up", "duration": "1"}
    m = env([{**song, "file": A}, {**song, "file": B}, {"file": OTHER, "artist": "X", "title": "Other"}])
    return music, m


def test_tag_writes_do_not_change_the_audio_hash(lib):
    music, _ = lib
    h = dedupe.hashes([A, B, OTHER])
    assert h[A] == h[B] != h[OTHER]


def test_apply_merges_state_into_one_copy_and_loses_nothing(lib, tmp_path):
    music, m = lib
    m.stickers[A] = {"like": "2"}  # like on one copy ...
    musicdb.add_events([("local", "2026-09-26T10:00:00", YT, None, None, None, None, None, json.dumps({"file": B}))])
    c = musicdb.db()  # ... plays and a skip on the other
    c.execute("INSERT INTO skips VALUES (?,?,?,?,?,?)", ("2026-09-27T10:00:00", B, "", 10, 200, 10)); c.commit()
    musicdb.PLAYLISTS.mkdir()
    (musicdb.PLAYLISTS / "Mine.m3u").write_text(f"{B}\n{OTHER}\n")
    lyrics.paths(B)[1].parent.mkdir(parents=True); lyrics.paths(B)[1].write_text("la la\n")
    lyrics.save({B: {"state": "plain"}})

    groups = dedupe.plan(m)
    assert [(g["keep"], g["drop"]) for g in groups] == [(A, [B])]  # the liked copy stays
    dedupe.apply(groups, m)
    m.songs = [s for s in m.songs if s["file"] != B]  # what `mpc update` would do

    assert not (music / B).exists() and list((tmp_path / "quarantine").rglob("*.mp3"))[0].name == B.split("/")[1]
    t = ID3(music / A)
    assert t.getall("APIC") and t.getall("TXXX:MusicBrainz Album Id")  # tags only the dropped copy had
    assert (musicdb.PLAYLISTS / "Mine.m3u").read_text() == f"{A}\n{OTHER}\n"
    assert (musicdb.PLAYLISTS / "Folder Love.m3u").read_text() == f"{B}\n".replace(B, A)  # folder order kept
    assert lyrics.paths(A)[1].read_text() == "la la\n" and lyrics.load() == {A: {"state": "plain"}}
    plays, *_ = musicdb.counted(musicdb.db(), musicdb.library())
    assert dict(plays) == {A: 1}  # the play logged on B now counts on A
    assert m.stickers[A]["like"] == "2" and m.stickers[A]["skips"].strip() == "1"
    musicdb.SKIPS_LOG.write_text(json.dumps({"ts": musicdb.epoch_of("2026-09-27T10:00:00"), "file": B}) + "\n")
    musicdb.import_skips(None)  # the hourly re-import of the scrobbler's log must not add a second skip
    musicdb.sync(None)
    assert m.stickers[A]["skips"].strip() == "1"
    out = doctor.check()
    assert out["stale-paths"] == [] and out["shared-ids"] == [] and out["accounting"]["ok"]
    assert dedupe.plan(m) == []  # idempotent


def test_apply_refuses_copies_with_different_likes(lib, monkeypatch):
    music, m = lib
    m.stickers[A] = {"like": "2"}; m.stickers[B] = {"like": "0"}
    plan = dedupe.plan
    monkeypatch.setattr(dedupe, "plan", lambda: plan(m))
    with pytest.raises(SystemExit):
        dedupe.main(["--apply"])
    assert (music / B).exists()
