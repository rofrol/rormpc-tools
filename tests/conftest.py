"""Offline test setup: settings point at a throwaway directory before any tool module is imported, and MPD is a
fake in memory. Nothing here touches the network, the real MPD or the user's data."""
import os, pathlib, tempfile

_root = pathlib.Path(tempfile.mkdtemp(prefix="rormpc-tools-test-"))
os.environ.update({
    "RORMPC_TOOLS_CONFIG": str(_root / "absent.toml"),
    "XDG_CONFIG_HOME": str(_root / "config"), "XDG_DATA_HOME": str(_root / "data"),
    "XDG_CACHE_HOME": str(_root / "cache"), "MUSICDB": str(_root / "plays.db"),
    "MUSICDB_DATA": str(_root / "music-data"), "YTMB_MUSIC_DIR": str(_root / "music"),
    "MPD_PORT": "1",  # a forgotten monkeypatch fails to connect instead of reaching the real MPD
    "LB_CUTOFF": "2026-01-01T00:00:00",
})

import pytest  # noqa: E402

from rormpc_tools import musicdb  # noqa: E402


class FakeMPD:
    """The few MPD calls the tools make, over a list of song dicts ({"file", "artist", "title", ...})."""

    def __init__(self, songs=()):
        self.songs = [dict(s) for s in songs]
        self.stickers = {}

    def listallinfo(self):
        return [dict(s) for s in self.songs]

    def find(self, _tag, rel):
        return [dict(s) for s in self.songs if s["file"] == rel]

    def sticker_list(self, _type, f):
        if f not in self.stickers:
            raise RuntimeError("no such sticker")
        return dict(self.stickers[f])

    def sticker_set(self, _type, f, k, v):
        self.stickers.setdefault(f, {})[k] = v

    def sticker_delete(self, _type, f, k):
        self.stickers[f].pop(k)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB and data dir per test; returns a function that installs a FakeMPD with the given songs."""
    data = tmp_path / "music-data"
    data.mkdir()
    for name, value in {"DB": tmp_path / "plays.db", "DATA": data, "PLAYLISTS": tmp_path / "playlists",
                        "LISTENS_LOG": tmp_path / "listens.jsonl", "SKIPS_LOG": tmp_path / "skips.jsonl",
                        "MPD_LOG": tmp_path / "mpd.log", "NF_KEEP": data / "not-finished-keep.jsonl",
                        "DONE": data / "deletions" / "done.jsonl", "PENDING": data / "deletions" / "pending.jsonl",
                        "LB_CUTOFF": "2026-01-01T00:00:00"}.items():
        monkeypatch.setattr(musicdb, name, value)
    monkeypatch.setattr(musicdb, "LB_PAUSED", [False])
    from rormpc_tools import identity
    identity._cache.clear()
    monkeypatch.setattr(identity, "TAG_CACHE", tmp_path / "identity-tags.json")
    holder = {}

    def install(songs=()):
        holder["mpd"] = FakeMPD(songs)
        monkeypatch.setattr(musicdb, "mpd", lambda: holder["mpd"])
        return holder["mpd"]

    install()
    return install
