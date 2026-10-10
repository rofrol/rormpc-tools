"""mpd-player's MPRIS player without a bus: the properties as pure functions of MPD's status, what is signalled when,
and every D-Bus method as a transport command in the daemon's queue. The interface is introspected through
dbus-fast (no bus needed)."""
import asyncio

import pytest

from rormpc_tools import player
from rormpc_tools.player import control, mpris

pytest.importorskip("dbus_fast")

SONG = {"file": "dir/a.mp3", "title": "A", "artist": ["X", "Y"], "album": "Al"}


def status(state="play", songid="5", **kw):
    return {"state": state, "songid": songid, "elapsed": "10", "duration": "200.5", "playlistlength": "3",
            "volume": "40", **kw}


def test_properties_while_playing():
    p = mpris.properties(status(), SONG)
    assert p == {"PlaybackStatus": "Playing", "CanGoNext": True, "CanGoPrevious": True, "CanPlay": True,
                 "CanPause": True, "CanSeek": True, "Volume": 0.4,
                 "Metadata": {"mpris:trackid": "/org/mpd_player/song/5", "xesam:title": "A",
                              "xesam:artist": ["X", "Y"], "xesam:album": "Al", "mpris:length": 200500000}}


def test_properties_paused_stopped_empty_and_untagged():
    assert mpris.properties(status("pause"), SONG)["PlaybackStatus"] == "Paused"
    stopped = mpris.properties({"state": "stop", "playlistlength": "3", "volume": "-1"}, {})
    assert stopped["PlaybackStatus"] == "Stopped" and not stopped["CanSeek"] and stopped["CanPlay"]
    assert stopped["Metadata"] == {"mpris:trackid": mpris.NO_TRACK} and stopped["Volume"] == 0
    empty = mpris.properties({"state": "stop", "playlistlength": "0"}, {})
    assert not any(empty[k] for k in ("CanGoNext", "CanGoPrevious", "CanPlay", "CanPause"))
    m = mpris.metadata({"state": "play", "songid": "1"}, {"file": "d/x.flac", "artist": "Solo"})
    assert m == {"mpris:trackid": "/org/mpd_player/song/1", "xesam:title": "x.flac", "xesam:artist": ["Solo"]}


def test_position_is_extrapolated_only_while_playing():
    assert mpris.position_us(status(), 100.0, 102.5) == 12_500_000
    assert mpris.position_us(status("pause"), 100.0, 102.5) == 10_000_000
    assert mpris.position_us({"state": "stop"}, 100.0, 102.5) == 0


def test_set_position_only_for_the_current_track_and_inside_it():
    s = status()
    assert mpris.set_position_command("/org/mpd_player/song/5", 30_000_000, s, SONG) == \
        {"command": "seek", "position": 30.0}
    assert mpris.set_position_command("/org/mpd_player/song/4", 30_000_000, s, SONG) is None
    assert mpris.set_position_command("/org/mpd_player/song/5", -1, s, SONG) is None
    assert mpris.set_position_command("/org/mpd_player/song/5", 300_000_000, s, SONG) is None
    assert mpris.set_position_command(mpris.NO_TRACK, 0, {"state": "stop"}, {}) is None


def test_the_interfaces_as_the_spec_names_them():
    root, p, _ = mpris.interfaces(mpris.Mpris())
    r, x = root.introspect(), p.introspect()
    assert r.name == "org.mpris.MediaPlayer2" and x.name == "org.mpris.MediaPlayer2.Player"
    assert {m.name for m in r.methods} == {"Raise", "Quit"}
    assert {(q.name, q.signature) for q in r.properties} >= {("Identity", "s"), ("CanQuit", "b"),
                                                            ("HasTrackList", "b")}
    assert {m.name: "".join(a.signature for a in m.in_args) for m in x.methods} == {
        "Next": "", "Previous": "", "Pause": "", "PlayPause": "", "Stop": "", "Play": "", "Seek": "x",
        "SetPosition": "ox", "OpenUri": "s"}
    props = {q.name: q.signature for q in x.properties}
    assert props["Metadata"] == "a{sv}" and props["Position"] == "x" and props["PlaybackStatus"] == "s"
    assert [(s.name, [a.signature for a in s.args]) for s in x.signals] == [("Seeked", ["x"])]


class MPD:
    def __init__(self):
        self.st = status()
        self.calls = []

    async def status(self):
        self.calls.append("status")
        return dict(self.st)

    async def currentsong(self):
        self.calls.append("currentsong")
        return dict(SONG, title=f"song {self.st['songid']}")

    async def next(self):
        self.calls.append("next")

    async def pause(self, v):
        self.calls.append(f"pause {v}")

    async def seekcur(self, t):
        self.calls.append(f"seekcur {t}")


class Bus:
    """Stands in for the exported Player interface: what would be signalled."""

    def __init__(self):
        self.signals = []

    def Seeked(self, position):
        self.signals.append(("Seeked", position))


def live(mpd):
    m = mpris.Mpris()
    d = player.Daemon(mpd, [m])
    m.daemon = d
    m.player = Bus()
    m.emit = lambda bus, diff: bus.signals.append(("PropertiesChanged", diff))
    return d, m


def test_what_is_signalled_when():
    mpd = MPD()
    d, m = live(mpd)
    asyncio.run(d.step({"player", "playlist"}))
    (kind, first), = m.player.signals
    assert kind == "PropertiesChanged" and first["PlaybackStatus"] == "Playing" and "Metadata" in first
    m.player.signals.clear()
    mpd.st["state"] = "pause"
    asyncio.run(d.step({"player"}))
    assert m.player.signals == [("PropertiesChanged", {"PlaybackStatus": "Paused"})]
    m.player.signals.clear()
    mpd.st["elapsed"] = "99"
    asyncio.run(d.step(set()))  # a seek, whichever wake reads it
    assert m.player.signals == [("Seeked", 99_000_000)]
    m.player.signals.clear()
    asyncio.run(d.step({"mixer"}))
    asyncio.run(d.step({"player"}))  # an event that moved nothing (e.g. after our own command)
    assert m.player.signals == []
    mpd.st["state"] = "play"
    asyncio.run(d.step({"player"}))
    asyncio.run(d.step({"player"}))  # playing on from 99 s: where the extrapolation expects it
    assert m.player.signals == [("PropertiesChanged", {"PlaybackStatus": "Playing"})]
    m.player.signals.clear()
    mpd.st["elapsed"] = "0"
    asyncio.run(d.step({"player"}))  # the same song from its start (repeat single, Previous to itself)
    assert m.player.signals == [("Seeked", 0)]
    m.player.signals.clear()
    mpd.calls.clear()
    mpd.st.update(songid="6", elapsed="0")
    asyncio.run(d.step({"player"}))
    (kind, diff), = m.player.signals  # another song: no Seeked
    assert set(diff) == {"Metadata"} and diff["Metadata"]["xesam:title"] == "song 6"
    assert mpd.calls.count("currentsong") == 1


def test_every_method_is_a_transport_command_in_the_daemons_queue():
    mpd = MPD()
    d, m = live(mpd)
    asyncio.run(d.step({"player"}))
    _, p, _ = mpris.interfaces(m)
    for name in ("PlayPause", "Play", "Pause", "Stop", "Next", "Previous"):
        getattr(p, name)()
    p.Seek(-2_500_000)
    p.SetPosition("/org/mpd_player/song/5", 7_000_000)
    p.SetPosition("/org/mpd_player/song/9", 7_000_000)  # not the current song: ignored
    assert [(c, src) for c, src, _ in d.commands] == [
        ({"command": "toggle"}, "mpris"), ({"command": "play"}, "mpris"), ({"command": "pause"}, "mpris"),
        ({"command": "stop"}, "mpris"), ({"command": "next"}, "mpris"), ({"command": "prev"}, "mpris"),
        ({"command": "seek", "offset": -2.5}, "mpris"), ({"command": "seek", "position": 7.0}, "mpris")]


@pytest.mark.parametrize("offset, want", [(5.0, ["seekcur 15.000"]), (-30.0, ["seekcur 0.000"]),
                                          (500.0, ["next"])])
def test_relative_seek(offset, want, capsys):
    mpd = MPD()
    d = player.Daemon(mpd, [])
    control.submit(d, {"command": "seek", "offset": offset}, "mpris")
    asyncio.run(d.step(set()))
    assert [c for c in mpd.calls if c != "status"] == want
    assert "command seek from mpris:" in capsys.readouterr().err
