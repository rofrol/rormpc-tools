"""mpd-player's shuffle module: nomination with priority 1, weights, cooldowns, rounds."""
import asyncio, time

import pytest

from rormpc_tools import player
from rormpc_tools.player import shuffle, upnext
from test_upnext import QueueMPD


class PlayingMPD(QueueMPD):
    async def random(self, on):
        self.rand = bool(int(on))

    async def playlistinfo(self):
        return [{"id": s["id"], "file": s["file"], **({"prio": str(s["prio"])} if s["prio"] else {})} for s in self.q]

    async def playlistid(self, id_):
        found = [x for x in await self.playlistinfo() if x["id"] == str(id_)]
        if not found:
            raise RuntimeError("[50@0] {playlistid} No such song")  # what MPD answers
        return found

    async def currentsong(self):
        return {"file": self.q[self.pos(self.cur)]["file"]} if self.cur else {}

    async def next(self):
        self.calls.append(("next",))


class HeaviestRng:
    """No exploration; the weighted draw takes the heaviest candidate (first on ties)."""
    def random(self):
        return 0.99

    def choices(self, pool, weights):
        return [pool[max(range(len(pool)), key=lambda i: (weights[i], -i))]]

    def choice(self, pool):
        return pool[0]


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "rormpc"


def setup(files=("a", "b", "c", "d", "e", "f"), weights=None, source=None, random=True):
    if weights:
        player.write_state("weights", {"files": {f: {"w": w} for f, w in weights.items()}})
    if source:
        player.write_state("source", {"source": source})
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


def test_nominates_the_heaviest_with_priority_one():
    d, mpd, sh = setup(weights={"c": 3, "d": 2})
    assert sh.nominee["file"] == "c" and mpd.prio("c") == 1
    assert player.read_state("shuffle")["nominee"]["file"] == "c"


def test_after_the_nominee_plays_the_next_one_is_picked_and_recent_is_skipped():
    d, mpd, sh = setup(weights={"c": 3, "d": 2})
    play(d, mpd, "c")
    assert sh.nominee["file"] == "d" and "c" in sh.recent


def test_up_next_requests_are_not_nominated_and_stay_above():
    d, mpd, sh = setup(weights={"c": 3})
    send(d, mpd, "shuffle reroll", "upnext add c")
    assert mpd.prio("c") == 255
    assert sh.nominee and sh.nominee["file"] != "c" and mpd.prio(sh.nominee["file"]) == 1


def test_random_off_moves_the_pick_right_after_the_current_song():
    d, mpd, sh = setup(weights={"e": 3}, random=False)
    assert sh.active and sh.nominee["file"] == "e"
    assert mpd.files()[:2] == ["a", "e"] and mpd.prio("e") == 0  # plays next in queue order


def test_random_off_pick_goes_after_up_next_requests():
    d, mpd, sh = setup(weights={"e": 3}, random=False)
    send(d, mpd, "upnext add c")
    assert mpd.files()[:3] == ["a", "c", "e"]


def test_switching_random_picks_again_the_other_way():
    d, mpd, sh = setup(weights={"c": 3, "e": 2})
    assert mpd.prio("c") == 1
    mpd.rand = False
    asyncio.run(d.step({"options"}))
    assert mpd.prio("c") == 0  # its priority was taken back
    asyncio.run(d.step({"options"}))
    assert sh.nominee and mpd.files()[1] == sh.nominee["file"]


def test_heard_enough_cools_down_growing_and_skips_the_playing_song():
    d, mpd, sh = setup(weights={"a": 3, "c": 2})
    send(d, mpd, "shuffle heardenough a")
    assert ("next",) in mpd.calls
    first = sh.cooldown["a"]
    assert first["level"] == 0 and first["until"] == pytest.approx(time.time() + 86400, abs=5)
    send(d, mpd, "shuffle heardenough a")
    assert sh.cooldown["a"]["level"] == 1
    send(d, mpd, "shuffle unheardenough a")
    assert "a" not in sh.cooldown


def test_cooldown_excludes_from_picks():
    d, mpd, sh = setup(weights={"c": 3, "d": 2})
    send(d, mpd, "shuffle heardenough c")
    assert sh.nominee["file"] == "d" and mpd.prio("c") == 0


def test_hits_round_plays_each_song_once_then_stops():
    d, mpd, sh = setup(files=("a", "b", "c"), weights={"c": 3}, source={"kind": "hits", "name": "80s", "len": 3})
    assert sh.round["source"] == "hits:80s"
    seen = ["a"]
    while sh.nominee:
        f = sh.nominee["file"]
        assert f not in seen
        seen.append(f)
        play(d, mpd, f)
    assert sorted(seen) == ["a", "b", "c"] and sh.round["done"]
    assert "round done" in player.read_state("shuffle")["reason"]
    send(d, mpd, "shuffle newround")
    assert sh.nominee is not None and not sh.round["done"]


def test_off_withdraws_and_stays_off():
    d, mpd, sh = setup(weights={"c": 3})
    send(d, mpd, "shuffle off")
    assert sh.nominee is None and mpd.prio("c") == 0
    assert shuffle.Shuffle().enabled is False  # remembered


def test_musicdb_shuffle_weight():
    from rormpc_tools import musicdb
    assert musicdb.shuffle_weight(0, None) == 1
    assert musicdb.shuffle_weight(0, "2") == 2  # a like
    assert 1 < musicdb.shuffle_weight(5, "1") < musicdb.shuffle_weight(50, "1") <= 3
    assert musicdb.shuffle_weight(1000, "2") == 3  # capped
    assert musicdb.shuffle_weight(10, "0") == 0.25  # a dislike: rare


def test_replaced_queue_drops_the_gone_nominee_and_starts_a_round():
    d, mpd, sh = setup(weights={"c": 3})
    old = sh.nominee["id"]
    player.write_state("source", {"source": {"kind": "hits", "name": "80s", "len": 2}})
    mpd.q = [{"id": "90", "file": "x", "prio": 0}, {"id": "91", "file": "y", "prio": 0}]
    mpd.cur = "90"
    asyncio.run(d.step({"playlist", "player"}))
    assert sh.nominee and sh.nominee["id"] != old and sh.nominee["file"] == "y"
    assert sh.round["source"] == "hits:80s" and sh.round["heard"] == ["x"]


def test_turning_it_on_turns_random_off_and_random_on_turns_it_off():
    d, mpd, sh = setup(weights={"c": 3})  # random on
    send(d, mpd, "shuffle off")
    send(d, mpd, "shuffle on")
    assert mpd.rand is False and sh.enabled and sh.nominee
    mpd.rand = True  # e.g. x in rormpc, or a phone
    asyncio.run(d.step({"options"}))
    assert not sh.enabled and sh.nominee is None
    assert shuffle.Shuffle().enabled is False
