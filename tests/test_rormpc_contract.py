"""What rormpc reads from the tools: `--version` (its debuginfo) and `musicdb delete --preview`'s JSON (its delete
menu). rormpc's delete_menu.rs parses a copy of this shape in its own test; change both together and bump
PREVIEW_VERSION when a field changes meaning or goes away."""
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
