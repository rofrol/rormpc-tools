"""`mpd-player nowplaying`: macOS's Now Playing (Control Center, the lock screen, AirPods, keyboards whose media keys
macOS routes to the Now Playing app) for MPD. A second process beside the daemon, with its own launchd agent
(docs/media-keys-plan.md, section 2, option B), so PyObjC and Cocoa's run loop never reach the playback daemon.

- It shows what MPD plays: title, artist, album, duration, elapsed and rate, set on every player or queue change (no
  ticking: macOS extrapolates from elapsed and rate), and the cover (embedded, else the folder's, at most COVER_MAX
  bytes). Stopped clears it.
- Every remote command (toggle, play, pause, stop, next, previous, the scrubber) goes as one datagram to mpd-player's
  command socket, so it acts exactly like the media keys: Previous walks the weighted shuffle's trail, Next from a
  pause plays the next song. Nothing is sent to MPD from here. While mpd-player is down a command fails, and macOS is
  told so.
- Claiming Now Playing (decided 2026-10-10): nothing is shown and no command is taken until MPD is first seen
  playing after this process starts, so a paused MPD at login does not take the slot from a browser or Music; from
  then on it shows MPD's true state (paused stays paused).
- Threads: the main thread runs Cocoa (NSApplication as an accessory: no Dock icon) and every MediaPlayer call; one
  thread watches MPD with python-mpd2's asyncio client on its own plain asyncio loop and hands each snapshot to the
  main thread. A lost MPD connection ends the process (launchd starts it again); after the Mac wakes, the state is
  read again.
- Public MediaPlayer API only (MPNowPlayingInfoCenter, MPRemoteCommandCenter); no app bundle, no audio of its own.

`mpd-player nowplaying --check` only loads the frameworks and exits 0 (the installer runs it before it retires
another Now Playing provider).
"""
import asyncio, importlib, os, pathlib, sys, threading

from mpd.asyncio import MPDClient
from mpd.base import CommandError

from . import control, log

COVER_MAX = 4 * 1024 * 1024
# remote command -> transport command on the socket
REMOTE = {"toggle": "toggle", "play": "play", "pause": "pause", "stop": "stop", "next": "next", "previous": "prev"}
SUBSYSTEMS = ["player", "playlist"]


def _text(v):
    return ", ".join(v) if isinstance(v, list) else v


def info(status, song):
    """MPD's status and current song -> what Now Playing shows: {"state", "songid", "file", "title", "artist"?,
    "album"?, "duration"?, "elapsed", "rate"}; only {"state": "stop"} when nothing is playing or paused."""
    state = status.get("state")
    if state not in ("play", "pause") or not song:
        return {"state": "stop"}
    out = {"state": state, "songid": status.get("songid"), "file": song.get("file"),
           "title": _text(song.get("title")) or pathlib.PurePosixPath(song.get("file", "")).name,
           "artist": _text(song.get("artist")), "album": _text(song.get("album")),
           "elapsed": float(status.get("elapsed", 0)), "rate": 1.0 if state == "play" else 0.0}
    duration = status.get("duration") or song.get("duration") or song.get("time")
    if duration:
        out["duration"] = float(duration)
    return {k: v for k, v in out.items() if v is not None}


class Claim:
    """Nothing is shown until MPD is first seen playing; from then on every state. `first` is True for the view that
    claims."""

    def __init__(self):
        self.claimed = False
        self.first = False

    def view(self, shown):
        self.first = False
        if not self.claimed:
            if shown["state"] != "play":
                return None
            self.claimed = self.first = True
        return shown


def handler(remote, path=None, deliver=control.deliver):
    """A remote command -> a function that sends its transport command to the socket; True when it was sent."""
    command = REMOTE.get(remote, remote)

    def handle(position=None):
        why = deliver(command, position, path, sender="nowplaying")
        if why:
            log(f"nowplaying: {remote}: {why}")
        return why is None
    return handle


async def fetch_cover(mpd, file):
    """The song's embedded picture, else its folder's cover; None when there is none or it is over COVER_MAX."""
    for get in (mpd.readpicture, mpd.albumart):
        try:
            data = (await get(file)).get("binary")
        except CommandError:  # albumart: "No file exists"
            continue
        if data:
            return data if len(data) <= COVER_MAX else None
    return None


async def snapshot(mpd, cover):
    """(what to show, cover) from a fresh status; `cover` is (file, bytes) of the last one, fetched again only for
    another song, before the info is posted, so a cover never arrives for a song that is no longer current."""
    status = await mpd.status()
    song = await mpd.currentsong() if status.get("state") in ("play", "pause") else {}
    shown = info(status, song)
    f = shown.get("file")
    if f and f != cover[0]:
        cover = (f, await fetch_cover(mpd, f))
    if f and cover[1]:
        shown["cover"] = cover[1]
    return shown, cover


class Watcher(threading.Thread):
    """MPD on its own thread and asyncio loop: posts ("info", shown) after every change and refresh(), and
    ("lost", why) once when the connection ends."""

    def __init__(self, post, connect):
        super().__init__(name="mpd", daemon=True)
        self.post = post
        self.connect = connect  # async () -> a connected client
        self.loop = None
        self.wake = None

    def refresh(self):
        """Any thread: read MPD's state again (after the Mac wakes, macOS's extrapolated position is wrong)."""
        loop, wake = self.loop, self.wake
        if loop is not None:
            loop.call_soon_threadsafe(wake.set)

    def run(self):
        try:
            asyncio.run(self.watch())
        except BaseException as e:  # whatever ends the thread ends the process
            self.post("lost", f"{type(e).__name__}: {e or 'connection closed'}")

    async def watch(self):
        mpd = await self.connect()
        self.wake = asyncio.Event()
        self.loop = asyncio.get_running_loop()

        async def idle():
            try:
                async for _ in mpd.idle(SUBSYSTEMS):
                    self.wake.set()
            finally:
                self.wake.set()
        watcher = asyncio.create_task(idle())
        cover = (None, None)
        while True:
            self.wake.clear()  # before reading: a change after this sets it again
            shown, cover = await snapshot(mpd, cover)
            self.post("info", shown)
            await self.wake.wait()
            if watcher.done():
                watcher.result()  # the connection's error
                raise ConnectionError("MPD ended the idle")


async def connect_mpd():
    c = MPDClient()
    await c.connect(os.environ.get("MPD_HOST", "localhost"), int(os.environ.get("MPD_PORT", 6600)))
    return c


def main(a):
    if sys.platform != "darwin":
        print("mpd-player nowplaying is macOS's Now Playing; on Linux mpd-player itself is the MPRIS player",
              file=sys.stderr)
        return 2
    if a.check:
        for framework in ("AppKit", "MediaPlayer", "PyObjCTools.AppHelper"):
            importlib.import_module(framework)
        print("nowplaying: MediaPlayer and AppKit load")
        return 0
    return run_cocoa(a.socket)


def run_cocoa(path):
    import MediaPlayer as MP
    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSImage, NSWorkspace,
                        NSWorkspaceDidWakeNotification)
    from Foundation import NSMutableDictionary, NSOperationQueue
    from PyObjCTools import AppHelper

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    ok, failed = MP.MPRemoteCommandHandlerStatusSuccess, MP.MPRemoteCommandHandlerStatusCommandFailed
    states = {"play": MP.MPNowPlayingPlaybackStatePlaying, "pause": MP.MPNowPlayingPlaybackStatePaused}
    claim = Claim()
    exit_code = [0]
    art = [None, None]  # (cover bytes, MPMediaItemArtwork) of the song shown
    center = None  # MediaPlayer is not touched before the claim

    def register():
        commands = MP.MPRemoteCommandCenter.sharedCommandCenter()
        for remote, getter in (("toggle", "togglePlayPauseCommand"), ("play", "playCommand"),
                               ("pause", "pauseCommand"), ("stop", "stopCommand"), ("next", "nextTrackCommand"),
                               ("previous", "previousTrackCommand")):
            send = handler(remote, path)
            cmd = getattr(commands, getter)()
            cmd.setEnabled_(True)
            cmd.removeTarget_(None)
            cmd.addTargetWithHandler_(lambda event, send=send: ok if send() else failed)
        seek = handler("seek", path)
        cmd = commands.changePlaybackPositionCommand()
        cmd.setEnabled_(True)
        cmd.removeTarget_(None)
        cmd.addTargetWithHandler_(lambda event: ok if seek(float(event.positionTime())) else failed)
        for getter in ("changePlaybackRateCommand", "seekBackwardCommand", "seekForwardCommand",
                       "skipBackwardCommand", "skipForwardCommand"):
            getattr(commands, getter)().setEnabled_(False)

    def artwork(data):
        if art[0] is not data:
            img = NSImage.alloc().initWithData_(data)
            art[0] = data
            art[1] = None if img is None else MP.MPMediaItemArtwork.alloc().initWithBoundsSize_requestHandler_(
                img.size(), lambda size: img)
        return art[1]

    def show(view):
        if view["state"] == "stop":
            center.setNowPlayingInfo_(None)
            center.setPlaybackState_(MP.MPNowPlayingPlaybackStateStopped)
            return
        d = NSMutableDictionary.dictionary()
        d[MP.MPNowPlayingInfoPropertyMediaType] = MP.MPNowPlayingInfoMediaTypeAudio
        d[MP.MPMediaItemPropertyTitle] = view["title"]
        for key, prop in (("artist", MP.MPMediaItemPropertyArtist), ("album", MP.MPMediaItemPropertyAlbumTitle),
                          ("duration", MP.MPMediaItemPropertyPlaybackDuration)):
            if key in view:
                d[prop] = view[key]
        d[MP.MPNowPlayingInfoPropertyElapsedPlaybackTime] = view["elapsed"]
        d[MP.MPNowPlayingInfoPropertyPlaybackRate] = view["rate"]
        if view.get("cover") and (a := artwork(view["cover"])) is not None:
            d[MP.MPMediaItemPropertyArtwork] = a
        center.setNowPlayingInfo_(d)
        center.setPlaybackState_(states[view["state"]])

    def on_post(kind, payload):
        nonlocal center
        if kind == "lost":
            log(f"nowplaying: MPD connection lost ({payload}); exiting")
            exit_code[0] = 1
            AppHelper.stopEventLoop()
            return
        view = claim.view(payload)
        if view is None:
            return
        if claim.first:
            center = MP.MPNowPlayingInfoCenter.defaultCenter()
            register()
            log("nowplaying: MPD plays: showing it in Now Playing")
        show(view)

    watcher = Watcher(lambda kind, payload: AppHelper.callAfter(on_post, kind, payload), connect_mpd)
    NSWorkspace.sharedWorkspace().notificationCenter().addObserverForName_object_queue_usingBlock_(
        NSWorkspaceDidWakeNotification, None, NSOperationQueue.mainQueue(), lambda note: watcher.refresh())
    watcher.start()
    AppHelper.runConsoleEventLoop(installInterrupt=True)
    return exit_code[0]
