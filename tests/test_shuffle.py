"""mpd-player's shuffle module: lanes (familiar / rediscovery / new), cadence, skips, rests, priority 1, rounds,
owning MPD's random; and musicdb's per-file data for it."""
import asyncio, json, time

import pytest

from rormpc_tools import player
from rormpc_tools.player import shuffle, upnext
from test_upnext import QueueMPD

DAY = 86400


class PlayingMPD(QueueMPD):
    async def random(self, on):
        self.rand = bool(int(on))

    async def status(self):
        s = await super().status()
        s.update(elapsed="0", duration="200")
        return s

    async def playlistinfo(self):
        return [{"id": s["id"], "file": s["file"], "pos": str(i), **({"prio": str(s["prio"])} if s["prio"] else {})}
                for i, s in enumerate(self.q)]

    async def playlistid(self, id_):
        found = [x for x in await self.playlistinfo() if x["id"] == str(id_)]
        if not found:
            raise RuntimeError("[50@0] {playlistid} No such song")  # what MPD answers
        return found

    async def currentsong(self):
        return {"file": self.q[self.pos(self.cur)]["file"], "duration": "200"} if self.cur else {}

    async def next(self):
        self.calls.append(("next",))


class HeaviestRng:
    """Lanes in LANES order (familiar first); the weighted draw takes the heaviest candidate (first on ties)."""
    def shuffle(self, xs):
        pass

    def choices(self, pool, weights):
        return [pool[max(range(len(pool)), key=lambda i: (weights[i], -i))]]


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "rormpc"


@pytest.fixture
def clock(monkeypatch):
    t = [time.time()]
    monkeypatch.setattr(shuffle.time, "time", lambda: t[0])
    return t


def heard(plays, days_ago=1, **kw):
    return {"plays": plays, "heard": plays, "last": time.time() - days_ago * DAY, **kw}


def setup(files=("a", "b", "c", "d", "e", "f"), data=None, source=None, random=True, cycle=None):
    player.write_state("weights", {"version": 2, "generated": 0, "global_cadence": 10,
                                   "files": data if data is not None else {}})
    if source:
        player.write_state("source", {"source": source})
    if cycle:
        player.write_state("shuffle", {"cycle": cycle})
    mpd = PlayingMPD(list(files), random=random)
    mpd.cur, mpd.state = "1", "play"
    u, sh = upnext.UpNext(), shuffle.Shuffle(rng=HeaviestRng())
    d = player.Daemon(mpd, [u, sh])
    asyncio.run(u.start(d))
    asyncio.run(d.step({"player", "options", "playlist"}))
    return d, mpd, sh


def play(d, mpd, file):
    mpd.cur = mpd.q[[s["file"] for s in mpd.q].index(file)]["id"]
    mpd.q[mpd.pos(mpd.cur)]["prio"] = 0
    asyncio.run(d.step({"player"}))


def send(d, mpd, *msgs):
    mpd.messages = list(msgs)
    asyncio.run(d.step({"message"}))


def test_familiar_lane_prefers_plays_likes_and_overdue_songs():
    data = {"b": heard(30, days_ago=1, cadence=10), "c": heard(5, days_ago=20, cadence=10),
            "d": heard(5, days_ago=20, cadence=10, liked=True), "e": {}}
    d, mpd, sh = setup(data=data)
    # b: sqrt(31) * clamp(0.1) -> 5.6*0.2; c: sqrt(6)*2 = 4.9; d liked: x3 = 14.7
    assert sh.nominee["file"] == "d" and sh.nominee["lane"] == "familiar" and mpd.prio("d") == shuffle.PLAN_N
    assert "familiar: played 20 d ago, usually every 10 d, 5x, liked" in sh.nominee["why"]


def test_new_lane_has_a_daily_budget_and_lends_its_turn(clock):
    d, mpd, sh = setup(data={"b": heard(3, days_ago=400)}, cycle=["new"])
    assert sh.nominee["lane"] == "new" and sh.nominee["file"] == "c"
    sh.new_today = {"day": shuffle.today(), "n": shuffle.NEW_PER_DAY}  # budget spent
    send(d, mpd, "shuffle reroll")
    assert sh.nominee["lane"] == "rediscovery" and sh.nominee["file"] == "b"  # lent by new
    assert "(lent by new)" in sh.nominee["why"]


def test_early_skip_rests_the_song_and_lowers_its_weight(clock):
    d, mpd, sh = setup(data={"b": heard(9, days_ago=30), "c": heard(9, days_ago=30)})
    play(d, mpd, "b")
    clock[0] += 5  # five seconds later the user skips
    play(d, mpd, "c")
    now = clock[0]
    assert sh.live[-1] == {"t": round(now), "file": "b", "kind": "early"}
    assert sh.rests["b"] == pytest.approx(now + 48 * 3600)
    assert sh.skip_factor("b", now) == pytest.approx(0.6)
    assert sh.nominee["file"] != "b"


def test_a_song_played_to_the_end_rests_twelve_hours(clock):
    d, mpd, sh = setup(data={"b": heard(9), "c": heard(9)})
    play(d, mpd, "b")
    clock[0] += 195
    play(d, mpd, "c")
    assert sh.live[-1]["kind"] == "finished" and sh.rests["b"] == pytest.approx(clock[0] + 12 * 3600)


def test_pause_does_not_count_as_playing(clock):
    d, mpd, sh = setup(data={"b": heard(9), "c": heard(9)})
    play(d, mpd, "b")
    mpd.state = "pause"
    asyncio.run(d.step({"player"}))
    clock[0] += 500  # paused (e.g. the gap at 0:00 or the user)
    asyncio.run(d.step({"player"}))
    mpd.state = "play"
    play(d, mpd, "c")
    assert sh.live[-1]["kind"] == "early"


def test_new_song_skipped_early_on_two_days_is_quarantined(clock):
    d, mpd, sh = setup(data={"c": {"early": [clock[0] - 2 * DAY, clock[0] - 1 * DAY]}})
    assert sh.quarantined("c", clock[0])
    assert not sh.quarantined("d", clock[0])


def test_up_next_requests_are_not_nominated_and_stay_above():
    d, mpd, sh = setup(data={"c": heard(50)})
    send(d, mpd, "shuffle reroll", "upnext add c")
    assert mpd.prio("c") == 255
    assert sh.nominee and sh.nominee["file"] != "c" and mpd.prio(sh.nominee["file"]) == shuffle.PLAN_N


def test_heard_enough_cools_down_growing_and_skips_the_playing_song():
    d, mpd, sh = setup(data={"a": heard(30), "c": heard(2)})
    send(d, mpd, "shuffle heardenough a")
    assert ("next",) in mpd.calls
    first = sh.cooldown["a"]
    assert first["level"] == 0 and first["until"] == pytest.approx(time.time() + DAY, abs=5)
    send(d, mpd, "shuffle heardenough a")
    assert sh.cooldown["a"]["level"] == 1
    send(d, mpd, "shuffle unheardenough a")
    assert "a" not in sh.cooldown


def test_hits_round_plays_each_song_once_then_stops():
    d, mpd, sh = setup(files=("a", "b", "c"), data={"c": heard(3)}, source={"kind": "hits", "name": "80s", "len": 3})
    assert sh.round["source"] == "hits:80s"
    seen = ["a"]
    while sh.nominee:
        f = sh.nominee["file"]
        assert f not in seen
        seen.append(f)
        play(d, mpd, f)
    assert sorted(seen) == ["a", "b", "c"] and sh.round["done"]
    assert "round done" in player.read_state("shuffle")["reason"]
    sh.rests.clear()
    send(d, mpd, "shuffle newround")
    assert sh.nominee is not None and not sh.round["done"]


def test_it_owns_random_and_never_moves_songs():
    d, mpd, sh = setup(data={"e": heard(3)}, random=False)  # enabled with random off: it turns random on
    assert mpd.rand is True and sh.active and mpd.prio("e") == shuffle.PLAN_N
    assert mpd.files() == ["a", "b", "c", "d", "e", "f"]  # the queue's order is untouched


def test_off_turns_random_off_and_release_keeps_plain_random():
    d, mpd, sh = setup(data={"c": heard(3)})
    send(d, mpd, "shuffle off")
    assert mpd.rand is False and not sh.enabled and mpd.prio("c") == 0
    send(d, mpd, "shuffle on")
    assert mpd.rand is True and sh.enabled and sh.nominee
    send(d, mpd, "shuffle release")  # rormpc's x: plain random
    assert mpd.rand is True and not sh.enabled and sh.nominee is None
    asyncio.run(d.step({"options"}))
    assert not sh.enabled and mpd.rand is True


def test_random_turned_off_elsewhere_turns_it_off():
    d, mpd, sh = setup(data={"c": heard(3)})
    mpd.rand = False  # a phone
    asyncio.run(d.step({"options"}))
    assert not sh.enabled and sh.nominee is None and mpd.rand is False
    assert shuffle.Shuffle().enabled is False


def test_replaced_queue_drops_the_gone_nominee_and_starts_a_round():
    d, mpd, sh = setup(data={"c": heard(3)})
    old = sh.nominee["id"]
    player.write_state("source", {"source": {"kind": "hits", "name": "80s", "len": 2}})
    mpd.q = [{"id": "90", "file": "x", "prio": 0}, {"id": "91", "file": "y", "prio": 0}]
    mpd.cur = "90"
    asyncio.run(d.step({"playlist", "player"}))
    assert sh.nominee and sh.nominee["id"] != old and sh.nominee["file"] == "y"
    assert sh.round["source"] == "hits:80s" and sh.round["heard"] == ["x"]


def test_new_weights_drop_live_outcomes_they_cover(clock):
    d, mpd, sh = setup(data={"b": heard(3), "c": heard(3)})
    play(d, mpd, "b")
    clock[0] += 3
    play(d, mpd, "c")
    assert sh.live
    player.write_state("weights", {"version": 2, "generated": clock[0] + 1, "global_cadence": 10, "files": {}})
    sh._weights = (None, sh._weights[1])
    sh.data()
    assert sh.live == []


FILES12 = tuple("abcdefghijklmnopqrst")  # 20: enough left after rests, requests and the playing song


def test_plan_draws_ahead_in_play_order_and_only_the_head_has_priority():
    d, mpd, sh = setup(files=FILES12, data={f: heard(3) for f in FILES12})
    assert len(sh.plan) == shuffle.PLAN_N
    # the whole plan is published: plan[k] has priority PLAN_N - k, so MPD plays it in order
    assert [mpd.prio(e["file"]) for e in sh.plan] == list(range(shuffle.PLAN_N, 0, -1))
    assert "a" not in [e["file"] for e in sh.plan]  # not the playing song
    assert len({e["file"] for e in sh.plan}) == shuffle.PLAN_N  # no repeats
    assert player.read_state("shuffle")["plan"][0]["why"]


def test_head_plays_then_the_plan_moves_up_and_is_topped_up(state):
    d, mpd, sh = setup(files=FILES12, data={f: heard(3) for f in FILES12})
    first, second = sh.plan[0]["file"], sh.plan[1]["file"]
    lanes_before = len(sh.cycle) + len(sh.plan)
    play(d, mpd, first)
    assert sh.plan[0]["file"] == second and mpd.prio(second) == shuffle.PLAN_N and len(sh.plan) == shuffle.PLAN_N
    assert json.loads((state / "auto.jsonl").read_text().splitlines()[-1])["file"] == first
    assert (len(sh.cycle) + len(sh.plan)) % len(shuffle.LANES) == (lanes_before - 1) % len(shuffle.LANES)


def test_a_planned_song_played_by_hand_or_requested_leaves_the_plan():
    d, mpd, sh = setup(files=FILES12, data={f: heard(3) for f in FILES12})
    third = sh.plan[2]["file"]
    play(d, mpd, third)  # Enter on it
    assert third not in [e["file"] for e in sh.plan]
    fourth = sh.plan[3]["file"]
    send(d, mpd, f"upnext add {fourth}")
    assert fourth not in [e["file"] for e in sh.plan] and mpd.prio(fourth) == 255
    assert len(sh.plan) == shuffle.PLAN_N


def test_heard_enough_on_the_head_plans_again_without_it():
    d, mpd, sh = setup(files=FILES12, data={f: heard(3) for f in FILES12})
    head = sh.plan[0]["file"]
    send(d, mpd, f"shuffle heardenough {head}")
    assert head not in [e["file"] for e in sh.plan] and mpd.prio(head) == 0 and mpd.prio(sh.plan[0]["file"]) == shuffle.PLAN_N



def test_a_round_takes_only_the_snapshot_not_the_song_playing_at_the_switch():
    d, mpd, sh = setup(files=("x", "b", "c"), data={"c": heard(3)},
                       source={"kind": "hits", "name": "80s", "len": 2, "files": ["b", "c"]})
    mpd.cur = "1"  # x was playing when the source switched
    asyncio.run(d.step({"player"}))
    assert "x" not in [e["file"] for e in sh.plan] and sh.round["total"] == 2


def test_a_full_plan_kept_across_a_restart_gets_its_priorities():
    d, mpd, sh = setup(files=FILES12, data={f: heard(3) for f in FILES12})
    for s in mpd.q:
        s["prio"] = 0  # e.g. written by an older version that set only the head
    asyncio.run(d.step({"player"}))
    assert [mpd.prio(e["file"]) for e in sh.plan] == list(range(shuffle.PLAN_N, 0, -1))
