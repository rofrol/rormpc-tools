"""`shuffle prev`: Previous walks back through the songs that really played, never MPD's own random order; leaving a
song that way is neutral; the move is logged for `musicdb import-skips`."""
import asyncio, json

import pytest

from rormpc_tools import musicdb, player
from rormpc_tools.player import shuffle
from test_shuffle import FILES12, PlayingMPD, clock, heard, play, send, setup, state  # noqa: F401 (fixtures)


@pytest.fixture(autouse=True)
def no_mpd_previous(monkeypatch):
    async def previous(self):
        self.calls.append(("previous",))
    monkeypatch.setattr(PlayingMPD, "previous", previous, raising=False)


def prev(d, mpd, clock, cmd=""):
    clock[0] += 1  # a separate press, not key repeat
    send(d, mpd, f"shuffle prev {cmd}".strip())


def playing(mpd):
    return mpd.q[mpd.pos(mpd.cur)]["file"]


def prev_log(state):
    p = state / "prev.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def walk(clock, files=FILES12, order=("b", "c", "d")):
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files})
    for f in order:
        clock[0] += 5
        play(d, mpd, f)
    return d, mpd, sh


def test_each_press_goes_one_song_further_back_without_mpd_previous(clock):
    d, mpd, sh = walk(clock)
    assert [e["file"] for e in sh.trail] == ["a", "b", "c", "d"]
    for want in ("c", "b", "a"):
        prev(d, mpd, clock)
        assert playing(mpd) == want
    prev(d, mpd, clock)  # the start of the history: nothing happens
    assert playing(mpd) == "a" and sh.cursor == 0
    assert ("previous",) not in mpd.calls
    assert [e["file"] for e in sh.trail] == ["a", "b", "c", "d"]  # songs reached with Previous are not added


def test_a_song_no_longer_in_the_queue_is_passed_over_and_not_re_added(clock):
    d, mpd, sh = walk(clock)
    mpd.q = [s for s in mpd.q if s["file"] != "c"]
    prev(d, mpd, clock)
    assert playing(mpd) == "b" and "c" not in mpd.files()


def test_forward_play_continues_the_trail_from_the_cursor(clock):
    d, mpd, sh = walk(clock)
    prev(d, mpd, clock)
    prev(d, mpd, clock)  # back at b
    clock[0] += 5
    play(d, mpd, "e")  # b ends, the next song plays
    assert [e["file"] for e in sh.trail] == ["a", "b", "e"] and sh.cursor is None
    prev(d, mpd, clock)
    assert playing(mpd) == "b"  # what really played before e


def test_key_repeat_is_debounced(clock):
    d, mpd, sh = walk(clock)
    prev(d, mpd, clock)
    send(d, mpd, "shuffle prev")  # the same instant: auto-repeat
    assert playing(mpd) == "c"


def test_leaving_a_song_with_previous_is_neutral(clock, state):
    d, mpd, sh = walk(clock)
    live, rests = list(sh.live), dict(sh.rests)
    clock[0] += 5  # d played 5 s: a skip, were it Next
    prev(d, mpd, clock)
    assert sh.live == live and sh.rests == rests and "d" not in sh.rests
    assert sh.skip_factor("d", clock[0]) == 1
    assert sh.history[-1]["file"] == "d" and sh.history[-1]["kind"] == "back"
    sh.recent.remove("d")  # not even when the soft "recently played" rule would allow it
    send(d, mpd, "shuffle reroll")
    assert "d" not in [e["file"] for e in sh.plan]  # the song left is not put back


def test_a_song_already_played_to_the_end_keeps_its_finished_outcome(clock):
    d, mpd, sh = walk(clock)
    clock[0] += 190
    prev(d, mpd, clock)
    assert sh.live[-1]["file"] == "d" and sh.live[-1]["kind"] == "finished"


def test_the_song_gone_back_to_leaves_the_plan_and_the_rest_keeps_its_order(clock):
    files = FILES12
    d, mpd, sh = setup(files=files, data={**{f: heard(3) for f in files}, "a": heard(50)})
    clock[0] += 5
    play(d, mpd, "b")
    sh.rests.pop("a")  # make the song gone back to a planned one
    sh.recent.remove("a")
    send(d, mpd, "shuffle reroll")
    assert sh.plan[0]["file"] == "a"
    rest = [e["file"] for e in sh.plan[1:]]
    prev(d, mpd, clock)
    assert playing(mpd) == "a"
    planned = [e["file"] for e in sh.plan]
    assert "a" not in planned and "b" not in planned and len(planned) == shuffle.PLAN_N
    assert planned[:len(rest)] == rest
    assert [mpd.prio(f) for f in planned] == list(range(shuffle.PLAN_N, 0, -1))
    assert not (player.state_dir() / "auto.jsonl").read_text().count('"a"')  # not one of the shuffle's own picks


def test_up_next_requests_stay_first(clock):
    d, mpd, sh = walk(clock)
    send(d, mpd, "upnext add t")
    prev(d, mpd, clock)
    assert mpd.prio("t") == 255 and mpd.prio(sh.plan[0]["file"]) == shuffle.PLAN_N


def test_each_move_is_logged_before_acting_then_confirmed(clock, state):
    d, mpd, sh = walk(clock)
    seen = []
    playid = mpd.playid

    async def logged_playid(id_):
        seen.append(prev_log(state))
        await playid(id_)
    mpd.playid = logged_playid
    prev(d, mpd, clock, "k1")
    intent = {"cmd": "k1", "t": round(clock[0], 3), "from": "d", "from_id": 4, "to": "c", "to_id": 3}
    assert seen == [[intent]]
    assert prev_log(state) == [intent, {"cmd": "k1", "result": "confirmed"}]


def test_a_move_whose_transition_is_not_observed_fails_and_counts_normally(clock, state):
    d, mpd, sh = walk(clock)

    async def refused(id_):
        raise RuntimeError("[50@0] {playid} No such song")
    mpd.playid = refused
    prev(d, mpd, clock, "k2")
    assert prev_log(state)[-1] == {"cmd": "k2", "result": "failed"} and sh.pending_prev is None
    clock[0] += 5
    play(d, mpd, "e")  # Next: d is an ordinary early skip
    assert sh.live[-1]["file"] == "d" and sh.live[-1]["kind"] == "early"


def test_off_uses_mpd_previous():
    d, mpd, sh = setup(data={"c": heard(3)})
    send(d, mpd, "shuffle off", "shuffle prev")
    assert ("previous",) in mpd.calls


def test_the_trail_survives_a_restart_without_a_duplicate(clock):
    d, mpd, sh = walk(clock)
    sh2 = shuffle.Shuffle(rng=sh.rng)
    d.modules["shuffle"] = sh2
    asyncio.run(d.step({"player"}))
    assert [e["file"] for e in sh2.trail] == ["a", "b", "c", "d"]


def test_import_skips_drops_the_skip_of_a_song_left_with_previous(env, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    log = tmp_path / "state" / "rormpc" / "prev.jsonl"
    log.parent.mkdir(parents=True)
    t = musicdb.epoch_of("2026-10-07T00:15:00")
    log.write_text("".join(json.dumps(r) + "\n" for r in [
        {"cmd": "1", "t": t, "from": "x.mp3", "from_id": 5, "to": "w.mp3", "to_id": 4},
        {"cmd": "1", "result": "confirmed"},
        {"cmd": "2", "t": t + 100, "from": "y.mp3", "from_id": 6, "to": "w.mp3", "to_id": 4},
        {"cmd": "2", "result": "failed"}]))
    musicdb.SKIPS_LOG.write_text("".join(json.dumps({"ts": ts, "file": f}) + "\n" for ts, f in [
        (t + 1.5, "x.mp3"),  # the Previous move: dropped
        (t + 1, "z.mp3"),  # another song
        (t + 3, "x.mp3"),  # outside +-2 s: a real skip
        (t + 100, "y.mp3")]))  # the move failed: a real skip
    musicdb.import_skips(None)
    got = sorted(musicdb.db().execute("SELECT ts, file FROM skips"))
    assert got == sorted([(musicdb.local_ts(t + 1), "z.mp3"), (musicdb.local_ts(t + 3), "x.mp3"),
                          (musicdb.local_ts(t + 100), "y.mp3")])
