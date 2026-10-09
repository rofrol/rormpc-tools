"""What rormpc reads from the tools: `--version` (its debuginfo), `hits --json` (its Hits pane) and `musicdb delete
--preview`'s JSON (its delete menu). rormpc's delete_menu.rs and hits.rs parse a copy of these shapes in their own
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
                                               "sets": ["-likes"], "show_hidden": False, "source": None,
                                               "sort": "plays"}
    assert data["rules"] == {"schema": 1, "sets": {"likes": -1}, "rank": "plays", "years_of": "release",
                             "period": "1980-1989", "top": "1-100", "genre": "", "artist": "", "owned": False}
    assert data["formula"] == "Library − Likes ∩ 1980-1989 ∩ Top 1-100%"
    assert data["summary"] == "Library − Likes ∩ 1980-1989 ∩ Top 1-100% · 1 of 1"
    assert data["counts"] == {"selected": 1, "owned": 1, "candidates": 1, "cohort": 2}
    [row] = data["rows"]
    assert set(row) == {"rank", "pct", "cohort", "ranked", "artist", "title", "year", "years", "points", "peak",
                        "listens", "sets", "genres", "mbid", "file", "hidden", "plays", "reason"}
    assert (row["file"], row["rank"], row["cohort"], row["ranked"], row["sets"]) == (SONG["file"], 2, 2, True, [])  # "B" ranks first on the tie


def test_hits_refuses_top_without_a_rank(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hits", "--set", "+recommended", "--top", "1-10"])
    with pytest.raises(SystemExit, match="needs a rank"):
        hits.main()
