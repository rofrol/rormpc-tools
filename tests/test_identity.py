"""musicdb identity: one id per file that survives renames, and the file-name pattern lives in one place."""
import json, pathlib, shutil, subprocess

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

from rormpc_tools import dedupe, identity, musicdb

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
YT = "dQw4w9WgXcQ"
OLD = f"Mix/001--Rick--Rick_Astley_-_Never--{YT}--20091025.mp3"
NEW = f"Rick Astley - Never Gonna Give You Up [{YT}].mp3"
CD = "CD/01 Take On Me.flac"


def make(music, rel, freq):
    p = music / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-y", str(p)], check=True)
    if p.suffix == ".mp3":
        t = ID3(); t.add(TPE1(text="Rick Astley")); t.add(TIT2(text="Never Gonna Give You Up")); t.save(p)


@pytest.fixture
def lib(env, tmp_path, monkeypatch):
    music = tmp_path / "music"
    make(music, OLD, 440); make(music, CD, 660)
    monkeypatch.setattr(musicdb, "MUSIC", music)
    monkeypatch.setattr(dedupe, "CACHE", tmp_path / "hash-cache.json")
    m = env([{"file": OLD, "artist": "Rick Astley", "title": "Never Gonna Give You Up"},
             {"file": CD, "artist": "a-ha", "title": "Take On Me", "musicbrainz_trackid": "mb-aha"}])
    return music, m


def rows():
    return {r["id"]: r for r in musicdb.jsonl(identity.registry_path())}


def test_sync_gives_every_file_an_id_in_the_registry_and_the_tags(lib):
    music, m = lib
    rep = identity.sync(m)
    assert sorted(rep["new"]) == sorted([OLD, CD]) and sorted(rep["tagged"]) == sorted([OLD, CD])
    by_path = {r["path"]: r for r in rows().values()}
    assert by_path[OLD]["ytid"] == YT and by_path[CD]["mbid"] == "mb-aha" and by_path[CD].get("ytid") is None
    assert identity.read_tags(OLD) == (by_path[OLD]["id"], YT)
    assert identity.read_tags(CD) == (by_path[CD]["id"], None)  # FLAC: Vorbis comments
    again = identity.sync(m)
    assert not any(again.values())  # stable: nothing new, nothing re-tagged


def test_a_renamed_file_keeps_its_id_ytid_and_plays(lib):
    music, m = lib
    identity.sync(m)
    sid = identity.resolve(OLD)["id"]
    musicdb.add_events([("local", "2026-09-26T10:00:00", YT, None, None, None, None, None, json.dumps({"file": OLD}))])
    (music / NEW).parent.mkdir(parents=True, exist_ok=True)
    (music / OLD).rename(music / NEW)  # moved by hand, no alias recorded
    m.songs[0]["file"] = NEW
    rep = identity.sync(m)
    assert rep["renamed"] == [{"id": sid, "from": OLD, "to": NEW}] and not rep["new"] and not rep["gone"]
    assert identity.resolve(OLD)["id"] == identity.resolve(NEW)["id"] == sid
    assert identity.ytid(NEW) == YT
    plays, *_ = musicdb.counted(musicdb.db(), musicdb.library())
    assert dict(plays) == {NEW: 1}


def test_a_copy_carrying_the_same_id_is_reported_not_merged(lib):
    music, m = lib
    identity.sync(m)
    shutil.copy(music / OLD, music / "Copy.mp3")
    m.songs.append({"file": "Copy.mp3", "artist": "Rick Astley", "title": "Never Gonna Give You Up"})
    rep = identity.sync(m)
    assert rep["conflicts"][0]["files"] == ["Copy.mp3", OLD] or rep["conflicts"][0]["files"] == [OLD, "Copy.mp3"]


def test_gone_and_merged_files(lib):
    music, m = lib
    identity.sync(m)
    cd_id, rick_id = identity.resolve(CD)["id"], identity.resolve(OLD)["id"]
    musicdb.write_jsonl(musicdb.DATA / "aliases.jsonl", [{"old": CD, "new": OLD}])  # e.g. `versions same`
    (music / CD).unlink(); m.songs = [s for s in m.songs if s["file"] != CD]
    rep = identity.sync(m)
    assert rep["gone"] == [CD]
    r = rows()[cd_id]
    assert r["state"] == "merged" and r["into"] == rick_id and r["path"] is None
    assert identity.resolve(CD)["id"] == rick_id


def test_name_patterns():
    assert identity.ytid_from_name(OLD) == YT and identity.ytid_from_name(NEW) == YT
    assert identity.ytid_from_name("Song (2) [abc].mp3") is None
    assert identity.ytid_from_name("x--Ŝ234567890--20200101.mp3") is None  # ASCII ids only


def test_the_file_name_pattern_is_parsed_only_in_identity():
    src = pathlib.Path(identity.__file__).parent
    pattern = "{11})--"  # an 11-character YouTube id group followed by the "--" of the old file names
    assert pattern in r'YTID_IN_NAME = re.compile(r"--([\w-]{11})--\d{8}\.mp3$")'  # the gate catches the old form
    offenders = [p.name for p in src.glob("*.py") if p.name != "identity.py" and pattern in p.read_text()]
    assert offenders == []


def test_doctor_reports_unregistered_and_untagged_files(lib):
    from rormpc_tools import doctor
    music, m = lib
    assert {p["why"] for p in doctor.identity_problems([OLD, CD])} == {"not registered"}
    identity.sync(m, write=False)
    assert {p["why"] for p in doctor.identity_problems([OLD, CD])} == {"id missing from the tags"}
    identity.sync(m)
    assert doctor.identity_problems([OLD, CD]) == []


def test_state_kept_under_an_old_path_follows_a_rename(lib, tmp_path, monkeypatch):
    from rormpc_tools import lyrics as ly
    monkeypatch.setattr(ly, "LYRICS", tmp_path / "lyrics"); monkeypatch.setattr(ly, "INDEX", tmp_path / "lyrics" / "index.json")
    from rormpc_tools import doctor
    music, m = lib
    identity.sync(m)
    c = musicdb.db()
    for day in ("2026-09-27", "2026-09-28"):
        c.execute("INSERT INTO skips VALUES (?,?,?,?,?,?)", (f"{day}T10:00:00", OLD, "", 10, 200, 10))
    c.commit()
    musicdb.write_jsonl(musicdb.NF_KEEP, [{"file": OLD, "action": "keep"}])
    from rormpc_tools import lyrics
    lyrics.paths(OLD)[0].parent.mkdir(parents=True); lyrics.paths(OLD)[0].write_text("[00:01]la\n")
    lyrics.save({OLD: {"state": "synced"}})
    (music / OLD).rename(music / NEW); m.songs[0]["file"] = NEW
    identity.sync(m)
    assert lyrics.paths(NEW)[0].read_text() == "[00:01]la\n" and lyrics.load() == {NEW: {"state": "synced"}}
    assert musicdb.canon(OLD) == NEW
    assert musicdb.skipped(musicdb.db(), {}) == {NEW: 2}
    assert musicdb.kept() == {NEW}
    assert doctor.check()["stale-paths"] == []
