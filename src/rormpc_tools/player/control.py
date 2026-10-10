"""The command socket: media keys (Karabiner's `send_user_command`), scripts and `mpd-player send` reach the daemon
with transport commands, one datagram each, without starting a process, mpc or a DNS lookup per press.

- Path: `$XDG_RUNTIME_DIR/rormpc/player.sock` when XDG_RUNTIME_DIR is set (Linux), otherwise
  `$XDG_STATE_HOME/rormpc/player.sock` (`~/.local/state/rormpc/player.sock`); `mpd-player --socket PATH` overrides
  it and `mpd-player socket-path` prints it. A path too long for `sun_path` is never truncated.
- Access: the directory must be a real directory (not a symlink) owned by this user; its group/other bits are taken
  away (0700), the socket is bound under umask 077 and is 0600. Only the same user can send.
- One owner: the daemon holds an exclusive flock on `player.lock` beside the socket for its whole life, and only
  under it unlinks a leftover socket (never a regular file or a symlink) and binds. A second instance runs without
  the socket. Any failure is logged and the daemon runs without the socket; MPD's channel still works.
- Protocol: a JSON object `{"command": "next"}` (`"v": 1` implied; another `v` is refused), `seek` also takes
  `"position"` (seconds); unknown fields are ignored. A datagram that is not JSON and is exactly one command word
  (`next`, for socat) means the same. Anything else (a JSON string, a module command such as `gap set 5`, bad JSON,
  more than 4 KiB) is dropped with a rate-limited log line. At most 32 commands wait; beyond that the newest is
  dropped.
- Commands: next, prev, toggle, play, pause, stop, seek. Each runs in the daemon's step, before MPD channel messages
  and the status refresh, and reads a fresh MPD status first. Next and Previous from a pause (the user's, the gap's
  silence or "Pause for…") end up playing; the timers see the change and cancel themselves. Previous is
  `shuffle prev` (the trail, with its key-repeat debounce) when the shuffle module runs. play, pause and stop are
  idempotent; pause also ends the gap's silence, so the gap does not play on by itself.
- Fire and forget: nothing is answered (Karabiner's sender is unbound). Each command is logged with its source and
  the time from receipt to MPD's answer.
"""
import asyncio, errno, fcntl, json, math, os, pathlib, socket, stat, sys, time

from mpd.base import CommandError

from . import log

COMMANDS = ("next", "prev", "toggle", "play", "pause", "stop", "seek")
MAX_DATAGRAM = 4096
QUEUE_MAX = 32
# sizeof(sockaddr_un.sun_path), including the terminating NUL
SUN_PATH_MAX = 104 if sys.platform == "darwin" else 108
# delay: not a wait, a log rate limit: at most one "dropped" line per this many seconds, the drops in between are
# counted in the next line
DROP_LOG_INTERVAL = 10.0


def socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return pathlib.Path(runtime) / "rormpc" / "player.sock"
    base = os.environ.get("XDG_STATE_HOME") or str(pathlib.Path.home() / ".local/state")
    return pathlib.Path(base) / "rormpc" / "player.sock"


def parse(data):
    """A datagram -> {"command": ..., "position"?: seconds}; ValueError(why) when it is dropped."""
    if len(data) > MAX_DATAGRAM:
        raise ValueError(f"longer than {MAX_DATAGRAM} bytes")
    try:
        text = data.decode()
    except UnicodeDecodeError:
        raise ValueError("not UTF-8") from None
    try:
        obj = json.loads(text)
    except ValueError:
        word = text.strip()
        if word in COMMANDS and word != "seek":
            return {"command": word}
        raise ValueError(f"not a command: {text[:80]!r}") from None
    if not isinstance(obj, dict):
        raise ValueError(f"not a JSON object: {text[:80]!r}")
    v = obj.get("v", 1)
    if v != 1 or isinstance(v, bool):
        raise ValueError(f"unknown version {v!r}")
    command = obj.get("command")
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    if command != "seek":
        return {"command": command}
    position = obj.get("position")
    if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position) \
            or position < 0:
        raise ValueError(f"seek needs a position in seconds, got {position!r}")
    return {"command": "seek", "position": float(position)}


class DropLog:
    """Log lines for dropped datagrams, at most one per DROP_LOG_INTERVAL, so a stuck sender cannot fill the log."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.last = None
        self.suppressed = 0

    def note(self, msg):
        now = self.clock()
        if self.last is not None and now - self.last < DROP_LOG_INTERVAL:
            self.suppressed += 1
            return
        more = f" ({self.suppressed} more dropped since the last line)" if self.suppressed else ""
        log(f"command socket: {msg}{more}")
        self.last, self.suppressed = now, 0


class Socket:
    """The bound socket and its lock. `open()` returns False (and logs why) when the daemon runs without it."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.sock = None
        self.lock_fd = None
        self.ino = None

    def open(self):
        try:
            self._open()
            return True
        except (OSError, ValueError) as e:
            log(f"command socket {self.path}: {e}; running without it")
            self.close()
            return False

    def _open(self):
        if len(os.fsencode(self.path)) >= SUN_PATH_MAX:
            raise ValueError(f"path longer than {SUN_PATH_MAX - 1} bytes")
        d = self.path.parent
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)  # a symlink fails here
        try:
            st = os.fstat(fd)
            if st.st_uid != os.getuid():
                raise ValueError(f"{d} belongs to uid {st.st_uid}, not {os.getuid()}")
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o700)  # e.g. the state dir an older mpd-player created 0755
                log(f"command socket: {d} was {stat.S_IMODE(st.st_mode):o}, now 700")
        finally:
            os.close(fd)
        self.lock_fd = os.open(d / "player.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another mpd-player holds player.lock") from None
        try:
            st = os.lstat(self.path)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(st.st_mode):
                raise ValueError("something other than a socket is at that path")
            os.unlink(self.path)  # a leftover of a crashed daemon; we hold the lock
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        old = os.umask(0o077)
        try:
            self.sock.bind(os.fspath(self.path))
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        self.ino = os.lstat(self.path).st_ino
        self.sock.setblocking(False)

    def close(self):
        if self.ino is not None:
            try:
                if os.lstat(self.path).st_ino == self.ino:
                    os.unlink(self.path)
            except FileNotFoundError:
                pass
            self.ino = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        if self.lock_fd is not None:
            os.close(self.lock_fd)  # releases the flock
            self.lock_fd = None


class Receiver(asyncio.DatagramProtocol):
    """Runs on the daemon's loop: parse and queue only; the command itself runs in the daemon's step."""

    def __init__(self, daemon):
        self.daemon = daemon

    def datagram_received(self, data, addr):
        enqueue(self.daemon, data, "socket")

    def error_received(self, exc):
        log(f"command socket: {exc}")


def enqueue(d, data, source):
    try:
        cmd = parse(data)
    except ValueError as e:
        d.drops.note(f"dropped a {source} datagram: {e}")
        return
    if len(d.commands) >= QUEUE_MAX:
        d.drops.note(f"queue full ({QUEUE_MAX}), dropped {cmd['command']} from {source}")
        return
    d.commands.append((cmd, source, time.monotonic()))
    d.wake()


async def run_command(d, item):
    cmd, source, received = item
    name = cmd["command"]
    try:
        what = await TRANSPORT[name](d, cmd)
    except Exception as e:  # a failed command must not stop the daemon; a lost connection ends it in run()
        what = f"failed: {e}"
    ms = (time.monotonic() - received) * 1000
    log(f"command {name} from {source}: {what or 'done'} in {ms:.0f} ms")


async def _play_if_left_paused(d, before, moved):
    """Next and Previous from a pause play the song they moved to (decided 2026-10-10)."""
    after = await d.mpd.status()
    if before.get("state") in ("play", "pause") and after.get("state") == "pause" and moved(after):
        await d.mpd.play()
        return "played"
    return ""


async def _next(d, cmd):
    s = await d.mpd.status()
    await d.mpd.next()
    return await _play_if_left_paused(d, s, lambda after: True)


async def _prev(d, cmd):
    s = await d.mpd.status()
    sh = d.modules.get("shuffle")
    if sh is not None:
        await sh.on_message(d, "prev", "")  # the trail, or MPD's previous when the shuffle is not active
    else:
        await d.mpd.previous()
    return await _play_if_left_paused(d, s, lambda after: after.get("songid") != s.get("songid"))


async def _toggle(d, cmd):
    s = await d.mpd.status()
    if s.get("state") == "play":
        await d.mpd.pause(1)
        return "paused"
    await d.mpd.play()  # paused (also the gap's silence or "Pause for…") or stopped
    return "played"


async def _play(d, cmd):
    if (await d.mpd.status()).get("state") != "play":
        await d.mpd.play()
        return "played"
    return "already playing"


async def _pause(d, cmd):
    s = await d.mpd.status()
    gap = d.modules.get("gap")
    if gap is not None and gap.gap_until:
        gap.gap_until = None  # paused on purpose: the silence must not play on
        return "ended the gap's silence, paused"
    if s.get("state") == "play":
        await d.mpd.pause(1)
        return "paused"
    return "not playing"


async def _stop(d, cmd):
    if (await d.mpd.status()).get("state") != "stop":
        await d.mpd.stop()
        return "stopped"
    return "already stopped"


async def _seek(d, cmd):
    if (await d.mpd.status()).get("state") == "stop":
        raise CommandError("nothing is playing")
    await d.mpd.seekcur(f"{cmd['position']:.3f}")


TRANSPORT = {"next": _next, "prev": _prev, "toggle": _toggle, "play": _play, "pause": _pause, "stop": _stop,
             "seek": _seek}


def check(path=None):
    """`mpd-player socket-path --check`: whether a daemon is bound, found by connecting without sending anything."""
    path = pathlib.Path(path) if path else socket_path()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect(os.fspath(path))
    except FileNotFoundError:
        print(f"{path}: not bound (no socket)")
        return 1
    except ConnectionRefusedError:
        print(f"{path}: not bound (a leftover socket, nobody listening)")
        return 1
    except OSError as e:
        print(f"{path}: not bound ({e.strerror})")
        return 1
    finally:
        s.close()
    print(f"{path}: bound")
    return 0


def send(command, position=None, path=None):
    """`mpd-player send`: one canonical datagram, fire and forget. Returns the exit status."""
    path = pathlib.Path(path) if path else socket_path()
    payload = {"command": command}
    if command == "seek":
        payload["position"] = position
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.setblocking(False)
    try:
        s.sendto(json.dumps(payload).encode(), os.fspath(path))
    except FileNotFoundError:
        print(f"mpd-player is not running (no socket at {path})", file=sys.stderr)
        return 1
    except ConnectionRefusedError:
        print(f"mpd-player is not running (nobody bound at {path})", file=sys.stderr)
        return 1
    except OSError as e:
        if e.errno in (errno.ENOBUFS, errno.EAGAIN):
            print(f"mpd-player is not reading its socket {path} (buffer full)", file=sys.stderr)
            return 1
        raise
    finally:
        s.close()
    return 0
