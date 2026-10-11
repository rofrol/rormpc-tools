"""Instant filtering, stage 1 (decided 2026-10-11): rounds belong to source.json's `source_id`, a song is heard in a
round only after a scrobble-qualifying play, and banned songs are never planned."""
import asyncio

import pytest

from rormpc_tools import banned, player
from rormpc_tools.player import shuffle
from test_shuffle import clock, heard, listen, play, send, setup, state  # noqa: F401 (fixtures)

FILES = tuple("abcdefgh")


@pytest.fixture(autouse=True)
def ban_log(tmp_path, monkeypatch):
    monkeypatch.setattr(banned, "DATA", tmp_path / "music-data")
    return tmp_path / "music-data" / "banned.jsonl"


def source(files, **kw):
    return {"kind": "hits", "name": "Music", "len": len(files), "files": list(files), "rules_hash": "h1",
            "source_id": "6f1c0a52-3b7e-4d55-9d1e-0f6a7b3c2d10", "generation": 1, **kw}


def write_source(src):
    player.write_state("source", {"source": src})


def planned(sh):
    return [e["file"] for e in sh.plan]


def test_a_filter_change_keeps_the_round_new_members_join_unheard_and_leavers_stay_heard(clock):
    src = source(("b", "c", "d"))
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=src)
    assert sh.round["source"] == f"hits:id:{src['source_id']}" and sh.round["total"] == 3
    play(d, mpd, "b")
    listen(d, clock)
    assert sh.round["heard"] == ["b"]
    # a filter click: other rules, other members, the next generation; the source is the same
    write_source(source(("c", "d", "e"), rules_hash="h2", generation=2))
    asyncio.run(d.step({"playlist"}))
    assert sh.round["number"] == 1 and sh.round["heard"] == ["b"] and sh.round["total"] == 3
    assert set(planned(sh)) <= {"c", "d", "e"} and "e" in planned(sh)  # e joined the round unheard
    # b comes back with the next click: it was heard in this round, so it does not play again at once
    write_source(source(("b", "c", "d", "e"), rules_hash="h3", generation=3))
    asyncio.run(d.step({"playlist"}))
    assert sh.round["heard"] == ["b"] and "b" not in [e["file"] for e in sh.plan if e.get("round") == 0]


def test_another_source_id_starts_a_new_round(clock):
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=source(("b", "c")))
    play(d, mpd, "b")
    listen(d, clock)
    write_source(source(("b", "c"), source_id="0d5e2f6a-1111-4c3b-8a77-5e2b9c4d1a00"))
    asyncio.run(d.step({"playlist"}))
    assert sh.round["source"] == "hits:id:0d5e2f6a-1111-4c3b-8a77-5e2b9c4d1a00" and sh.round["heard"] == []


def test_without_source_id_the_rules_hash_keys_the_round_as_before(clock):
    src = {k: v for k, v in source(("b", "c")).items() if k not in ("source_id", "generation")}
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=src)
    assert sh.round["source"] == "hits:h1"
    play(d, mpd, "b")
    listen(d, clock)
    write_source({**src, "rules_hash": "h2"})
    asyncio.run(d.step({"playlist"}))
    assert sh.round["source"] == "hits:h2" and sh.round["heard"] == []


def test_a_song_is_heard_only_after_a_scrobble_qualifying_play(clock):
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=source(FILES))
    play(d, mpd, "b")  # 200 s long (the fake MPD): heard after 100 s
    assert "b" not in sh.round["heard"]
    listen(d, clock, 60)
    assert "b" not in sh.round["heard"]
    mpd.state = "pause"
    asyncio.run(d.step({"player"}))
    listen(d, clock, 500)  # a pause does not count
    assert "b" not in sh.round["heard"]
    mpd.state = "play"
    asyncio.run(d.step({"player"}))
    listen(d, clock, 40)
    assert sh.round["heard"] == ["b"]
    assert player.read_state("shuffle")["round"]["heard"] == ["b"]  # published on that wake


def test_a_song_skipped_before_the_threshold_stays_unheard_and_can_come_again(clock):
    files = ("a", "b", "c")
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files}, source=source(files))
    play(d, mpd, "b")
    clock[0] += 99
    play(d, mpd, "c")  # skipped at 99 s of 200
    assert "b" not in sh.round["heard"]
    sh.rests.clear()
    send(d, mpd, "shuffle reroll")
    assert [(e["file"], e["round"]) for e in sh.plan if e["file"] == "b"] == [("b", 0)]  # still owed this round


def test_the_last_seconds_before_a_song_change_count(clock):
    files = ("a", "b", "c")
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files}, source=source(files))
    play(d, mpd, "b")
    clock[0] += 120  # no wake meanwhile: the next song's event is the first one after the threshold
    play(d, mpd, "c")
    assert "b" in sh.round["heard"]


def test_the_threshold_is_ro_listenbrainz_mpds():
    assert shuffle.listen_needed(200) == 100
    assert shuffle.listen_needed(3600) == 240  # always enough
    assert shuffle.listen_needed(0) == 240  # unknown length


def test_newround_while_a_song_plays_does_not_count_it_in_the_new_round(clock):
    files = ("a", "b", "c")
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files}, source=source(files))
    play(d, mpd, "b")
    send(d, mpd, "shuffle newround")
    listen(d, clock)
    assert sh.round["number"] == 2 and "b" not in sh.round["heard"]


def test_a_banned_song_is_never_planned_even_when_queued(clock):
    banned.append([{"ts": "2026-10-11T10:00:00", "action": "ban", "key": "file:c", "file": "c", "artist": "",
                    "title": "", "op": "x"}])
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=source(FILES))
    assert "c" not in planned(sh) and mpd.prio("c") == 0
    assert sh.round["total"] == len(FILES) - 1  # not a member of the round either


def test_a_ban_drops_a_planned_song_at_the_next_wake(clock):
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=source(FILES))
    head = sh.plan[0]["file"]
    banned.append([{"ts": "2026-10-11T10:00:00", "action": "ban", "key": f"file:{head}", "file": head,
                    "artist": "", "title": "", "op": "x"}])
    asyncio.run(d.step({"player"}))
    assert head not in planned(sh) and mpd.prio(head) == 0 and sh.plan
    banned.append([{"ts": "2026-10-11T10:01:00", "action": "unban", "key": f"file:{head}", "file": head,
                    "artist": "", "title": "", "op": "y"}])
    send(d, mpd, "shuffle reroll")
    assert head in planned(sh)


def test_the_round_ends_when_only_banned_songs_are_left(clock):
    files = ("a", "b", "c")
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files}, source=source(files))
    send(d, mpd, "shuffle rounds manual")
    banned.append([{"ts": "2026-10-11T10:00:00", "action": "ban", "key": "file:c", "file": "c", "artist": "",
                    "title": "", "op": "x"}])
    listen(d, clock)  # a heard
    play(d, mpd, "b")
    listen(d, clock)
    assert sh.round["done"] and sh.round["total"] == 2
