"""`mpd-player nowplaying` without Now Playing: what it shows (pure), when it claims, its remote commands as datagrams
on the command socket, and its MPD thread against a fake asyncio MPD. Nothing here touches MediaPlayer or Cocoa."""
import asyncio, pathlib, shutil, tempfile

import pytest
from mpd.base import CommandError

from rormpc_tools import player
from rormpc_tools.player import control, nowplaying


def test_info_of_a_playing_song():
    status = {"state": "play", "songid": "7", "elapsed": "12.5", "duration": "200.1"}
    song = {"file": "a/b.mp3", "title": "T", "artist": ["X", "Y"], "album": "Al"}
    assert nowplaying.info(status, song) == {"state": "play", "songid": "7", "file": "a/b.mp3", "title": "T",
                                             "artist": "X, Y", "album": "Al", "elapsed": 12.5, "rate": 1.0,
                                             "duration": 200.1}


def test_info_paused_untagged_and_stopped():
    shown = nowplaying.info({"state": "pause", "songid": "1", "elapsed": "3"}, {"file": "dir/x.flac", "time": "60"})
    assert shown == {"state": "pause", "songid": "1", "file": "dir/x.flac", "title": "x.flac", "elapsed": 3.0,
                     "rate": 0.0, "duration": 60.0}
    assert nowplaying.info({"state": "stop"}, {}) == {"state": "stop"}
    assert nowplaying.info({"state": "play"}, {}) == {"state": "stop"}  # a song gone between the two reads


def test_nothing_is_claimed_until_mpd_plays_then_the_true_state():
    c = nowplaying.Claim()
    paused, playing, stopped = {"state": "pause"}, {"state": "play"}, {"state": "stop"}
    assert c.view(paused) is None and c.view(stopped) is None and not c.first
    assert c.view(playing) is playing and c.first
    assert c.view(paused) is paused and not c.first  # paused stays paused, still shown
    assert c.view(stopped) is stopped and not c.first


def test_remote_commands_become_transport_commands_named_nowplaying():
    sent = []

    def deliver(command, position, path, sender):
        sent.append((command, position, path, sender))
    for remote in ("toggle", "play", "pause", "stop", "next", "previous"):
        assert nowplaying.handler(remote, "/s", deliver)()
    assert nowplaying.handler("seek", "/s", deliver)(83.5)
    assert sent == [("toggle", None, "/s", "nowplaying"), ("play", None, "/s", "nowplaying"),
                    ("pause", None, "/s", "nowplaying"), ("stop", None, "/s", "nowplaying"),
                    ("next", None, "/s", "nowplaying"), ("prev", None, "/s", "nowplaying"),  # the trail
                    ("seek", 83.5, "/s", "nowplaying")]


def test_a_command_fails_while_mpd_player_is_down(capsys):
    d = pathlib.Path(tempfile.mkdtemp(prefix="mps-"))
    try:
        assert not nowplaying.handler("next", d / "player.sock")()
        assert "nowplaying: next: mpd-player is not running" in capsys.readouterr().err
    finally:
        shutil.rmtree(d)


def test_a_remote_command_reaches_the_daemons_queue_through_the_socket():
    d = pathlib.Path(tempfile.mkdtemp(prefix="mps-"))
    path = d / "rormpc" / "player.sock"
    s = control.Socket(path)
    assert s.open()

    async def main():
        daemon = player.Daemon(None, [], s)
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: control.Receiver(daemon), sock=s.sock)
        try:
            assert nowplaying.handler("previous", path)()
            assert nowplaying.handler("seek", path)(30.0)
            while len(daemon.commands) < 2:
                daemon._event.clear()
                await daemon._event.wait()
            return [(c, src) for c, src, _ in daemon.commands]
        finally:
            transport.close()
    try:
        assert asyncio.run(main()) == [({"command": "prev"}, "nowplaying"),
                                       ({"command": "seek", "position": 30.0}, "nowplaying")]
    finally:
        s.close()
        shutil.rmtree(d)


class AMPD:
    """The asyncio client calls the MPD thread makes; `events` feeds idle, None ends the connection."""

    def __init__(self, picture=b"img"):
        self.st = {"state": "play", "songid": "1", "elapsed": "3", "duration": "200"}
        self.songs = {"1": {"file": "a.mp3", "title": "A"}, "2": {"file": "b.mp3", "title": "B"}}
        self.picture = picture
        self.calls = []
        self.events = asyncio.Queue()

    async def status(self):
        return dict(self.st)

    async def currentsong(self):
        return dict(self.songs[self.st["songid"]])

    async def readpicture(self, f):
        self.calls.append(("readpicture", f))
        return {"binary": self.picture} if self.picture else {}

    async def albumart(self, f):
        self.calls.append(("albumart", f))
        raise CommandError("[50@0] {albumart} No file exists")

    async def idle(self, subsystems):
        while True:
            ev = await self.events.get()
            if ev is None:
                raise ConnectionError("Connection lost while reading line")
            yield [ev]


def test_the_mpd_thread_posts_every_change_and_a_refresh_and_fetches_a_cover_once_per_song():
    mpd = AMPD()

    async def main():
        got = asyncio.Queue()

        async def connect():
            return mpd
        w = nowplaying.Watcher(lambda kind, payload: got.put_nowait((kind, payload)), connect)
        task = asyncio.create_task(w.watch())
        kind, shown = await got.get()
        assert kind == "info" and shown["title"] == "A" and shown["cover"] == b"img"
        mpd.st["state"] = "pause"
        mpd.events.put_nowait("player")
        assert (await got.get())[1]["state"] == "pause"
        w.refresh()  # the Mac woke
        assert (await got.get())[1]["state"] == "pause"
        mpd.st.update(state="play", songid="2")
        mpd.events.put_nowait("player")
        assert (await got.get())[1]["title"] == "B"
        mpd.events.put_nowait(None)
        with pytest.raises(ConnectionError):
            await task
    asyncio.run(main())
    assert mpd.calls == [("readpicture", "a.mp3"), ("readpicture", "b.mp3")]


def test_the_folder_cover_when_none_is_embedded_and_none_when_too_big(monkeypatch):
    mpd = AMPD(picture=None)
    shown, cover = asyncio.run(nowplaying.snapshot(mpd, (None, None)))
    assert "cover" not in shown and cover == ("a.mp3", None)
    assert mpd.calls == [("readpicture", "a.mp3"), ("albumart", "a.mp3")]
    monkeypatch.setattr(nowplaying, "COVER_MAX", 2)
    shown, cover = asyncio.run(nowplaying.snapshot(AMPD(picture=b"big"), (None, None)))
    assert "cover" not in shown


def test_a_lost_connection_is_posted_once_and_ends_the_thread():
    posts = []

    async def connect():
        raise ConnectionRefusedError("nobody at 6600")
    nowplaying.Watcher(lambda kind, payload: posts.append((kind, payload)), connect).run()
    assert posts == [("lost", "ConnectionRefusedError: nobody at 6600")]


def test_cli():
    a = player.parse_args(["--socket", "/x.sock", "nowplaying", "--check"])
    assert a.action == "nowplaying" and a.check and a.socket == "/x.sock"
