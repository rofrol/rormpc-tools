"""What rormpc reads from the tools: `--version` (its debuginfo), `hits --json` (its Hits pane), `musicdb delete
--preview`'s JSON (its delete menu) and `musicdb banned --json` (its Banned list); and what mpd-player reads from
rormpc: source.json (`source_id` keys the round, `generation` is ignored, older files still work). rormpc's delete_menu.rs and hits.rs parse a copy of these shapes in their own
tests; change both together and bump PREVIEW_VERSION when a field changes meaning or goes away."""
import argparse, json, sys

import pytest

from rormpc_tools import hits, musicdb, settings

SONG = {"file": "yt/001--Rick_Astley--dQw4w9WgXcQ--20091025.mp3", "artist": "Rick Astley",
        "title": "Never Gonna Give You Up", "duration": "213"}


@pytest.mark.parametrize("tool, name", [(musicdb, "musicdb"), (hits, "hits")])
def test_version_flag(tool, name, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [name, "--version"])
    tool.main()
    assert capsys.readouterr().out == f"{name} {settings.version()}\n"


def test_delete_preview_is_versioned_and_changes_nothing(env, capsys):
    env([SONG])
    musicdb.delete(argparse.Namespace(files=[SONG["file"]], preview=True, youtube=False, permanent=False,
                                      listenbrainz=False))
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == musicdb.PREVIEW_VERSION == 1
    [song] = out["songs"]
    assert set(song) == {"file", "artist", "title", "ytid", "exists", "plays", "lb_listens", "shared"}
    assert (song["artist"], song["ytid"], song["lb_listens"], song["shared"]) == ("Rick Astley", "dQw4w9WgXcQ", 0, [])
    assert not musicdb.PENDING.exists()


def test_hits_json_carries_the_rules_formula_and_counts(env, monkeypatch, tmp_path, capsys):
    """rormpc's Hits pane (hits.rs `HitsFile`, test `hits_file_contract`) reads these fields; version 1 only
    gains fields. Old files without "rules"/"sets" still load there (mapped from args.source)."""
    env([SONG, SONG | {"file": "b.mp3", "title": "B"}])
    songs = {s["file"]: {"artist": s["artist"], "title": s["title"], "file": s["file"], "year": 1987, "years": [1987],
                         "mbid": None, "artist_mbid": None, "tags": [], "listens": 0, "hidden": False,
                         "liked": s["file"] == "b.mp3"} for s in musicdb.mpd().songs}
    monkeypatch.setattr(hits, "library_songs", lambda: songs)
    out = tmp_path / "current.json"
    monkeypatch.setattr(sys, "argv", ["hits", "--years", "1980-1989", "--set", "-likes", "--rank", "plays",
                                      "--years-of", "release", "--top", "1-100", "--json", str(out)])
    hits.main()
    capsys.readouterr()
    data = json.loads(out.read_text())
    assert data["version"] == 1
    assert {"label", "generated_at", "args", "rules", "formula", "summary", "counts", "rank_note", "rows",
            "artists"} <= set(data)
    assert data["args"] | {"period": None} == {"period": None, "top": "1-100", "genre": "", "artist": "",
                                               "owned": False, "rank": "plays", "years_of": "release",
                                               "sets": ["-likes"], "set_names": {}, "show_hidden": False,
                                               "show_excluded": False, "source": None, "sort": "plays",
                                               "open_list": None, "open_list_name": None}
    assert data["rules"] == {"schema": 1, "sets": {"likes": -1}, "rank": "plays", "years_of": "release",
                             "period": "1980-1989", "top": "1-100", "genre": "", "artist": "", "owned": False}
    assert data["formula"] == "Library − Likes ∩ 1980-1989 ∩ Top 1-100%"
    assert data["summary"] == "Library − Likes ∩ 1980-1989 ∩ Top 1-100% · 1 of 1"
    assert data["counts"] == {"selected": 1, "owned": 1, "candidates": 1, "cohort": 2, "pinned": 0, "excluded": 0,
                              "banned": 0}
    [row] = data["rows"]
    assert set(row) == {"rank", "pct", "cohort", "ranked", "artist", "title", "year", "years", "points", "peak",
                        "listens", "sets", "genres", "mbid", "file", "hidden", "plays", "reason", "pinned",
                        "excluded", "exceptions", "song_id", "chart_key", "deleted"}
    assert (row["file"], row["rank"], row["cohort"], row["ranked"], row["sets"]) == (SONG["file"], 2, 2, True, [])  # "B" ranks first on the tie
    assert (row["pinned"], row["excluded"], row["exceptions"], row["song_id"]) == (False, False, [], None)
    assert row["chart_key"] == hits.hide_key(SONG["artist"], SONG["title"])
    assert row["deleted"] is None


def test_hits_refuses_top_without_a_rank(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hits", "--set", "+recommended", "--top", "1-10"])
    with pytest.raises(SystemExit, match="needs a rank"):
        hits.main()


def test_banned_json_is_versioned(env, monkeypatch, tmp_path, capsys):
    """rormpc's Banned list reads {version, songs: [{key, file, artist, title, ts}]}, newest ban first."""
    from rormpc_tools import banned
    monkeypatch.setattr(banned, "DATA", tmp_path / "music-data")
    env([SONG])
    musicdb.ban_cmd(argparse.Namespace(cmd="ban", files=[SONG["file"]], undo=False))
    capsys.readouterr()
    musicdb.banned_cmd(argparse.Namespace(json=True))
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == musicdb.BANNED_VERSION == 1
    [song] = out["songs"]
    assert set(song) == {"key", "file", "artist", "title", "ts"}
    assert (song["key"], song["file"], song["artist"]) == ("yt:dQw4w9WgXcQ", SONG["file"], "Rick Astley")


def source_json(**extra):
    """source.json as rormpc's Replace writes it (rormpc_upnext.rs), plus the instant-filtering fields."""
    return {"source": {"kind": "hits", "name": "1980s", "len": 2, "files": ["b", "c"],
                       "rules": {"schema": 1, "sets": {"billboard": 1}}, "rules_hash": "8c1f", "added": [], **extra}}


@pytest.mark.parametrize("extra, key", [
    ({"source_id": "6f1c0a52-3b7e-4d55-9d1e-0f6a7b3c2d10", "generation": 7},
     "hits:id:6f1c0a52-3b7e-4d55-9d1e-0f6a7b3c2d10"),
    ({}, "hits:8c1f"),  # an older rormpc: no source_id, the rules hash keys the round
])
def test_mpd_player_reads_source_json(extra, key, tmp_path, monkeypatch):
    import asyncio
    from rormpc_tools import player
    from rormpc_tools.player import shuffle
    from test_shuffle import PlayingMPD
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    player.write_state("source", source_json(**extra))
    mpd = PlayingMPD(["a", "b", "c"], random=True)
    mpd.cur, mpd.state = "1", "play"
    sh = shuffle.Shuffle()
    asyncio.run(player.Daemon(mpd, [sh]).step({"player", "options", "playlist"}))
    assert sh.round["source"] == key and sh.round["total"] == 2
    # the next generation of the same source keeps the round
    if extra:
        player.write_state("source", source_json(**{**extra, "generation": 8, "rules_hash": "9d2e"}))
        asyncio.run(player.Daemon(mpd, [sh]).step({"playlist"}))
        assert sh.round["source"] == key
