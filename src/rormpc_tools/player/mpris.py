"""MPRIS on Linux: mpd-player is `org.mpris.MediaPlayer2.mpd_player` on the session bus, so GNOME, KDE, playerctl and
the shells' media keys reach MPD through the daemon (docs/media-keys-plan.md, section 3).

- On by default whenever a session bus is there and dbus-fast is installed (it is, on Linux); `mpd-player --no-mpris`
  turns it off. Without a bus it logs once and runs without; the socket and MPD's channel still work.
- Every method goes through the same transport commands as the command socket (the queue, a fresh MPD status
  first): PlayPause is toggle, Play/Pause/Stop the idempotent ones (never a toggle), Previous walks the weighted
  shuffle's trail, Next from a pause plays, Seek is relative (before the start: the start; past the end: Next),
  SetPosition absolute and only for the track it names.
- Properties come from the daemon's status: PlaybackStatus, Metadata (`mpris:trackid` /org/mpd_player/song/<MPD song
  id>, title, artist list, album, length in microseconds), the Can* values from the real queue and state;
  PropertiesChanged whenever they change, Seeked when the same song is more than a second away from where the last
  status put it (a seek from anywhere, the song again). Position is extrapolated from the
  latest MPD status (D-Bus property reads cannot wait for MPD) and never signalled, as the spec says. Volume is
  read only: it is not a transport command.
- The name is requested with replacement allowed and queued: another player that takes it gets it, and the daemon
  gets it back when that one leaves (both logged). A lost bus connection is reconnected once, on that event.
- Another MPD MPRIS bridge (a bus name `org.mpris.MediaPlayer2.mpd...`: mpd-mpris, mpDris2, rmpcd) is warned about at
  start: two of them answer the same keys.
"""
import asyncio, importlib.util, pathlib, time

from . import Module, log
from .control import submit

BUS_NAME = "org.mpris.MediaPlayer2.mpd_player"
OBJECT_PATH = "/org/mpris/MediaPlayer2"
NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack"
OTHER_BRIDGES = "org.mpris.MediaPlayer2.mpd"
STATUS = {"play": "Playing", "pause": "Paused"}
# delay: not a wait, a tolerance: a position this far from where the last status said the song would be is a jump
# (a seek, the same song again), so Seeked is signalled; MPD's elapsed and the extrapolation differ by milliseconds
SEEK_TOLERANCE_US = 1_000_000
# D-Bus method -> transport command
METHODS = {"PlayPause": "toggle", "Play": "play", "Pause": "pause", "Stop": "stop", "Next": "next",
           "Previous": "prev"}
# D-Bus signatures (named, so the annotations of the interface below are plain names)
S, B, D, X, O, AS, ASV = "s", "b", "d", "x", "o", "as", "a{sv}"
META_TYPES = {"mpris:trackid": O, "mpris:length": X, "xesam:title": S, "xesam:artist": AS, "xesam:album": S,
              "xesam:url": S}


def trackid(songid):
    return f"/org/mpd_player/song/{songid}" if songid else NO_TRACK


def _list(v):
    return v if isinstance(v, list) else [v]


def _first(v):
    return v[0] if isinstance(v, list) else v


def duration(status, song):
    d = status.get("duration") or song.get("duration") or song.get("time")
    return float(d) if d else None


def metadata(status, song):
    """{key: plain value}; the bus layer wraps each in a Variant of META_TYPES[key]."""
    if status.get("state") not in STATUS or not song:
        return {"mpris:trackid": NO_TRACK}
    m = {"mpris:trackid": trackid(status.get("songid")),
         "xesam:title": _first(song.get("title")) or pathlib.PurePosixPath(song.get("file", "")).name}
    if song.get("artist"):
        m["xesam:artist"] = _list(song["artist"])
    if song.get("album"):
        m["xesam:album"] = _first(song["album"])
    if (length := duration(status, song)) is not None:
        m["mpris:length"] = int(length * 1e6)
    return m


def properties(status, song):
    """The player properties that are signalled when they change (Position is not)."""
    queued = int(status.get("playlistlength", 0)) > 0
    state = status.get("state")
    return {"PlaybackStatus": STATUS.get(state, "Stopped"), "Metadata": metadata(status, song),
            "CanGoNext": queued, "CanGoPrevious": queued, "CanPlay": queued, "CanPause": queued,
            "CanSeek": state in STATUS and duration(status, song) is not None,
            "Volume": max(0, int(status.get("volume", -1))) / 100}


def changed(old, new):
    return {k: v for k, v in new.items() if old.get(k) != v}


def position_us(status, at, now):
    """Microseconds into the song: the status's elapsed, plus the time since it was read while playing."""
    if status.get("state") not in STATUS:
        return 0
    elapsed = float(status.get("elapsed", 0))
    if status.get("state") == "play":
        elapsed += max(0.0, now - at)
    return int(elapsed * 1e6)


def seek_command(offset_us):
    return {"command": "seek", "offset": offset_us / 1e6}


def set_position_command(track, position, status, song):
    """SetPosition: an absolute seek, None when `track` is not the current song or the position is outside it."""
    if status.get("state") not in STATUS or track != trackid(status.get("songid")) or position < 0:
        return None
    length = duration(status, song)
    if length is not None and position > length * 1e6:
        return None
    return {"command": "seek", "position": position / 1e6}


def interfaces(mpris):
    """The two D-Bus interfaces over an Mpris module (dbus-fast is imported only here)."""
    from dbus_fast import PropertyAccess, Variant
    from dbus_fast.service import ServiceInterface, dbus_property, method, signal

    class Root(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2")

        @method()
        def Raise(self):
            pass

        @method()
        def Quit(self):
            pass

        @dbus_property(access=PropertyAccess.READ)
        def CanQuit(self) -> B:
            return False

        @dbus_property(access=PropertyAccess.READ)
        def CanRaise(self) -> B:
            return False

        @dbus_property(access=PropertyAccess.READ)
        def HasTrackList(self) -> B:
            return False

        @dbus_property(access=PropertyAccess.READ)
        def Identity(self) -> S:
            return "mpd-player"

        @dbus_property(access=PropertyAccess.READ)
        def SupportedUriSchemes(self) -> AS:
            return []

        @dbus_property(access=PropertyAccess.READ)
        def SupportedMimeTypes(self) -> AS:
            return []

    class Player(ServiceInterface):
        def __init__(self):
            super().__init__("org.mpris.MediaPlayer2.Player")

        def _run(self, cmd):
            if cmd is not None:
                submit(mpris.daemon, cmd, "mpris")

        @method()
        def Next(self):
            self._run({"command": METHODS["Next"]})

        @method()
        def Previous(self):
            self._run({"command": METHODS["Previous"]})

        @method()
        def Pause(self):
            self._run({"command": METHODS["Pause"]})

        @method()
        def PlayPause(self):
            self._run({"command": METHODS["PlayPause"]})

        @method()
        def Stop(self):
            self._run({"command": METHODS["Stop"]})

        @method()
        def Play(self):
            self._run({"command": METHODS["Play"]})

        @method()
        def Seek(self, offset: X):
            self._run(seek_command(offset))

        @method()
        def SetPosition(self, track: O, position: X):
            self._run(set_position_command(track, position, mpris.status, mpris.song))

        @method()
        def OpenUri(self, uri: S):
            log(f"mpris: OpenUri is not supported ({uri})")

        @signal()
        def Seeked(self, position) -> X:
            return position

        def _prop(self, name):
            return mpris.props.get(name, properties({}, {})[name])

        @dbus_property(access=PropertyAccess.READ)
        def PlaybackStatus(self) -> S:
            return self._prop("PlaybackStatus")

        @dbus_property(access=PropertyAccess.READ)
        def Rate(self) -> D:
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MinimumRate(self) -> D:
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def MaximumRate(self) -> D:
            return 1.0

        @dbus_property(access=PropertyAccess.READ)
        def Metadata(self) -> ASV:
            return wrap_metadata(self._prop("Metadata"))

        @dbus_property(access=PropertyAccess.READ)
        def Volume(self) -> D:
            return self._prop("Volume")

        @dbus_property(access=PropertyAccess.READ)
        def Position(self) -> X:
            return position_us(mpris.status, mpris.at, time.monotonic())

        @dbus_property(access=PropertyAccess.READ)
        def CanGoNext(self) -> B:
            return self._prop("CanGoNext")

        @dbus_property(access=PropertyAccess.READ)
        def CanGoPrevious(self) -> B:
            return self._prop("CanGoPrevious")

        @dbus_property(access=PropertyAccess.READ)
        def CanPlay(self) -> B:
            return self._prop("CanPlay")

        @dbus_property(access=PropertyAccess.READ)
        def CanPause(self) -> B:
            return self._prop("CanPause")

        @dbus_property(access=PropertyAccess.READ)
        def CanSeek(self) -> B:
            return self._prop("CanSeek")

        @dbus_property(access=PropertyAccess.READ)
        def CanControl(self) -> B:
            return True

    def wrap_metadata(m):
        return {k: Variant(META_TYPES[k], v) for k, v in m.items()}

    def emit(player, diff):
        if "Metadata" in diff:
            diff = dict(diff, Metadata=wrap_metadata(diff["Metadata"]))
        player.emit_properties_changed(diff)

    return Root(), Player(), emit


class Mpris(Module):
    name = "mpris"

    def __init__(self):
        self.daemon = None
        self.bus = None
        self.player = None
        self.emit = None
        self.status = {}
        self.song = {}
        self.at = 0.0  # time.monotonic() of self.status
        self.props = {}

    async def start(self, d):
        self.daemon = d
        if importlib.util.find_spec("dbus_fast") is None:
            log("mpris: dbus-fast is not installed; running without MPRIS")
            return
        await self.connect()

    async def connect(self):
        from dbus_fast import BusType, Message, NameFlag
        from dbus_fast.aio import MessageBus
        try:
            bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except Exception as e:  # no DBUS_SESSION_BUS_ADDRESS, nobody listening, ...
            log(f"mpris: no session bus ({e}); running without MPRIS")
            return False
        root, player, emit = interfaces(self)
        bus.export(OBJECT_PATH, root)
        bus.export(OBJECT_PATH, player)
        bus.add_message_handler(self._name_events)
        reply = await bus.request_name(BUS_NAME, NameFlag.ALLOW_REPLACEMENT)
        log(f"mpris: {BUS_NAME}: {reply.name.lower().replace('_', ' ')}")
        names = await bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                       interface="org.freedesktop.DBus", member="ListNames"))
        others = [n for n in (names.body[0] if names.body else []) if n.startswith(OTHER_BRIDGES) and n != BUS_NAME]
        if others:
            log(f"mpris: another MPD MPRIS bridge runs ({', '.join(others)}): both answer the media keys; stop it")
        self.bus, self.player, self.emit = bus, player, emit
        asyncio.get_running_loop().create_task(self._watch(bus))
        return True

    def _name_events(self, msg):
        if msg.member in ("NameLost", "NameAcquired") and msg.body and msg.body[0] == BUS_NAME:
            log(f"mpris: {msg.member} {BUS_NAME}")

    async def _watch(self, bus):
        try:
            await bus.wait_for_disconnect()
        except Exception as e:
            log(f"mpris: session bus connection lost ({type(e).__name__}: {e})")
        else:
            log("mpris: session bus connection lost")
        if self.bus is bus:
            self.bus = self.player = None
            self.props = {}
            if await self.connect():  # once, on this event
                await self.publish(self.status, self.song)

    async def on_status(self, d, s, changed_):
        if self.player is None:
            self.status, self.at = s, time.monotonic()
            return
        song = self.song
        if s.get("state") not in STATUS:
            song = {}
        elif s.get("songid") != self.status.get("songid") or "playlist" in changed_ or not song:
            song = await d.mpd.currentsong()
        await self.publish(s, song)

    async def publish(self, s, song):
        old, old_at = self.status, self.at
        self.status, self.song, self.at = s, song, time.monotonic()
        if self.player is None:
            return
        new = properties(s, song)
        diff = changed(self.props, new)
        self.props = new
        if diff:
            self.emit(self.player, diff)
        if s.get("songid") == old.get("songid") and s.get("state") in STATUS and old.get("state") in STATUS:
            now = position_us(s, self.at, self.at)
            if abs(now - position_us(old, old_at, self.at)) > SEEK_TOLERANCE_US:
                self.player.Seeked(now)
