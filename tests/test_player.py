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


# mute: volume 0 for a while, the old volume back at the deadline

class MixerMPD(FakeMPD):
    async def setvol(self, v):
        self.calls.append(("setvol", int(v)))
        self.st["volume"] = str(v)


def muted(status=None):
    from rormpc_tools.player import mute
    mpd = MixerMPD(status or {**PLAY, "volume": "88"})
    m = mute.Mute()
    d = player.Daemon(mpd, [m])
    run(m.start(d))
    return d, mpd, m


def send(d, mpd, *msgs):
    mpd.messages = list(msgs)
    return run(d.step({"message"}))


def test_mute_start_then_expiry_restores_the_volume(monkeypatch):
    d, mpd, m = muted()
    send(d, mpd, "mute start 300")
    st = player.read_state("mute")
    assert mpd.st["volume"] == "0" and st["volume"] == 88 and st["generation"] == 1 and st["error"] is None
    assert st["deadline"] == pytest.approx(time.time() + 300, abs=1)
    monkeypatch.setattr(time, "time", lambda: st["deadline"] + 0.1)
    assert run(d.step(set())) is True
    st = player.read_state("mute")
    assert mpd.st["volume"] == "88" and st["deadline"] is None and st["last"] == "expired"
    assert ("play",) not in mpd.calls  # expiry never starts playback


def test_mute_survives_a_restart():
    from rormpc_tools.player import mute
    d, mpd, m = muted()
    send(d, mpd, "mute start 60")
    again = mute.Mute()
    assert again.deadline() == m.deadline() and again.volume == 88


def test_a_volume_set_during_the_mute_cancels_and_is_kept(monkeypatch):
    d, mpd, m = muted()
    send(d, mpd, "mute start 60")
    mpd.st["volume"] = "40"  # mpc volume 40
    run(d.step({"mixer"}))
    assert m.deadline() is None and player.read_state("mute")["last"] == "overridden"
    later = time.time() + 3600
    monkeypatch.setattr(time, "time", lambda: later)
    run(d.step(set()))
    assert mpd.st["volume"] == "40"


def test_stop_unmutes_at_once_pause_does_not():
    d, mpd, m = muted()
    send(d, mpd, "mute start 60")
    mpd.st["state"] = "pause"
    run(d.step({"player"}))
    assert m.deadline() is not None and mpd.st["volume"] == "0"
    mpd.st["state"] = "stop"  # end of the queue
    run(d.step({"player"}))
    assert m.deadline() is None and mpd.st["volume"] == "88"
    assert player.read_state("mute")["last"] == "stopped"


def test_extend_unmute_and_cancel():
    d, mpd, m = muted()
    send(d, mpd, "mute start 60")
    first = m.deadline()
    send(d, mpd, "mute extend 300")
    assert m.deadline() == pytest.approx(first + 300)
    send(d, mpd, "mute cancel")  # forget the timer, stay muted
    assert m.deadline() is None and mpd.st["volume"] == "0"
    send(d, mpd, "mute start 60", "mute unmute")
    # still muted after cancel: a new start is refused (nothing to restore to), so unmute finds no mute
    assert m.deadline() is None and mpd.st["volume"] == "0"
    assert player.read_state("mute")["error"] == "not muted"


def test_unmute_restores_and_start_again_moves_the_deadline():
    d, mpd, m = muted()
    send(d, mpd, "mute start 60")
    send(d, mpd, "mute start 600")
    assert m.deadline() == pytest.approx(time.time() + 600, abs=1) and m.volume == 88
    send(d, mpd, "mute unmute")
    assert mpd.st["volume"] == "88" and player.read_state("mute")["last"] == "unmuted"


def test_mute_refusals_are_reported_in_the_state():
    d, mpd, m = muted({**PLAY, "state": "stop", "volume": "88"})
    send(d, mpd, "mute start 60")
    assert player.read_state("mute")["error"] == "nothing is playing" and mpd.calls == []
    d, mpd, m = muted({**PLAY, "volume": "-1"})
    send(d, mpd, "mute start 60")
    assert player.read_state("mute")["error"] == "MPD has no volume control"
    for bad in ["mute start 0", "mute start x", "mute extend 60", "mute frob"]:
        before = player.read_state("mute")["generation"]
        send(d, mpd, bad)
        st = player.read_state("mute")
        assert st["generation"] == before + 1 and st["error"]
    assert mpd.calls == []
