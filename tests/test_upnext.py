"""mpd-player's upnext module against a fake async MPD that models the queue (ids, priorities, positions)."""
import asyncio

import pytest

from rormpc_tools import player
from rormpc_tools.player import upnext


class QueueMPD:
    def __init__(self, files, random=True, consume=False):
        self.q = [{"id": str(i + 1), "file": f, "prio": 0} for i, f in enumerate(files)]
        self.next_id = len(files) + 1
        self.cur = None
        self.state = "stop"
        self.rand = random
        self.consume = consume
        self.messages = []
        self.calls = []

    def pos(self, id_):
        return next(i for i, s in enumerate(self.q) if s["id"] == str(id_))

    async def status(self):
        s = {"state": self.state, "random": "1" if self.rand else "0", "consume": "1" if self.consume else "0",
             "repeat": "0", "single": "0"}
        if self.cur is not None:
            s.update(songid=self.cur, song=str(self.pos(self.cur)))
        return s

    async def playlistinfo(self):
        return [{"id": s["id"], "file": s["file"], "pos": str(i)} for i, s in enumerate(self.q)]

    async def playlistid(self, id_):
        found = [{"id": s["id"], "file": s["file"], "pos": str(i), **({"prio": str(s["prio"])} if s["prio"] else {})}
                 for i, s in enumerate(self.q) if s["id"] == str(id_)]  # MPD leaves out a priority of 0
        if not found:
            raise RuntimeError("[50@0] {playlistid} No such song")  # what MPD answers
        return found

    async def addid(self, file, pos=None):
        s = {"id": str(self.next_id), "file": file, "prio": 0}
        self.next_id += 1
        if pos is None:
            self.q.append(s)
        else:
            self.q.insert(self.pos(self.cur) + 1 + int(pos), s)
        self.calls.append(("addid", file, pos))
        return s["id"]

    async def playid(self, id_):
        self.cur, self.state = str(id_), "play"
        self.q[self.pos(id_)]["prio"] = 0  # MPD resets the priority of a song that starts

    async def play(self):
        self.state = "play"

    async def prioid(self, prio, id_):
        self.q[self.pos(id_)]["prio"] = prio

    async def moveid(self, id_, to):
        s = self.q.pop(self.pos(id_))
        self.q.insert(self.pos(self.cur) + 1 + int(to), s)

    async def deleteid(self, id_):
        self.calls.append(("deleteid", str(id_)))
        self.q.pop(self.pos(id_))

    async def readmessages(self):
        m, self.messages = self.messages, []
        return [{"channel": "rormpc", "message": x} for x in m]

    def files(self):
        return [s["file"] for s in self.q]

    def prio(self, file):
        return next(s["prio"] for s in self.q if s["file"] == file)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def setup(**kw):
    mpd = QueueMPD(["a", "b", "c", "d"], **kw)
    mpd.cur, mpd.state = "1", "play"
    u = upnext.UpNext()
    d = player.Daemon(mpd, [u])
    asyncio.run(u.start(d))
    asyncio.run(d.step({"player", "options", "playlist"}))
    return d, mpd, u


def send(d, mpd, *msgs):
    mpd.messages = list(msgs)
    asyncio.run(d.step({"message"}))


def advance(d, mpd, id_):
    mpd.cur = str(id_)
    asyncio.run(d.step({"player"}))


def test_random_on_priorities_in_order_and_added_song_leaves_after_playing():
    d, mpd, u = setup()
    send(d, mpd, "upnext add c", "upnext add x")
    assert mpd.prio("c") == 255 and mpd.prio("x") == 254
    assert [e["added"] for e in u.entries] == [False, True]
    x = u.entries[1]["id"]
    advance(d, mpd, mpd.q[mpd.pos(u.entries[0]["id"])]["id"])  # c plays
    advance(d, mpd, x)  # x plays
    advance(d, mpd, "2")  # then b: x has played and leaves the queue
    assert "x" not in mpd.files() and "c" in mpd.files()
    assert u.entries == [] and u.playing is None
    assert player.read_state("upnext")["entries"] == []


def test_asking_again_moves_to_top():
    d, mpd, u = setup()
    send(d, mpd, "upnext add b", "upnext add c", "upnext add c")
    assert [e["file"] for e in u.entries] == ["c", "b"]
    assert mpd.prio("c") == 255 and mpd.prio("b") == 254


def test_random_off_moves_after_current_in_order():
    d, mpd, u = setup(random=False)
    send(d, mpd, "upnext add d", "upnext add c")
    assert mpd.files()[:3] == ["a", "d", "c"]


def test_playnow_adds_after_current_then_removes_it():
    d, mpd, u = setup(random=False)
    send(d, mpd, "upnext playnow x")
    assert mpd.files()[:3] == ["a", "x", "b"] and mpd.files()[mpd.pos(mpd.cur)] == "x"
    advance(d, mpd, "2")  # source goes on with b
    assert mpd.files() == ["a", "b", "c", "d"]


def test_playnow_of_a_queued_song_plays_its_entry():
    d, mpd, u = setup()
    send(d, mpd, "upnext playnow c")
    assert mpd.cur == "3" and len(mpd.q) == 4


def test_remove_and_clear():
    d, mpd, u = setup()
    send(d, mpd, "upnext add b", "upnext add x", "upnext add c")
    send(d, mpd, f"upnext remove {u.entries[0]['id']}")
    assert mpd.prio("b") == 0 and "b" in mpd.files()
    send(d, mpd, "upnext clear")
    assert "x" not in mpd.files() and mpd.prio("c") == 0 and u.entries == []


def test_move_and_first():
    d, mpd, u = setup()
    send(d, mpd, "upnext add b", "upnext add c", "upnext add d")
    ids = [e["id"] for e in u.entries]
    send(d, mpd, f"upnext move {ids[0]} 2")
    assert [e["file"] for e in u.entries] == ["c", "d", "b"]
    send(d, mpd, f"upnext first {ids[2]}")
    assert [e["file"] for e in u.entries] == ["d", "c", "b"]
    assert mpd.prio("d") == 255


def test_consume_on_refuses():
    d, mpd, u = setup(consume=True)
    send(d, mpd, "upnext add b")
    assert u.entries == [] and "consume" in player.read_state("upnext")["error"]


def test_replaced_queue_keeps_requests_by_file():
    d, mpd, u = setup()
    send(d, mpd, "upnext add c", "upnext add x")
    mpd.q = [{"id": "50", "file": "c", "prio": 0}, {"id": "51", "file": "a", "prio": 0}]  # queue replaced
    mpd.cur = "51"
    asyncio.run(d.step({"playlist", "player"}))
    assert u.entries[0] == {"id": 50, "file": "c", "added": False}
    assert u.entries[1]["file"] == "x" and u.entries[1]["added"] and "x" in mpd.files()  # added again
    assert mpd.prio("c") == 255 and mpd.prio("x") == 254


def test_entries_skipped_past_between_wakes_count_as_played():
    d, mpd, u = setup()
    send(d, mpd, "upnext add c", "upnext add x")
    c, x = (e["id"] for e in u.entries)
    # three quick `next`s: c and x each start (MPD resets their priority) and are skipped before the daemon wakes
    for id_ in (c, x):
        mpd.q[mpd.pos(id_)]["prio"] = 0
    advance(d, mpd, "2")
    assert u.entries == [] and u.playing is None
    assert "x" not in mpd.files() and "c" in mpd.files()  # the added one leaves the queue, the source song stays
    assert player.read_state("upnext")["entries"] == []


def test_turning_random_on_keeps_the_waiting_entries():
    d, mpd, u = setup(random=False)
    send(d, mpd, "upnext add c")  # random off: moved after the current song, no priority
    mpd.rand = True
    asyncio.run(d.step({"options", "player"}))
    assert [e["file"] for e in u.entries] == ["c"] and mpd.prio("c") == 255
