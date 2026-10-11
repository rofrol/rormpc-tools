"""Banned (decided 2026-10-11): `musicdb ban|unban`, undo of the last ban, `musicdb banned`, and `hits` leaving
banned songs out after the ranks. A temporary data dir only; ListenBrainz and the files are never touched."""
import argparse, json, sys

import pytest

from rormpc_tools import banned, hits, musicdb

RICK = {"file": "yt/001--Rick_Astley--dQw4w9WgXcQ--20091025.mp3", "artist": "Rick Astley",
        "title": "Never Gonna Give You Up", "duration": "213"}
B = {"file": "b.mp3", "artist": "Band", "title": "B", "duration": "200"}


@pytest.fixture
def log(env, tmp_path, monkeypatch):
    monkeypatch.setattr(banned, "DATA", tmp_path / "music-data")
    monkeypatch.delenv("SELECTED_SONGS", raising=False)
    monkeypatch.delenv("FILE", raising=False)
    env([RICK, B])
    return tmp_path / "music-data" / "banned.jsonl"


def run(capsys, cmd, *files, undo=False):
    musicdb.ban_cmd(argparse.Namespace(cmd=cmd, files=list(files), undo=undo))
    return capsys.readouterr().out.strip()


def listed(capsys):
    musicdb.banned_cmd(argparse.Namespace(json=True))
    return json.loads(capsys.readouterr().out)


def test_ban_appends_an_event_with_the_song_key_and_the_file_stays(log, capsys, tmp_path):
    assert run(capsys, "ban", RICK["file"]) == "Banned: Rick Astley - Never Gonna Give You Up"
    [e] = [json.loads(line) for line in log.read_text().splitlines()]
    assert {k: e[k] for k in ("action", "key", "file", "artist", "title")} == {
        "action": "ban", "key": "yt:dQw4w9WgXcQ", "file": RICK["file"], "artist": "Rick Astley",
        "title": "Never Gonna Give You Up"}
    assert e["ts"] and e["op"]
    assert not musicdb.PENDING.exists()  # not a deletion
    assert run(capsys, "ban", RICK["file"]) == f"Already banned: {RICK['file']}"
    assert len(log.read_text().splitlines()) == 1


def test_unban_and_undo_of_the_last_ban(log, capsys):
    run(capsys, "ban", RICK["file"])
    run(capsys, "ban", B["file"])
    assert [s["file"] for s in listed(capsys)["songs"]] == [B["file"], RICK["file"]]  # newest first
    assert run(capsys, "ban", undo=True) == "Unbanned: Band - B"  # the newest ban
    assert [s["file"] for s in listed(capsys)["songs"]] == [RICK["file"]]
    assert run(capsys, "unban", RICK["file"]) == "Unbanned: Rick Astley - Never Gonna Give You Up"
    assert listed(capsys)["songs"] == []
    with pytest.raises(SystemExit, match="Nothing to undo"):
        run(capsys, "ban", undo=True)
    assert [json.loads(line)["action"] for line in log.read_text().splitlines()] == ["ban", "ban", "unban", "unban"]


def test_undo_takes_back_a_whole_ban_command(log, capsys):
    run(capsys, "ban", RICK["file"], B["file"])
    run(capsys, "ban", undo=True)
    assert listed(capsys)["songs"] == []


def test_songs_come_from_selected_songs_else_file(log, capsys, monkeypatch):
    monkeypatch.setenv("FILE", B["file"])
    run(capsys, "ban")
    monkeypatch.setenv("SELECTED_SONGS", RICK["file"])
    run(capsys, "ban")
    assert {s["file"] for s in listed(capsys)["songs"]} == {B["file"], RICK["file"]}


def test_hits_leaves_banned_songs_out_after_the_ranks(log, capsys, monkeypatch, tmp_path):
    songs = {s["file"]: {"artist": s["artist"], "title": s["title"], "file": s["file"], "year": 1987,
                         "years": [1987], "mbid": None, "artist_mbid": None, "tags": [], "listens": 0,
                         "hidden": False, "liked": False} for s in (RICK, B)}
    monkeypatch.setattr(hits, "library_songs", lambda: songs)
    out = tmp_path / "current.json"
    argv = ["hits", "--years", "1980-1989", "--rank", "none", "--years-of", "release", "--json", str(out)]

    def rows():
        monkeypatch.setattr(sys, "argv", argv)
        hits.main()
        capsys.readouterr()
        data = json.loads(out.read_text())
        return data["counts"], [(r["file"], r["rank"]) for r in data["rows"]]

    counts, before = rows()
    assert counts["banned"] == 0 and len(before) == 2
    run(capsys, "ban", before[0][0])
    counts, after = rows()
    assert after == before[1:]  # the other song keeps its rank
    assert counts["banned"] == 1 and counts["owned"] == 1
