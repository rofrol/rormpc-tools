"""Instant filtering, stage 1b: a song is heard in a round by the scrobbler's own listen rule, read from
ro-listenbrainz-mpd's config.toml (listen_fraction, listen_max_seconds, listen_uninterrupted)."""
import asyncio, math, os

import pytest

from rormpc_tools.player import shuffle
from test_shuffle import PlayingMPD, clock, heard, play, setup, state  # noqa: F401 (fixtures)
from test_shuffle_source_id import ban_log, source  # noqa: F401 (fixture)

FILES = ("a", "b", "c")


class ElapsedMPD(PlayingMPD):
    """MPD whose elapsed is what the test set (songs are 200 s long)."""
    elapsed = 0.0

    async def status(self):
        s = await super().status()
        s["elapsed"] = str(self.elapsed)
        return s


@pytest.fixture
def config(tmp_path, monkeypatch):
    """The scrobbler's config in a temporary file; returns a function that writes its [submission] lines."""
    path = tmp_path / "listenbrainz-mpd" / "config.toml"
    path.parent.mkdir()
    monkeypatch.setattr(shuffle, "SCROBBLER_CONFIG", path)

    def write(text):
        path.write_text(text)
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # a new mtime even within one tick
    return write


def start():
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=source(FILES))
    mpd.__class__ = ElapsedMPD
    play(d, mpd, "b")
    return d, mpd, sh


def advance(d, mpd, clock, seconds):
    """Playback goes on for `seconds`, then MPD wakes the daemon."""
    clock[0] += seconds
    if mpd.state == "play":
        mpd.elapsed += seconds
    asyncio.run(d.step({"player"}))


def seek(d, mpd, to):
    mpd.elapsed = to
    asyncio.run(d.step({"player"}))


def test_the_three_settings_are_read(config):
    config("[submission]\nlisten_fraction = 0.9\nlisten_max_seconds = 0\nlisten_uninterrupted = true\n")
    assert shuffle.read_listen_rule(shuffle.SCROBBLER_CONFIG) == shuffle.ListenRule(0.9, None, True)
    config("[submission]\nlisten_fraction = 1\nlisten_max_seconds = 120\n")
    assert shuffle.read_listen_rule(shuffle.SCROBBLER_CONFIG) == shuffle.ListenRule(1.0, 120, False)


@pytest.mark.parametrize("text", [
    None,  # no file
    "not = [toml",
    "[mpd]\naddress = 'localhost:6600'\n",
    "submission = 3\n",
    "[submission]\nlisten_fraction = 0\nlisten_max_seconds = -5\nlisten_uninterrupted = 'yes'\n",
    "[submission]\nlisten_fraction = 1.5\nlisten_max_seconds = true\n",
    "[submission]\nlisten_fraction = '0.9'\nlisten_max_seconds = 30.5\n",
])
def test_a_missing_or_invalid_file_or_key_falls_back_to_the_scrobblers_defaults(config, text):
    if text is not None:
        config(text)
    assert shuffle.read_listen_rule(shuffle.SCROBBLER_CONFIG) == shuffle.ListenRule(0.5, 240, False)


def test_one_invalid_key_keeps_the_others(config):
    config("[submission]\nlisten_fraction = 2\nlisten_max_seconds = 60\nlisten_uninterrupted = true\n")
    assert shuffle.read_listen_rule(shuffle.SCROBBLER_CONFIG) == shuffle.ListenRule(0.5, 60, True)


def test_max_seconds_zero_means_no_cap_and_an_unknown_length_never_counts():
    rule = shuffle.ListenRule(0.9, None, False)
    assert shuffle.listen_needed(3600, rule) == pytest.approx(3240)
    assert shuffle.listen_needed(0, rule) == math.inf
    assert shuffle.listen_needed(3600, shuffle.ListenRule(0.9, 240, False)) == 240
    assert shuffle.listen_needed(0, shuffle.ListenRule(0.9, 240, False)) == 240


def test_the_rule_is_read_again_when_the_config_changes(config, clock):
    d, mpd, sh = start()
    advance(d, mpd, clock, 120)
    assert "b" in sh.round["heard"]  # the defaults: half of 200 s
    config("[submission]\nlisten_fraction = 0.9\nlisten_max_seconds = 0\n")
    mpd.elapsed = 0.0
    play(d, mpd, "c")
    advance(d, mpd, clock, 120)
    assert "c" not in sh.round["heard"]  # 0.9 of 200 s now
    advance(d, mpd, clock, 60)
    assert "c" in sh.round["heard"]


@pytest.mark.parametrize("uninterrupted", [False, True])
def test_a_seek_restarts_the_count_only_with_the_uninterrupted_rule(config, clock, uninterrupted):
    config(f"[submission]\nlisten_fraction = 0.5\nlisten_max_seconds = 0\n"
           f"listen_uninterrupted = {str(uninterrupted).lower()}\n")
    d, mpd, sh = start()
    advance(d, mpd, clock, 60)
    seek(d, mpd, 140)
    advance(d, mpd, clock, 45)  # 105 s played in all, 45 s since the seek
    assert ("b" in sh.round["heard"]) is not uninterrupted
    advance(d, mpd, clock, 10)  # 55 s since the seek, still not enough
    assert ("b" in sh.round["heard"]) is not uninterrupted


def test_a_pause_is_not_a_seek(config, clock):
    config("[submission]\nlisten_fraction = 0.5\nlisten_max_seconds = 0\nlisten_uninterrupted = true\n")
    d, mpd, sh = start()
    advance(d, mpd, clock, 60)
    mpd.state = "pause"
    advance(d, mpd, clock, 0)  # MPD's player event wakes the daemon at the pause
    advance(d, mpd, clock, 300)
    mpd.state = "play"
    advance(d, mpd, clock, 0)
    advance(d, mpd, clock, 39)
    assert "b" not in sh.round["heard"]  # 99 s played
    advance(d, mpd, clock, 1)
    assert "b" in sh.round["heard"]
