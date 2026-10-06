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
        return [{"id": s["id"], "file": s["file"], "pos": str(i), **({"prio": str(s["prio"])} if s["prio"] else {})}
                for i, s in enumerate(self.q)]

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
    assert musicdb.shuffle_weight(0, "2") == 2  # a like doubles
    assert musicdb.shuffle_weight(30, "1") == round(31 ** 0.75, 3)  # ~13: played songs clearly win
    assert musicdb.shuffle_weight(5, "1") < musicdb.shuffle_weight(50, "1")
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


def test_it_owns_random_and_never_moves_songs():
    d, mpd, sh = setup(weights={"e": 3}, random=False)  # enabled with random off: it turns random on
    assert mpd.rand is True and sh.active and mpd.prio("e") == 1
    assert mpd.files() == ["a", "b", "c", "d", "e", "f"]  # the queue's order is untouched


def test_off_turns_random_off_and_release_keeps_plain_random():
    d, mpd, sh = setup(weights={"c": 3})
    send(d, mpd, "shuffle off")
    assert mpd.rand is False and not sh.enabled and mpd.prio("c") == 0
    send(d, mpd, "shuffle on")
    assert mpd.rand is True and sh.enabled and sh.nominee
    send(d, mpd, "shuffle release")  # rormpc's x: plain random
    assert mpd.rand is True and not sh.enabled and sh.nominee is None
    asyncio.run(d.step({"options"}))
    assert not sh.enabled and mpd.rand is True


def test_random_turned_off_elsewhere_turns_it_off():
    d, mpd, sh = setup(weights={"c": 3})
    mpd.rand = False  # a phone
    asyncio.run(d.step({"options"}))
    assert not sh.enabled and sh.nominee is None and mpd.rand is False
    assert shuffle.Shuffle().enabled is False

