"""mpd-player's rounds (decided 2026-10-10): the next round starts by itself, the plan always aims at PLAN_N songs
(the rest from the next round, marked `round: 1`), playback alone moves the round on, a rest is relaxed when nothing
else is eligible, and a short plan says why."""
import asyncio

from rormpc_tools import player
from rormpc_tools.player import shuffle
from test_shuffle import DAY, clock, heard, play, send, setup, state  # noqa: F401 (fixtures)

FILES = tuple("abcdefghijklmn")  # 14: "a" plays, 13 to plan


def hits(files):
    return {"kind": "hits", "name": "80s", "len": len(files), "files": list(files), "rules_hash": "h1"}


def planned(sh):
    return [(e["file"], e.get("round")) for e in sh.plan]


def test_the_plan_takes_the_rest_from_the_next_round_without_marking_it_heard():
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=hits(FILES))
    sh.round["heard"] = list("abcdefghij")  # 10 heard: k, l, m, n are left in this round
    send(d, mpd, "shuffle reroll")
    rounds = [r for _, r in planned(sh)]
    assert len(sh.plan) == shuffle.PLAN_N and rounds == [0] * 4 + [1] * 6
    assert {f for f, r in planned(sh) if r == 0} == set("klmn")
    assert "a" not in [f for f, _ in planned(sh)]  # the playing song
    assert sh.round["heard"] == list("abcdefghij") and sh.round["number"] == 1  # planning heard nothing
    assert [mpd.prio(f) for f, _ in planned(sh)] == list(range(shuffle.PLAN_N, 0, -1))
    assert player.read_state("shuffle")["ahead_note"] == ""


def test_the_round_rolls_when_its_last_song_plays_and_the_marks_move_up(clock):
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=hits(FILES))
    sh.round["heard"] = list("abcdefghijkl")  # m and n are left
    send(d, mpd, "shuffle reroll")
    assert [r for _, r in planned(sh)][:3] == [0, 0, 1]
    clock[0] += 200
    play(d, mpd, sh.plan[0]["file"])
    assert sh.round["number"] == 1
    clock[0] += 200
    play(d, mpd, sh.plan[0]["file"])  # the round's last song: every song heard, the next round starts
    assert sh.round["number"] == 2 and sh.round["heard"] == [] and not sh.round["done"]
    assert len(sh.plan) == shuffle.PLAN_N and all(e["round"] == 0 for e in sh.plan)


def test_a_round_left_with_resting_songs_moves_on_when_its_next_round_entry_plays(clock):
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=hits(FILES))
    sh.round["heard"] = list("abcdefghijkl")
    sh.rests.update({"m": clock[0] + DAY, "n": clock[0] + DAY})  # this round's last two rest
    send(d, mpd, "shuffle reroll")
    assert all(r == 1 for _, r in planned(sh)) and sh.round["number"] == 1
    head = sh.plan[0]["file"]
    clock[0] += 200
    play(d, mpd, head)
    assert sh.round["number"] == 2 and sh.round["heard"] == [head]
    assert all(e["round"] == 0 for e in sh.plan)


def test_rounds_continue_by_themselves(clock):
    files = ("a", "b", "c")
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files}, source=hits(files))
    for f in ("b", "c"):
        clock[0] += 200
        play(d, mpd, f)
    sh.rests.clear()
    asyncio.run(d.step({"player"}))
    assert sh.round["number"] == 2 and not sh.round["done"] and sh.plan
    assert "round done" not in player.read_state("shuffle")["reason"]


def test_manual_rounds_stop_until_newround(clock):
    files = ("a", "b", "c")
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files}, source=hits(files))
    send(d, mpd, "shuffle rounds manual")
    assert all(r == 0 for _, r in planned(sh))
    for f in ("b", "c"):
        clock[0] += 200
        play(d, mpd, f)
    sh.rests.clear()
    asyncio.run(d.step({"player"}))
    assert sh.round["done"] and not sh.plan
    send(d, mpd, "shuffle newround")
    assert sh.plan and sh.round["number"] == 2
    send(d, mpd, "shuffle rounds auto")
    assert player.read_state("shuffle")["rounds"] == "auto"


def test_a_small_source_shows_its_rounds_and_the_reason(clock):
    files = ("a", "b", "c", "d")
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files}, source=hits(files))
    sh.round["heard"] = ["a", "b"]
    send(d, mpd, "shuffle reroll")
    # c, d end this round; b opens the next one (a plays); each queue entry once in the plan
    assert sorted(planned(sh)) == [("b", 1), ("c", 0), ("d", 0)] and [r for _, r in planned(sh)] == [0, 0, 1]
    assert player.read_state("shuffle")["ahead_note"] == "3 ahead · 4 songs in the source"


def test_a_short_plan_names_the_resting_songs(clock):
    files = tuple("abcdefghijklmnopq")  # a plays, 4 free, 12 resting
    d, mpd, sh = setup(files=files, data={f: heard(3) for f in files})
    sh.rests.update({f: clock[0] + DAY for f in files[5:]})
    send(d, mpd, "shuffle reroll")
    assert len(sh.plan) == 4 and not any(e.get("relaxed") for e in sh.plan)
    assert player.read_state("shuffle")["ahead_note"] == "4 ahead · 12 resting"


def test_rest_is_relaxed_for_the_least_recently_played_song_when_nothing_is_eligible(clock):
    files = ("a", "b", "c", "d")
    data = {"b": heard(3, days_ago=1), "c": heard(3, days_ago=3), "d": heard(3, days_ago=2)}
    d, mpd, sh = setup(files=files, data=data)
    sh.rests.update({f: clock[0] + DAY for f in ("b", "c", "d")})
    send(d, mpd, "shuffle reroll")
    assert len(sh.plan) == 1 and sh.plan[0]["file"] == "c" and sh.plan[0]["relaxed"]
    assert sh.plan[0]["why"].startswith("rest relaxed") and mpd.prio("c") == shuffle.PLAN_N
    assert player.read_state("shuffle")["ahead_note"] == "1 ahead · 2 resting · rest relaxed"
    assert sh.reason == ""


def test_rest_relaxed_never_overrides_heard_enough(clock):
    files = ("a", "b")
    d, mpd, sh = setup(files=files, data={"b": heard(3)})
    sh.cooldown["b"] = {"until": clock[0] + DAY, "level": 0}
    send(d, mpd, "shuffle reroll")
    assert sh.plan == [] and "nothing to pick" in sh.reason


def test_rest_relaxed_in_a_round_prefers_a_song_the_round_still_owes(clock):
    files = ("a", "b", "c")
    data = {"b": heard(3, days_ago=1), "c": heard(3, days_ago=5)}
    d, mpd, sh = setup(files=files, data=data, source=hits(files))
    sh.round["heard"] = ["a", "c"]
    sh.rests.update({"b": clock[0] + DAY, "c": clock[0] + DAY})
    send(d, mpd, "shuffle reroll")
    assert planned(sh) == [("b", 0)] and sh.plan[0]["relaxed"]  # c played longer ago but was heard this round


def test_up_next_requests_stay_above_a_plan_across_rounds():
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=hits(FILES))
    sh.round["heard"] = list("abcdefghijkl")
    send(d, mpd, "shuffle reroll", "upnext add n")
    assert "n" not in [f for f, _ in planned(sh)] and mpd.prio("n") == 255
    assert max(mpd.prio(f) for f, _ in planned(sh)) == shuffle.PLAN_N


def test_a_new_source_resets_the_round_and_the_plan_marks():
    d, mpd, sh = setup(files=FILES, data={f: heard(3) for f in FILES}, source=hits(FILES))
    sh.round["heard"] = list("abcdefghijkl")
    send(d, mpd, "shuffle reroll")
    assert any(r == 1 for _, r in planned(sh))
    player.write_state("source", {"source": {**hits(FILES), "rules_hash": "h2"}})
    asyncio.run(d.step({"playlist"}))
    assert sh.round["source"] == "hits:h2" and sh.round["heard"] == [] and sh.round["number"] == 1
    assert all(r == 0 for _, r in planned(sh))
