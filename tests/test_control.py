"""mpd-player's command socket: payload parsing, the socket's lifecycle (modes, lock, leftovers), the bounded queue,
ordering inside the daemon's step, and the transport commands against a fake MPD that answers like MPD 0.24."""
import asyncio, os, pathlib, shutil, socket, stat, tempfile, time

import pytest
from mpd.base import CommandError

from rormpc_tools import player
from rormpc_tools.player import control, gap, pause, shuffle


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return tmp_path


@pytest.fixture
def short():
    """pytest's tmp_path is too long for sun_path on macOS (104 bytes with rormpc/player.sock)."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="mps-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def run(coro):
    return asyncio.run(coro)


# parsing

@pytest.mark.parametrize("data, want", [
    (b'{"command":"next"}', {"command": "next"}),
    (b'{"v": 1, "command": "toggle", "extra": [1]}', {"command": "toggle"}),
    (b'{"v": 1, "command": "seek", "position": 83.5}', {"command": "seek", "position": 83.5}),
    (b'{"command": "seek", "position": 0}', {"command": "seek", "position": 0.0}),
    (b"next", {"command": "next"}),
    (b"prev\n", {"command": "prev"}),  # echo | socat
    (b'{"command": "prev", "from": "nowplaying"}', {"command": "prev", "from": "nowplaying"}),
    (b'{"command": "prev", "from": "someone"}', {"command": "prev"}),  # only known senders name themselves
])
def test_accepted_payloads(data, want):
    assert control.parse(data) == want


@pytest.mark.parametrize("data", [
    b'"next"',  # a JSON string, as Karabiner sends a string payload
    b"gap set 5", b'{"command": "gap set 5"}',  # module commands stay on MPD's channel
    b'{"command": "nosuch"}', b"nosuch", b"seek", b"next please",
    b'{"command": "next"', b"", b"\xff\xfe",
    b'{"v": 2, "command": "next"}', b'{"v": true, "command": "next"}',
    b'{"command": "seek"}', b'{"command": "seek", "position": "83"}', b'{"command": "seek", "position": -1}',
    b'{"command": "seek", "position": true}', b'{"command": "seek", "position": NaN}',
    b'{"command": "next", "pad": "' + b"x" * 5000 + b'"}',
])
def test_dropped_payloads(data):
    with pytest.raises(ValueError):
        control.parse(data)


def test_drop_log_is_rate_limited(capsys):
    t = [100.0]
    drops = control.DropLog(clock=lambda: t[0])
    for _ in range(50):
        drops.note("dropped x")
    t[0] += control.DROP_LOG_INTERVAL
    drops.note("dropped y")
    lines = capsys.readouterr().err.splitlines()
    assert lines == ["command socket: dropped x", "command socket: dropped y (49 more dropped since the last line)"]


# the socket's lifecycle

def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


def test_bind_makes_a_private_directory_and_socket_and_unlinks_on_close(short):
    path = short / "rormpc" / "player.sock"
    s = control.Socket(path)
    assert s.open()
    assert mode(path.parent) == 0o700 and mode(path) == 0o600 and stat.S_ISSOCK(os.lstat(path).st_mode)
    s.close()
    assert not path.exists()


def test_an_older_wide_state_directory_is_narrowed(short):
    d = short / "rormpc"
    d.mkdir()
    os.chmod(d, 0o755)
    (d / "gap.json").write_text("{}")
    s = control.Socket(d / "player.sock")
    assert s.open()
    assert mode(d) == 0o700 and (d / "gap.json").exists()
    s.close()


def test_a_symlinked_directory_is_refused(short):
    real = short / "real"
    real.mkdir(mode=0o700)
    (short / "rormpc").symlink_to(real)
    s = control.Socket(short / "rormpc" / "player.sock")
    assert not s.open()
    assert list(real.iterdir()) == []


def test_a_foreign_directory_is_refused(short, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: os.stat(short).st_uid + 1)
    s = control.Socket(short / "rormpc" / "player.sock")
    assert not s.open()
    assert not (short / "rormpc" / "player.sock").exists()


def test_a_leftover_socket_is_replaced(short):
    path = short / "rormpc" / "player.sock"
    path.parent.mkdir(mode=0o700)
    old = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    old.bind(str(path))
    old.close()  # a crashed daemon's file, nobody bound
    ino = os.lstat(path).st_ino
    s = control.Socket(path)
    assert s.open() and os.lstat(path).st_ino != ino
    s.close()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_something_else_at_the_path_is_refused_and_kept(short, kind):
    path = short / "rormpc" / "player.sock"
    path.parent.mkdir(mode=0o700)
    if kind == "file":
        path.write_text("mine")
    else:
        path.symlink_to(short / "elsewhere")
    s = control.Socket(path)
    assert not s.open()
    assert os.path.lexists(path) and (kind != "file" or path.read_text() == "mine")


def test_a_second_instance_keeps_its_hands_off_the_socket(short):
    path = short / "rormpc" / "player.sock"
    first = control.Socket(path)
    assert first.open()
    ino = os.lstat(path).st_ino
    second = control.Socket(path)
    assert not second.open()
    assert os.lstat(path).st_ino == ino  # not unlinked, not rebound
    first.close()
    assert second.open()  # the lock went with the first
    second.close()


def test_close_leaves_a_socket_someone_else_bound_since(short):
    path = short / "rormpc" / "player.sock"
    s = control.Socket(path)
    assert s.open()
    os.unlink(path)
    other = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    other.bind(str(path))
    s.close()
    assert path.exists()
    other.close()


def test_a_path_too_long_for_sun_path_is_refused_not_truncated(short):
    path = short / ("x" * control.SUN_PATH_MAX) / "player.sock"
    assert not control.Socket(path).open()
    assert not path.parent.exists()


def test_socket_path_follows_xdg(monkeypatch, state):
    assert control.socket_path() == state / "rormpc" / "player.sock"
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert control.socket_path() == pathlib.Path("/run/user/1000/rormpc/player.sock")


# receiving: datagrams into the daemon's queue

class Idle:
    async def status(self):
        return {"state": "stop"}


def test_datagrams_are_queued_in_order_and_wake_the_daemon(short):
    path = short / "rormpc" / "player.sock"
    s = control.Socket(path)
    assert s.open()

    async def main():
        d = player.Daemon(Idle(), [], s)
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: control.Receiver(d), sock=s.sock)
        try:
            assert control.send("next", path=path) == 0
            assert control.send("seek", 12.5, path=path) == 0
            snd = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            snd.sendto(b"gap set 5", str(path))  # dropped
            snd.sendto(b"toggle", str(path))
            snd.close()
            while len(d.commands) < 3:  # each queued datagram sets the wake event
                d._event.clear()
                await d._event.wait()
            return [c for c, _, _ in d.commands]
        finally:
            transport.close()

    assert run(main()) == [{"command": "next"}, {"command": "seek", "position": 12.5}, {"command": "toggle"}]
    s.close()


def test_a_sender_that_names_itself_is_the_commands_source():
    d = player.Daemon(Idle(), [])
    control.enqueue(d, b'{"command": "next", "from": "nowplaying"}', "socket")
    control.enqueue(d, b"next", "socket")
    assert [(c, src) for c, src, _ in d.commands] == [({"command": "next"}, "nowplaying"),
                                                      ({"command": "next"}, "socket")]


def test_the_queue_drops_the_newest_beyond_its_bound(capsys):
    d = player.Daemon(Idle(), [])
    for i in range(control.QUEUE_MAX + 5):
        control.enqueue(d, b"next" if i < control.QUEUE_MAX else b"prev", "socket")
    assert len(d.commands) == control.QUEUE_MAX
    assert {c["command"] for c, _, _ in d.commands} == {"next"}
    assert "queue full" in capsys.readouterr().err


def test_send_without_a_running_daemon_exits_1(short, capsys):
    path = short / "player.sock"
    assert control.send("next", path=path) == 1
    assert f"mpd-player is not running (no socket at {path})" in capsys.readouterr().err
    left = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    left.bind(str(path))
    left.close()
    assert control.send("next", path=path) == 1
    assert "mpd-player is not running" in capsys.readouterr().err


def test_check_tells_a_bound_socket_from_a_leftover(short, capsys):
    path = short / "rormpc" / "player.sock"
    assert control.check(path) == 1 and "not bound (no socket)" in capsys.readouterr().out
    s = control.Socket(path)
    assert s.open()
    assert control.check(path) == 0 and capsys.readouterr().out == f"{path}: bound\n"
    s.sock.close()  # a crashed daemon: the file stays, nobody listens
    s.sock = None
    assert control.check(path) == 1 and "nobody listening" in capsys.readouterr().out
    s.close()


def test_cli_send_needs_a_position_only_for_seek():
    assert player.parse_args(["send", "seek", "30"]).position == 30.0
    assert player.parse_args(["--socket", "/x.sock", "send", "next"]).socket == "/x.sock"
    assert player.parse_args([]).action is None
    for bad in (["send", "seek"], ["send", "next", "3"], ["send", "gap"]):
        with pytest.raises(SystemExit):
            player.parse_args(bad)


# the transport commands, against a fake MPD with MPD 0.24's answers (checked on a scratch MPD 0.24.15:
# next, previous and playid from a pause play; next at the last song stops; next while stopped is "Not playing")

class MPD:
    def __init__(self, state="play", songid="1", n=4):
        self.ids = [str(i + 1) for i in range(n)]
        self.st = {"state": state, "songid": songid, "single": "0", "repeat": "0", "elapsed": "10"}
        self.calls = []
        self.messages = []

    async def status(self):
        self.calls.append(("status",))
        s = dict(self.st)
        if s["state"] == "stop":
            s.pop("songid", None)
        return s

    async def _move(self, by, name):
        self.calls.append((name,))
        if self.st["state"] == "stop":
            raise CommandError("[2@0] {%s} Not playing" % name)
        i = self.ids.index(self.st["songid"]) + by
        if not 0 <= i < len(self.ids):
            self.st["state"] = "stop"
            return
        self.st.update(songid=self.ids[i], state="play", elapsed="0")

    async def next(self):
        await self._move(1, "next")

    async def previous(self):
        await self._move(-1, "previous")

    async def play(self):
        self.calls.append(("play",))
        self.st["state"] = "play"

    async def pause(self, v):
        self.calls.append(("pause", int(v)))
        self.st["state"] = "pause" if int(v) else "play"

    async def stop(self):
        self.calls.append(("stop",))
        self.st["state"] = "stop"

    async def seekcur(self, t):
        self.calls.append(("seekcur", t))

    async def single(self, v):
        self.calls.append(("single", v))
        self.st["single"] = v if v in ("oneshot", "1") else "0"

    async def readmessages(self):
        self.calls.append(("readmessages",))
        m, self.messages = self.messages, []
        return [{"channel": "rormpc", "message": x} for x in m]


def daemon(mpd, *modules):
    d = player.Daemon(mpd, list(modules))
    for m in modules:
        run(m.start(d))
    return d


def press(d, *payloads):
    for p in payloads:
        control.enqueue(d, p.encode(), "socket")
    return run(d.step(set()))


def actions(mpd):
    return [c for c in mpd.calls if c[0] != "status"]


def test_commands_run_in_order_before_messages_and_the_status_refresh_each_after_a_fresh_status():
    mpd = MPD()
    d = daemon(mpd)
    mpd.messages = ["nosuch thing"]
    for p in ("next", "toggle", "next"):
        control.enqueue(d, p.encode(), "socket")
    run(d.step({"message"}))
    assert mpd.calls[:7] == [("status",), ("next",), ("status",), ("status",), ("pause", 1), ("status",),
                             ("next",)]
    assert mpd.calls.index(("readmessages",)) > mpd.calls.index(("pause", 1))
    assert mpd.calls[-1] == ("status",) and not d.commands  # the step's own refresh comes last


def test_a_command_queued_while_the_step_runs_runs_in_the_same_step():
    mpd = MPD()
    d = daemon(mpd)
    real_next = mpd.next

    async def next_then_press():
        await real_next()
        if mpd.calls.count(("next",)) == 1:
            control.enqueue(d, b"stop", "socket")  # arrives while the first command runs
    mpd.next = next_then_press
    press(d, "next")
    assert ("stop",) in mpd.calls and not d.commands


@pytest.mark.parametrize("state, want_state, want_song", [("play", "play", "2"), ("pause", "play", "2"),
                                                          ("stop", "stop", None)])
def test_next_from_play_pause_and_stop(state, want_state, want_song, capsys):
    mpd = MPD(state=state)
    d = daemon(mpd)
    press(d, "next")
    s = run(mpd.status())
    assert s["state"] == want_state and s.get("songid") == want_song
    err = capsys.readouterr().err
    assert "command next from socket:" in err and " ms" in err
    if state == "stop":
        assert "failed" in err  # MPD's "Not playing", logged; the daemon goes on


def test_next_presses_play_if_mpd_ever_leaves_a_paused_player_paused():
    mpd = MPD(state="pause")

    async def next_stays_paused():
        mpd.calls.append(("next",))
        mpd.st["songid"] = "2"
    mpd.next = next_stays_paused
    press(daemon(mpd), "next")
    assert actions(mpd)[-1] == ("play",) and mpd.st["state"] == "play"


def gap_silence(mpd):
    g = gap.Gap(3)
    d = daemon(mpd, g, pause.Pause())
    run(d.step(set()))  # arms oneshot on song 1
    mpd.st.update(state="pause", songid="2", single="0", elapsed="0")  # MPD paused at 0:00 of the next song
    run(d.step({"player"}))
    assert g.deadline() is not None
    return d, g, g.deadline()


@pytest.mark.parametrize("key, song", [("next", "3"), ("prev", "1"), ("toggle", "2")])
def test_a_key_inside_the_gaps_silence_plays_and_the_timer_never_fires_later(key, song, monkeypatch):
    mpd = MPD()
    d, g, due = gap_silence(mpd)
    press(d, key)
    assert mpd.st["state"] == "play" and mpd.st["songid"] == song and g.deadline() is None
    n = mpd.calls.count(("play",))
    monkeypatch.setattr(time, "time", lambda: due + 1)  # a fake clock past the silence's old end
    run(d.step(set()))
    assert mpd.calls.count(("play",)) == n


def test_pause_inside_the_gaps_silence_ends_it_and_stays_paused():
    mpd = MPD()
    d, g, _ = gap_silence(mpd)
    press(d, "pause")
    assert g.deadline() is None and mpd.st["state"] == "pause"
    run(d.step({"player"}))
    assert g.deadline() is None and ("play",) not in mpd.calls


def paused_for_a_while(mpd):
    p = pause.Pause()
    d = daemon(mpd, gap.Gap(3), p)
    mpd.messages = ["pause start 600"]
    run(d.step({"message"}))
    assert p.active() and mpd.st["state"] == "pause"
    return d, p


@pytest.mark.parametrize("key, song", [("next", "2"), ("prev", "1"), ("toggle", "1"), ("play", "1")])
def test_a_key_inside_pause_for_a_while_plays_and_cancels_the_timer(key, song):
    mpd = MPD(songid="1" if key != "prev" else "2")
    d, p = paused_for_a_while(mpd)
    press(d, key)
    assert mpd.st["state"] == "play" and mpd.st["songid"] == song
    assert not p.active() and player.read_state("pause")["last"] == "overridden"


def test_prev_without_the_shuffle_is_mpds_previous_and_plays_from_a_pause():
    mpd = MPD(state="pause", songid="3")
    press(daemon(mpd), "prev")
    assert ("previous",) in mpd.calls and mpd.st["state"] == "play" and mpd.st["songid"] == "2"


@pytest.mark.parametrize("state, call, after", [("play", ("pause", 1), "pause"), ("pause", ("play",), "play"),
                                                ("stop", ("play",), "play")])
def test_toggle(state, call, after):
    mpd = MPD(state=state)
    press(daemon(mpd), "toggle")
    assert actions(mpd) == [call] and mpd.st["state"] == after


@pytest.mark.parametrize("key, state, want", [
    ("play", "play", []), ("play", "pause", [("play",)]), ("play", "stop", [("play",)]),
    ("pause", "play", [("pause", 1)]), ("pause", "pause", []), ("pause", "stop", []),
    ("stop", "play", [("stop",)]), ("stop", "pause", [("stop",)]), ("stop", "stop", []),
])
def test_play_pause_stop_are_idempotent(key, state, want):
    mpd = MPD(state=state)
    press(daemon(mpd), key)
    assert actions(mpd) == want


def test_seek_is_absolute_and_refused_when_stopped(capsys):
    mpd = MPD(state="pause")
    d = daemon(mpd)
    press(d, '{"command": "seek", "position": 83.5}')
    assert actions(mpd) == [("seekcur", "83.500")] and mpd.st["state"] == "pause"
    mpd.st["state"] = "stop"
    press(d, '{"command": "seek", "position": 1}')
    assert actions(mpd) == [("seekcur", "83.500")] and "nothing is playing" in capsys.readouterr().err


# prev through the weighted shuffle's trail

from test_shuffle import clock  # noqa: E402,F401 (fixture)
from test_shuffle_prev import no_mpd_previous, playing, prev_log, walk  # noqa: E402,F401 (fixtures)


def test_prev_goes_through_the_trail_with_its_debounce(clock, state):
    d, mpd, sh = walk(clock)
    clock[0] += 1
    press(d, "prev", "prev")  # the second one is key repeat
    assert playing(mpd) == "c" and ("previous",) not in mpd.calls
    clock[0] += shuffle.PREV_DEBOUNCE_S + 0.01
    press(d, '{"command": "prev"}')
    assert playing(mpd) == "b"
    results = [r["result"] for r in prev_log(state / "rormpc") if "result" in r]
    assert results == ["confirmed", "confirmed"]
