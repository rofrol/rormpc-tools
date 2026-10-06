"""Versioned forecast edits affect only daemon-owned priorities, never MPD queue positions."""
import asyncio

import pytest
from mpd.base import CommandError

from rormpc_tools import player
from rormpc_tools.player import shuffle
from test_shuffle import setup, send, play, state, clock  # shared isolated fixtures


def fixture_plan():
    return setup(files=tuple("abcdefghijklmnop"))


def swap(d, mpd, sh, a, b, token="test", version=None):
    send(d, mpd, f"shuffle swap {version or sh.plan_version} {a} {b} {token}")


def test_adjacent_swap_is_confirmed_and_never_reorders_queue():
    d, mpd, sh = fixture_plan()
    original = [e["id"] for e in sh.plan]
    queue = [e["id"] for e in mpd.q]
    version = sh.plan_version
    swap(d, mpd, sh, original[0], original[1])
    assert [e["id"] for e in sh.plan] == [original[1], original[0], *original[2:]]
    assert [e["id"] for e in mpd.q] == queue
    assert [mpd.prio(e["file"]) for e in sh.plan] == list(range(10, 0, -1))
    saved = player.read_state("shuffle")
    assert saved["ack"] == {"token": "test", "ok": True, "error": None, "version": sh.plan_version}
    assert sh.plan_version != version and sh.patch_base == original


def test_heartbeat_keeps_patch_and_version(clock):
    d, mpd, sh = fixture_plan()
    swap(d, mpd, sh, sh.plan[0]["id"], sh.plan[1]["id"])
    version, patched = sh.plan_version, [e["id"] for e in sh.plan]
    before = sh.updated_at
    clock[0] += player.MAX_WAIT + 1
    asyncio.run(d.step(set()))
    assert sh.updated_at > before and sh.plan_version == version
    assert [e["id"] for e in sh.plan] == patched and sh.patch_base


def test_stale_version_and_nonadjacent_ids_are_rejected():
    d, mpd, sh = fixture_plan()
    version = sh.plan_version
    ids = [e["id"] for e in sh.plan]
    swap(d, mpd, sh, ids[0], ids[1])
    patched = [e["id"] for e in sh.plan]
    swap(d, mpd, sh, ids[1], ids[2], token="stale", version=version)
    assert [e["id"] for e in sh.plan] == patched
    assert sh.ack["token"] == "stale" and not sh.ack["ok"] and "changed" in sh.ack["error"]
    swap(d, mpd, sh, patched[0], patched[2])
    assert not sh.ack["ok"] and "adjacent" in sh.ack["error"]
    swap(d, mpd, sh, patched[0], 99999)
    assert not sh.ack["ok"] and [e["id"] for e in sh.plan] == patched


def test_playing_patched_head_restores_original_survivors_before_top_up():
    d, mpd, sh = fixture_plan()
    original = [e["id"] for e in sh.plan]
    swap(d, mpd, sh, original[0], original[1])
    head = sh.plan[0]
    queue = [e["id"] for e in mpd.q]
    play(d, mpd, head["file"])
    assert sh.patch_base is None
    assert [e["id"] for e in sh.plan][:9] == [id_ for id_ in original if id_ != head["id"]]
    assert [e["id"] for e in mpd.q] == queue
    assert len(sh.plan) == 10 and sh.playing["origin"] == "auto"


def test_removal_restores_original_survivors():
    d, mpd, sh = fixture_plan()
    original = [e["id"] for e in sh.plan]
    swap(d, mpd, sh, original[3], original[4])
    asyncio.run(mpd.deleteid(original[3]))
    queue = [e["id"] for e in mpd.q]
    asyncio.run(d.step({"playlist"}))
    assert sh.patch_base is None
    assert [e["id"] for e in sh.plan][:9] == [id_ for id_ in original if id_ != original[3]]
    assert [e["id"] for e in mpd.q] == queue


def test_restart_restores_base_and_invalidates_old_version():
    d, mpd, sh = fixture_plan()
    original = [e["id"] for e in sh.plan]
    version = sh.plan_version
    swap(d, mpd, sh, original[0], original[1])
    new = shuffle.Shuffle(rng=sh.rng)
    d.modules["shuffle"] = new
    asyncio.run(d.step({"player", "playlist", "options"}))
    assert [e["id"] for e in new.plan] == original and new.patch_base is None
    swap(d, mpd, new, original[0], original[1], version=version)
    assert not new.ack["ok"] and new.plan_version != version


def test_partial_priority_failure_never_confirms_success(monkeypatch):
    d, mpd, sh = fixture_plan()
    original = [e["id"] for e in sh.plan]
    queue = [e["id"] for e in mpd.q]

    original_prioid, writes = mpd.prioid, []

    async def rejected(prio, id_):
        writes.append((prio, id_))
        if len(writes) == 1:
            return await original_prioid(prio, id_)  # a real partial publication, not failure before the first write
        raise CommandError("Scratch priority rejected")

    monkeypatch.setattr(mpd, "prioid", rejected)
    swap(d, mpd, sh, original[0], original[1])
    assert not sh.ack["ok"] and "rejected" in sh.ack["error"]
    assert [e["id"] for e in sh.plan] == original and [e["id"] for e in mpd.q] == queue
    assert player.read_state("shuffle")["publish_error"] and len(writes) >= 2
    monkeypatch.setattr(mpd, "prioid", original_prioid)
    asyncio.run(d.step({"playlist"}))
    assert player.read_state("shuffle")["publish_error"] is None
    assert [mpd.prio(e["file"]) for e in sh.plan] == list(range(10, 0, -1))
    swap(d, mpd, sh, original[0], original[1], token="after-recovery")
    assert sh.ack["ok"] and sh.ack["token"] == "after-recovery"


def test_external_play_between_keypress_and_dispatch_rejects_old_version():
    d, mpd, sh = fixture_plan()
    version = sh.plan_version
    original = sh.plan[:]
    mpd.cur = str(original[0]["id"])
    mpd.q[mpd.pos(mpd.cur)]["prio"] = 0
    swap(d, mpd, sh, original[1]["id"], original[2]["id"], version=version)
    assert not sh.ack["ok"] and "changed" in sh.ack["error"]
    assert sh.patch_base is None and [e["id"] for e in sh.plan][:9] == [e["id"] for e in original[1:]]


def test_two_messages_with_the_same_version_never_apply_two_swaps():
    d, mpd, sh = fixture_plan()
    version, ids = sh.plan_version, [e["id"] for e in sh.plan]
    send(d, mpd, f"shuffle swap {version} {ids[0]} {ids[1]} first",
         f"shuffle swap {version} {ids[1]} {ids[2]} second")
    assert [e["id"] for e in sh.plan] == [ids[1], ids[0], *ids[2:]]
    assert sh.ack["token"] == "second" and not sh.ack["ok"]


def test_additional_valid_swaps_keep_the_first_canonical_base():
    d, mpd, sh = fixture_plan()
    original = [e["id"] for e in sh.plan]
    swap(d, mpd, sh, original[0], original[1])
    swap(d, mpd, sh, original[0], original[2], token="second-valid")
    assert sh.ack["ok"] and sh.patch_base == original
    assert [e["id"] for e in sh.plan] == [original[1], original[2], original[0], *original[3:]]


@pytest.mark.parametrize("kind", ["hits", "playlist", "library"])
def test_source_change_invalidates_version_and_restores_base(kind):
    d, mpd, sh = fixture_plan()
    original = [e["id"] for e in sh.plan]
    swap(d, mpd, sh, original[0], original[1])
    version = sh.plan_version
    player.write_state("source", {"source": {"kind": kind, "name": "another-source"}})
    swap(d, mpd, sh, original[1], original[2], version=version)
    assert sh.patch_base is None and [e["id"] for e in sh.plan] == original
    assert not sh.ack["ok"] and sh.plan_version != version


def test_heard_enough_expires_patch_before_replacement():
    d, mpd, sh = fixture_plan()
    original = sh.plan[:]
    swap(d, mpd, sh, original[0]["id"], original[1]["id"])
    send(d, mpd, f"shuffle heardenough {original[4]['file']}")
    assert sh.patch_base is None
    assert [e["id"] for e in sh.plan][:9] == [e["id"] for e in original if e != original[4]]


def test_song_advancing_during_priority_publication_expires_instead_of_confirming(monkeypatch):
    d, mpd, sh = fixture_plan()
    original, queue = sh.plan[:], [e["id"] for e in mpd.q]
    original_prioid, writes = mpd.prioid, []

    async def advancing(prio, id_):
        await original_prioid(prio, id_)
        writes.append((prio, id_))
        if len(writes) == 1:
            mpd.cur = str(original[1]["id"])
            mpd.q[mpd.pos(mpd.cur)]["prio"] = 0

    monkeypatch.setattr(mpd, "prioid", advancing)
    swap(d, mpd, sh, original[0]["id"], original[1]["id"])
    assert not sh.ack["ok"] and "expired" in sh.ack["error"] and sh.patch_base is None
    assert [e["id"] for e in sh.plan][:9] == [e["id"] for e in original if e != original[1]]
    assert [e["id"] for e in mpd.q] == queue and sh.publish_error is None


def test_failed_withdrawal_is_stale_and_recovery_is_published(monkeypatch):
    d, mpd, sh = fixture_plan()
    original_prioid = mpd.prioid

    async def rejected(prio, id_):
        raise CommandError("Scratch withdrawal rejected")

    monkeypatch.setattr(mpd, "prioid", rejected)
    send(d, mpd, "shuffle off")
    assert player.read_state("shuffle")["publish_error"] and sh.plan and not sh.enabled
    monkeypatch.setattr(mpd, "prioid", original_prioid)
    asyncio.run(d.step({"playlist"}))
    assert not sh.plan and player.read_state("shuffle")["publish_error"] is None
