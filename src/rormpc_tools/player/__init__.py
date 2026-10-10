"""mpd-player: the one playback daemon next to MPD, for what MPD has no setting for. It runs with or without
rormpc, so phones, media keys and mpc see the same behaviour.

  mpd-player [--seconds 3]   # runs until killed; installed and started by `rormpc_install.sh companions`
  mpd-player send next       # a transport command over the command socket (next prev toggle play pause stop,
                             # seek SECONDS); exits 1 when mpd-player is not running
  mpd-player socket-path [--check]   # where the command socket is (media keys: Karabiner's send_user_command);
                             # --check: whether a daemon is bound to it
  mpd-player nowplaying      # macOS: MPD in Now Playing (Control Center, AirPods, ...), a second process whose
                             # commands go to the socket; see nowplaying.py

Modules (one file each in this package) see every change of MPD's player, options, mixer and queue, can set a
wall-clock deadline (it survives the Mac sleeping past it) and take commands from clients over MPD's
client-to-client messages: channel "rormpc", one command per message, `<module> <verb> [args]`, e.g.
`mpc sendmessage rormpc "gap set 5"`. A module's state is `$XDG_STATE_HOME/rormpc/<module>.json`, written only by
this daemon (temp file + rename), read by rormpc to show it.

- gap: N seconds of silence between songs (`gap set N`, 0 = off; remembered across restarts).
- upnext: songs asked for with "Play next" play before the rest of the queue (`upnext add FILE`, ...).
- shuffle: with random on, the next song is drawn by weight (plays, likes) and nominated below Up next
  (`shuffle on|off`, `shuffle heardenough FILE`, ...); `shuffle prev` is Previous through the songs that really
  played, without counting a skip (send it instead of `mpc prev`).
- pause: pause for a while, MPD plays on at the deadline unless someone did anything meanwhile
  (`pause start SECONDS`, ...).

Transport commands (next, prev, toggle, play, pause, stop, seek) come over a datagram socket instead, one JSON
object per datagram (`{"command": "next"}`), e.g. from media keys; see control.py. On Linux the daemon is also the
MPRIS player `org.mpris.MediaPlayer2.mpd_player` when a session bus is there (`--no-mpris`: not); see mpris.py.
"""
import argparse, asyncio, collections, json, os, pathlib, signal, sys, time

from mpd.asyncio import MPDClient
from mpd.base import CommandError

CHANNEL = "rormpc"
SUBSYSTEMS = ["player", "options", "mixer", "playlist", "message"]
# longest sleep between wakes: asyncio's clock may stop while the Mac sleeps, so a wall-clock deadline that passed
# during sleep is noticed at most this late after waking
MAX_WAIT = 30.0


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local/state")
    return pathlib.Path(base) / "rormpc"


def read_state(name, default=None):
    try:
        return json.loads((state_dir() / f"{name}.json").read_text())
    except (OSError, ValueError):
        return default


def write_state(name, data):
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{name}.json.tmp"
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, d / f"{name}.json")  # atomic: rormpc never reads half a file


def log(msg):
    print(msg, file=sys.stderr, flush=True)


async def queue_entry(mpd, id_):
    """The queue entry with song id `id_` ({"id", "file", "prio"?...}), or None when it is gone (MPD answers
    "No such song" with an error)."""
    try:
        found = await mpd.playlistid(id_)
    except Exception:
        return None
    return found[0] if found else None


class Module:
    """Hooks, all optional. `d` is the Daemon: `d.mpd` is the MPD client, `d.status` the latest status."""
    name = ""

    async def start(self, d):
        pass

    async def on_status(self, d, status, changed):
        """After every wake, with the fresh status and the subsystems that changed since the last call."""

    async def on_message(self, d, verb, args):
        log(f"{self.name}: unknown command {verb!r}")

    def holds_pause(self):
        """True while this module keeps MPD paused and will play on itself: no other module may press play."""
        return False

    def deadline(self):
        """Wall-clock time (time.time()) at which on_timer should run, or None."""
        return None

    async def on_timer(self, d):
        pass


class Daemon:
    def __init__(self, mpd, modules, socket=None):
        from .control import DropLog
        self.mpd = mpd
        self.modules = {m.name: m for m in modules}
        self.status = {}
        self.socket = socket  # control.Socket, bound, or None
        self.commands = collections.deque()  # (command, source, receipt time) from the socket, run in step()
        self.drops = DropLog()
        self._pending = set()
        self._event = asyncio.Event()

    def wake(self):
        self._event.set()

    async def _watch(self):
        async for changed in self.mpd.idle(SUBSYSTEMS):
            self._pending.update(changed)
            self._event.set()

    async def dispatch(self, text):
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or parts[0] not in self.modules:
            log(f"ignored message {text!r}")
            return
        name, verb = parts[0], parts[1]
        args = parts[2] if len(parts) > 2 else ""
        try:
            await self.modules[name].on_message(self, verb, args)
        except Exception as e:  # a bad command must not stop the daemon
            log(f"{name} {verb}: {e}")

    async def step(self, changed):
        """One round: socket commands, messages, status to every module, due timers. True when a timer fired (read
        again)."""
        if self.commands:
            from .control import run_command
            while self.commands:  # a command queued meanwhile also runs now, in order
                await run_command(self, self.commands.popleft())
        if "message" in changed:
            for m in await self.mpd.readmessages():
                if m.get("channel") == CHANNEL:
                    await self.dispatch(m.get("message", ""))
        self.status = await self.mpd.status()
        for m in self.modules.values():
            await self.guarded(m, m.on_status(self, self.status, changed))
        fired = False
        now = time.time()
        for m in self.modules.values():
            due = m.deadline()
            if due is not None and due <= now:
                await self.guarded(m, m.on_timer(self))
                fired = True
        return fired

    def pause_held(self):
        return any(m.holds_pause() for m in self.modules.values())

    async def guarded(self, module, coro):
        """An MPD error in one module (a song that vanished meanwhile, ...) is logged; the others go on. A lost
        connection still ends the daemon, and launchd restarts it."""
        try:
            await coro
        except CommandError as e:
            log(f"{module.name}: {e}")

    def wait_time(self):
        dues = [d for m in self.modules.values() if (d := m.deadline()) is not None]
        if not dues:
            return MAX_WAIT
        return min(MAX_WAIT, max(0.0, min(dues) - time.time()))

    async def run(self):
        transport = None
        if self.socket is not None and self.socket.sock is not None:  # bound before the channel is subscribed
            from .control import Receiver
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: Receiver(self), sock=self.socket.sock)
        try:
            await self._run()
        finally:
            if transport is not None:
                transport.close()

    async def _run(self):
        await self.mpd.subscribe(CHANNEL)
        for m in self.modules.values():
            await m.start(self)
        watcher = asyncio.create_task(self._watch())
        changed = set(SUBSYSTEMS) - {"message"}
        while True:
            self._event.clear()  # before reading: a change after this sets it again
            changed |= self._pending
            self._pending = set()
            if await self.step(changed):
                changed = set()
                continue
            changed = set()
            try:
                await asyncio.wait_for(self._event.wait(), self.wait_time())
            except TimeoutError:
                pass
            if watcher.done():
                watcher.result()  # the connection is gone: raise, launchd restarts us


def parse_args(argv=None):
    from .control import COMMANDS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=3,
                    help="silence between songs until one is chosen with `gap set N` (then that is remembered)")
    ap.add_argument("--socket", metavar="PATH", help="the command socket (default: see `mpd-player socket-path`)")
    ap.add_argument("--no-mpris", action="store_true", help="Linux: do not register as an MPRIS player")
    sub = ap.add_subparsers(dest="action")
    path = sub.add_parser("socket-path", help="print the command socket's path")
    path.add_argument("--check", action="store_true", help="also whether a daemon is bound (exit 1 when not)")
    send = sub.add_parser("send", help="send one transport command to the running daemon (fire and forget)")
    send.add_argument("command", choices=COMMANDS)
    send.add_argument("position", nargs="?", type=float, help="seek: the position in seconds")
    np = sub.add_parser("nowplaying", help="macOS: show MPD in Now Playing, its commands to the running daemon")
    np.add_argument("--check", action="store_true", help="only load the frameworks it needs (exit 0 when they load)")
    a = ap.parse_args(argv)
    if a.action == "send" and (a.command == "seek") != (a.position is not None):
        ap.error("seek takes a position in seconds, the other commands none")
    return a


async def _main(a):
    from . import control, gap, pause, shuffle, upnext
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)  # unlink on stop
    sock = control.Socket(a.socket or control.socket_path())
    try:
        if not sock.open():
            sock = None
        c = MPDClient()
        await c.connect(os.environ.get("MPD_HOST", "localhost"), int(os.environ.get("MPD_PORT", 6600)))
        modules = [gap.Gap(a.seconds), upnext.UpNext(), shuffle.Shuffle(), pause.Pause()]
        if sys.platform != "darwin" and not a.no_mpris:  # macOS: `mpd-player nowplaying`, a process of its own
            from . import mpris
            modules.append(mpris.Mpris())
        await Daemon(c, modules, sock).run()
    finally:
        if sock is not None:
            sock.close()


def main():
    from . import control
    a = parse_args()
    if a.action == "socket-path":
        if a.check:
            sys.exit(control.check(a.socket))
        print(a.socket or control.socket_path())
        return
    if a.action == "send":
        sys.exit(control.send(a.command, a.position, a.socket))
    if a.action == "nowplaying":
        from . import nowplaying
        sys.exit(nowplaying.main(a))
    try:
        asyncio.run(_main(a))
    except asyncio.CancelledError:  # SIGTERM (launchctl/systemctl stop): the socket was unlinked
        pass


if __name__ == "__main__":
    main()
