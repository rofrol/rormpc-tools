"""mpd-player: the gap module's state machine and the message dispatcher, against a fake async MPD."""
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
