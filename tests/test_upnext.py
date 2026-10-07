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


@pytest.mark.parametrize("random", [False, True])
@pytest.mark.parametrize("verb", ["play", "playnow"])
def test_failed_playid_keeps_request_and_reports_error(monkeypatch, random, verb):
    from mpd.base import CommandError

    d, mpd, u = setup(random=random)
    send(d, mpd, "upnext add c", "upnext add x")
    original = [e.copy() for e in u.entries]
    current, playing = u.current, u.playing
    playid = mpd.playid
    calls = []

    async def fail_playid(id_):
        calls.append(id_)
        raise CommandError("[50@0] {playid} No such song")

    monkeypatch.setattr(mpd, "playid", fail_playid)
    target = str(original[0]["id"]) if verb == "play" else original[0]["file"]
    send(d, mpd, f"upnext {verb} {target}")
    # send() also runs the next on_status with unchanged playback: it must not drop the failed request.
    assert u.entries == original
    assert u.current == current and u.playing == playing and mpd.cur == "1"
    saved = player.read_state("upnext")
    assert saved["entries"] == original and saved["playing"] == playing
    assert "Cannot play c" in saved["error"] and "No such song" in saved["error"]
    assert len(calls) == 1, "no automatic retry"
    asyncio.run(d.step({"player"}))
    assert u.entries == original and player.read_state("upnext")["error"] == saved["error"]
    monkeypatch.setattr(mpd, "playid", playid)
    send(d, mpd, f"upnext {verb} {target}")  # an explicit later user action, not an automatic retry
    assert u.playing == original[0] and u.entries == original[1:]
    assert player.read_state("upnext")["error"] is None


@pytest.mark.parametrize("random", [False, True])
def test_failed_new_playnow_keeps_added_song_waiting(monkeypatch, random):
    from mpd.base import CommandError

    d, mpd, u = setup(random=random)

    async def fail_playid(id_):
        raise CommandError("[50@0] {playid} No such song")

    monkeypatch.setattr(mpd, "playid", fail_playid)
    send(d, mpd, "upnext playnow x")
    assert u.entries == [{"id": 5, "file": "x", "added": True}]
    assert u.playing is None and u.current == 1 and mpd.cur == "1"
    assert "Cannot play x" in player.read_state("upnext")["error"]
    assert "x" in mpd.files(), "a failed start must not delete the added song"
    asyncio.run(d.step({"player"}))
    assert [e["file"] for e in u.entries] == ["x"], "a later wake must not treat a failed start as played"


def test_failed_play_preserves_previous_added_song(monkeypatch):
    from mpd.base import CommandError

    d, mpd, u = setup()
    send(d, mpd, "upnext playnow x", "upnext add c")
    previous, current = u.playing.copy(), u.current
    entries = [e.copy() for e in u.entries]

    async def fail_playid(id_):
        raise CommandError("[50@0] {playid} No such song")

    monkeypatch.setattr(mpd, "playid", fail_playid)
    send(d, mpd, f"upnext play {entries[0]['id']}")
    assert u.entries == entries and u.playing == previous and u.current == current
    assert "x" in mpd.files() and mpd.cur == str(current)
    assert not any(call == ("deleteid", str(current)) for call in mpd.calls)


def test_remove_then_play_stale_id_never_plays_neighbor():
    d, mpd, u = setup()
    send(d, mpd, "upnext add b", "upnext add c")
    target = u.entries[0]["id"]
    send(d, mpd, f"upnext remove {target}", f"upnext play {target}")
    assert mpd.cur == "1" and [e["file"] for e in u.entries] == ["c"]


def test_turning_random_on_keeps_the_waiting_entries():
    d, mpd, u = setup(random=False)
    send(d, mpd, "upnext add c")  # random off: moved after the current song, no priority
    mpd.rand = True
    asyncio.run(d.step({"options", "player"}))
    assert [e["file"] for e in u.entries] == ["c"] and mpd.prio("c") == 255


def skip_through(mpd, *ids):
    """`mpc next` several times before the daemon wakes: each song starts (MPD resets its priority) and is left."""
    for id_ in ids:
        mpd.cur = str(id_)
        mpd.q[mpd.pos(id_)]["prio"] = 0


def test_random_off_entries_skipped_past_between_wakes_count_as_played():
    d, mpd, u = setup(random=False)
    send(d, mpd, "upnext add c", "upnext add x")
    assert mpd.files()[:3] == ["a", "c", "x"] and mpd.prio("c") == 255 and mpd.prio("x") == 254
    c, x = (e["id"] for e in u.entries)
    skip_through(mpd, c, x, 2)  # c, x, then b
    asyncio.run(d.step({"player"}))
    assert u.entries == [] and u.playing is None
    assert "x" not in mpd.files() and "c" in mpd.files()


def test_random_off_jump_past_entries_keeps_them_waiting_after_the_new_song():
    d, mpd, u = setup(random=False)
    send(d, mpd, "upnext add c", "upnext add x")
    asyncio.run(mpd.playid(4))  # the user jumps to d: only d starts
    asyncio.run(d.step({"player"}))
    assert [e["file"] for e in u.entries] == ["c", "x"]
    assert mpd.files()[mpd.pos(mpd.cur):][:3] == ["d", "c", "x"]
    assert mpd.prio("c") == 255 and mpd.prio("x") == 254


def restart(mpd):
    """mpd-player starts again with its saved state against the same MPD."""
    u = upnext.UpNext()
    d = player.Daemon(mpd, [u])
    asyncio.run(u.start(d))
    asyncio.run(d.step({"player", "options", "playlist", "mixer"}))
    return d, u


@pytest.mark.parametrize("random", [False, True])
def test_entries_that_played_while_the_daemon_was_down_count_as_played(random):
    d, mpd, u = setup(random=random)
    send(d, mpd, "upnext add c", "upnext add x", "upnext add d")
    c, x, dd = (e["id"] for e in u.entries)
    skip_through(mpd, c, x)  # daemon down: c and x play, x is playing now
    d, u = restart(mpd)
    assert [e["file"] for e in u.entries] == ["d"] and u.playing["file"] == "x"
    assert mpd.prio("d") == 255
    skip_through(mpd, dd)
    asyncio.run(d.step({"player"}))
    assert "x" not in mpd.files() and u.playing["file"] == "d"


def test_a_saved_playing_entry_that_ended_while_down_is_finished():
    d, mpd, u = setup()
    send(d, mpd, "upnext playnow x")
    skip_through(mpd, 2)
    d, u = restart(mpd)
    assert u.playing is None and "x" not in mpd.files()


def test_state_from_before_marks_is_not_judged_once():
    d, mpd, u = setup(random=False)
    send(d, mpd, "upnext add c")
    saved = player.read_state("upnext")
    del saved["marked"]
    player.write_state("upnext", saved)
    mpd.q[mpd.pos(u.entries[0]["id"])]["prio"] = 0  # older versions gave no priority with random off
    d, u = restart(mpd)
    assert [e["file"] for e in u.entries] == ["c"] and mpd.prio("c") == 255
    assert player.read_state("upnext")["marked"] is True


def test_mpd_restart_keeps_refound_entries_waiting():
    d, mpd, u = setup()
    send(d, mpd, "upnext add c", "upnext add x")
    # MPD restarted: same queue, new ids; its state file brings the priorities back, c had started (0)
    mpd.q = [{"id": str(int(s["id"]) + 100), "file": s["file"], "prio": 0 if s["file"] == "c" else s["prio"]}
             for s in mpd.q]
    mpd.cur = "101"
    d, u = restart(mpd)
    assert [e["file"] for e in u.entries] == ["c", "x"]  # a replaced queue looks the same: not judged
    assert mpd.prio("c") == 255 and mpd.prio("x") == 254


def test_entry_starting_during_priority_writes_keeps_its_reset():
    d, mpd, u = setup()
    send(d, mpd, "upnext add c", "upnext add b")
    c = u.entries[0]["id"]
    prioid = mpd.prioid
    raced = []

    async def racing_prioid(prio, id_):
        if id_ == c and not raced:  # c starts after apply_order read the status, before c's write lands
            raced.append(id_)
            skip_through(mpd, c)
        await prioid(prio, id_)

    mpd.prioid = racing_prioid
    send(d, mpd, "upnext first " + str(u.entries[1]["id"]))
    assert raced and mpd.prio("c") == 0
    asyncio.run(d.step({"player"}))
    assert u.playing["file"] == "c" and [e["file"] for e in u.entries] == ["b"]
