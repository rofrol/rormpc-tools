"""mpd-player: the gap and pause modules' state machines and the message dispatcher, against a fake async MPD."""
import asyncio, time

import pytest

from rormpc_tools import player
from rormpc_tools.player import gap


class FakeMPD:
    def __init__(self, status):
        self.st = dict(status)
        self.calls = []
        self.messages = []

    async def status(self):
        return dict(self.st)

    async def single(self, v):
        self.calls.append(("single", v))
        self.st["single"] = "1" if v == "1" else ("oneshot" if v == "oneshot" else "0")

    async def play(self):
        self.calls.append(("play",))
        self.st["state"] = "play"

    async def readmessages(self):
        m, self.messages = self.messages, []
        return [{"channel": "rormpc", "message": x} for x in m]


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def daemon(status, seconds=3):
    mpd = FakeMPD(status)
    g = gap.Gap(seconds)
    d = player.Daemon(mpd, [g])
    run(g.start(d))
    return d, mpd, g


PLAY = {"state": "play", "songid": "1", "single": "0", "repeat": "0", "elapsed": "10"}


def test_arms_oneshot_while_playing():
    d, mpd, g = daemon(PLAY)
    run(d.step(set()))
    assert mpd.calls == [("single", "oneshot")] and g.armed_for == "1"


def test_pause_at_next_song_start_waits_then_resumes(monkeypatch):
    d, mpd, g = daemon(PLAY)
    run(d.step(set()))
    # MPD paused at 0:00 of song 2 and reset single
    mpd.st.update(state="pause", songid="2", single="0", elapsed="0")
    run(d.step({"player"}))
    assert g.deadline() == pytest.approx(time.time() + 3, abs=0.5)
    monkeypatch.setattr(time, "time", lambda: g.deadline() + 0.1)
    assert run(d.step(set())) is True
    assert mpd.calls[-1] == ("play",)


def test_user_pause_is_not_resumed():
    d, mpd, g = daemon(PLAY)
    run(d.step(set()))
    mpd.st.update(state="pause", elapsed="42")  # same song, single still oneshot
    run(d.step({"player"}))
    assert g.deadline() is None


def test_repeat_on_is_left_alone():
    d, mpd, g = daemon({**PLAY, "repeat": "1"})
    run(d.step(set()))
    assert mpd.calls == []


def test_gap_set_is_remembered_and_zero_disarms(state):
    d, mpd, g = daemon(PLAY)
    run(d.step(set()))
    mpd.messages = ["gap set 5"]
    run(d.step({"message"}))
    assert player.read_state("gap") == {"seconds": 5.0}
    assert gap.Gap(3).seconds == 5.0  # a restart keeps the chosen value
    mpd.messages = ["gap set 0"]
    run(d.step({"message"}))
    assert ("single", "0") in mpd.calls and g.armed_for is None
    n = len(mpd.calls)
    run(d.step({"player"}))
    assert len(mpd.calls) == n  # off: nothing armed again


def test_bad_commands_are_ignored():
    d, mpd, g = daemon(PLAY)
    mpd.messages = ["gap set 999", "nosuch thing", "gap", "gap frob 1"]
    run(d.step({"message"}))
    assert g.seconds == 3


def test_an_mpd_error_in_one_module_does_not_stop_the_others():
    from mpd.base import CommandError

    class Broken(player.Module):
        name = "broken"

        async def on_status(self, d, s, changed):
            raise CommandError("[50@0] {playlistid} No such song")

    mpd = FakeMPD(PLAY)
    g = gap.Gap(3)
    d = player.Daemon(mpd, [Broken(), g])
    run(d.step(set()))
    assert g.armed_for == "1"


# pause: MPD pauses for a while and plays on at the deadline

class PauseMPD(FakeMPD):
    async def pause(self, v):
        self.calls.append(("pause", int(v)))
        self.st["state"] = "pause" if int(v) else "play"

    async def setvol(self, v):
        self.calls.append(("setvol", int(v)))
        self.st["volume"] = str(v)


def paused(status=None, modules=()):
    from rormpc_tools.player import pause
    mpd = PauseMPD(status or PLAY)
    p = pause.Pause()
    d = player.Daemon(mpd, [*modules, p])
    for m in d.modules.values():
        run(m.start(d))
    return d, mpd, p


def send(d, mpd, *msgs):
    mpd.messages = list(msgs)
    return run(d.step({"message"}))


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def test_pause_start_then_expiry_plays_on(clock):
    d, mpd, p = paused()
    send(d, mpd, "pause start 300")
    st = player.read_state("pause")
    assert mpd.st["state"] == "pause" and mpd.calls == [("pause", 1)]
    assert st == {"deadline": 1300.0, "songid": "1", "generation": 1, "last": "started", "error": None}
    clock[0] = 1299.9
    assert run(d.step(set())) is False and mpd.st["state"] == "pause"
    clock[0] = 1300.1
    assert run(d.step(set())) is True
    st = player.read_state("pause")
    assert mpd.calls[-1] == ("pause", 0) and st["deadline"] is None and st["last"] == "expired"


def test_pause_start_while_paused_only_sets_the_timer(clock):
    d, mpd, p = paused({**PLAY, "state": "pause"})
    send(d, mpd, "pause start 60")
    assert mpd.calls == [] and p.deadline() == 1060.0


@pytest.mark.parametrize("user", [{"state": "play"}, {"state": "stop"}, {"songid": "7"}])
def test_anything_the_user_does_cancels_and_nothing_resumes_later(clock, user):
    d, mpd, p = paused()
    send(d, mpd, "pause start 60")
    mpd.st.update(user)  # mpc play / stop / next / a replaced queue
    run(d.step({"player"}))
    assert p.deadline() is None and player.read_state("pause")["last"] == "overridden"
    mpd.st["state"] = "pause"
    clock[0] += 3600
    run(d.step(set()))
    assert ("pause", 0) not in mpd.calls


def test_extend_resume_and_cancel(clock):
    d, mpd, p = paused()
    send(d, mpd, "pause start 60")
    send(d, mpd, "pause extend 300")
    assert p.deadline() == 1360.0
    send(d, mpd, "pause start 600")  # again: a new deadline from now
    assert p.deadline() == 1600.0 and mpd.calls == [("pause", 1)]
    send(d, mpd, "pause resume")
    assert mpd.st["state"] == "play" and player.read_state("pause")["last"] == "resumed"
    send(d, mpd, "pause start 60", "pause cancel")  # forget the timer, stay paused
    assert p.deadline() is None and mpd.st["state"] == "pause"
    clock[0] += 3600
    run(d.step(set()))
    assert mpd.st["state"] == "pause" and player.read_state("pause")["last"] == "cancelled"


def test_pause_refusals_are_reported_in_the_state():
    d, mpd, p = paused({**PLAY, "state": "stop"})
    send(d, mpd, "pause start 60")
    assert player.read_state("pause")["error"] == "nothing is playing" and mpd.calls == []
    for bad in ["pause start 0", "pause start x", "pause start 90000", "pause extend 60", "pause resume",
                "pause cancel", "pause frob"]:
        before = player.read_state("pause")["generation"]
        send(d, mpd, bad)
        st = player.read_state("pause")
        assert st["generation"] == before + 1 and st["error"]
    assert mpd.calls == []


def test_a_restart_keeps_the_timer_and_resumes_a_passed_deadline_only_if_still_paused(clock):
    from rormpc_tools.player import pause
    d, mpd, p = paused()
    send(d, mpd, "pause start 60")
    clock[0] += 600  # the daemon was down past the deadline
    again = pause.Pause()
    assert again.deadline() == 1060.0
    d2 = player.Daemon(mpd, [again])
    run(again.start(d2))
    assert run(d2.step(set())) is True and mpd.calls[-1] == ("pause", 0)
    assert player.read_state("pause")["last"] == "expired"

    send(d2, mpd, "pause start 60")
    mpd.st["state"] = "play"  # someone pressed play while the daemon was down
    clock[0] += 600
    third = pause.Pause()
    d3 = player.Daemon(mpd, [third])
    run(third.start(d3))
    n = len(mpd.calls)
    assert run(d3.step(set())) is False and len(mpd.calls) == n
    assert player.read_state("pause")["last"] == "overridden"


def test_a_leftover_mute_json_restores_its_volume_once(state):
    player.write_state("mute", {"deadline": 123.0, "volume": 77, "generation": 3, "last": "started", "error": None})
    d, mpd, p = paused({**PLAY, "volume": "0"})
    assert mpd.calls == [("setvol", 77)] and not (state / "rormpc" / "mute.json").exists()
    paused({**PLAY, "volume": "0"})  # once: the file is gone
    player.write_state("mute", {"deadline": None, "volume": 77})  # timer cancelled, stayed muted on purpose
    d, mpd, p = paused({**PLAY, "volume": "0"})
    assert mpd.calls == [] and not (state / "rormpc" / "mute.json").exists()
    player.write_state("mute", {"deadline": 123.0, "volume": 77})
    d, mpd, p = paused({**PLAY, "volume": "40"})  # someone set a volume since: kept
    assert mpd.calls == []


def test_the_gap_never_plays_inside_a_timed_pause_and_arms_again_after(clock):
    """A timed pause started during the gap's silence holds: the gap's deadline is dropped, the pause plays on at
    its own deadline, and the next song ends in a gap again."""
    g = gap.Gap(10)
    d, mpd, p = paused({**PLAY, "volume": "70"}, [g])
    run(d.step(set()))  # gap arms oneshot
    mpd.st.update(state="pause", songid="2", single="0", elapsed="0")  # song 1 ended: the gap's silence
    run(d.step({"player"}))
    assert g.deadline() == 1010.0
    clock[0] += 2
    send(d, mpd, "pause start 60")
    assert g.deadline() is None and p.deadline() == 1062.0
    clock[0] += 30  # past the gap's deadline
    assert run(d.step(set())) is False and ("play",) not in mpd.calls and mpd.st["state"] == "pause"
    clock[0] = 1062.5
    assert run(d.step(set())) is True and mpd.calls[-1] == ("pause", 0)
    run(d.step(set()))
    assert mpd.calls[-1] == ("single", "oneshot") and g.armed_for == "2"  # the gap is not lost
    assert ("play",) not in mpd.calls


def test_a_pause_asked_in_the_same_wake_as_the_gaps_silence_holds(clock):
    """The message is handled before the modules see the status: the gap must not take MPD's pause at 0:00 for its
    own silence, and a cancelled timer stays paused instead of being resumed by the gap."""
    g = gap.Gap(10)
    d, mpd, p = paused(PLAY, [g])
    run(d.step(set()))
    mpd.st.update(state="pause", songid="2", single="0", elapsed="0")
    mpd.messages = ["pause start 60"]
    run(d.step({"player", "message"}))
    assert g.deadline() is None and p.deadline() == 1060.0 and p.songid == "2"
    send(d, mpd, "pause cancel")
    clock[0] += 3600
    run(d.step({"player"}))
    assert mpd.st["state"] == "pause" and ("play",) not in mpd.calls and g.deadline() is None


def test_play_pressed_during_a_timed_pause_keeps_the_gap(clock):
    g = gap.Gap(10)
    d, mpd, p = paused(PLAY, [g])
    run(d.step(set()))
    send(d, mpd, "pause start 60")
    mpd.st.update(state="play", single="0")  # a phone pressed play; single was reset meanwhile
    run(d.step({"player"}))
    assert p.deadline() is None and mpd.calls[-1] == ("single", "oneshot") and g.armed_for == "1"


def test_gap_resumes_only_at_its_deadline_and_arms_again(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    d, mpd, g = daemon(PLAY)
    run(d.step(set()))
    mpd.st.update(state="pause", songid="2", single="0", elapsed="0")
    run(d.step({"player"}))
    assert g.deadline() == 1003.0
    clock[0] += 2.9
    run(d.step(set()))
    assert mpd.calls == [("single", "oneshot")]  # still silent
    clock[0] += 0.2
    run(d.step(set()))
    assert mpd.calls[-1] == ("play",) and g.deadline() is None
    run(d.step({"player"}))
    assert mpd.calls[-1] == ("single", "oneshot") and g.armed_for == "2"  # the next song ends in a gap too


@pytest.mark.parametrize("user", [{"state": "play"}, {"songid": "3"}, {"state": "stop"}])
def test_anything_the_user_does_during_the_silence_cancels_it(monkeypatch, user):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    d, mpd, g = daemon(PLAY)
    run(d.step(set()))
    mpd.st.update(state="pause", songid="2", single="0", elapsed="0")
    run(d.step({"player"}))
    assert g.deadline() is not None
    mpd.st.update(user)
    run(d.step({"player"}))
    assert g.deadline() is None
    clock[0] += 10
    run(d.step(set()))
    assert ("play",) not in mpd.calls
