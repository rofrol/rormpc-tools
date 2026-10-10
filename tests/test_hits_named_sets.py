"""Named sets (hits_sets.py): tag lists, stored MPD playlists, Live playlists and smart lists as ± sets, their
canonical keys, the formula naming them, a missing one as a clear error, smart list cycles, exceptions scoped to a
named set, and `hits sets --json` for rormpc's "+ set…" picker."""
import json, sys

import pytest

from rormpc_tools import hits, hits_exceptions as hx, hits_sets as hs, identity, musicdb, smartlists as sl, tags

LIBRARY = ["--rank", "plays", "--years-of", "release"]  # every library song by my plays: a, b, c, d


@pytest.fixture
def lib(env, tmp_path, monkeypatch):
    """Library songs a, b, c, d (ids id-a ...; c is liked) with plays a 4 .. d 1, no network, an empty hide log;
    tag lists "God" (a, c) and "Road: trip, live" (b); MPD playlists "Road trip" (b, d) and the generated "Hits x"
    (a); a Live playlist yt-PL1 "Discover copy" with d accepted and ready, a pending."""
    mpd = env([{"file": k, "artist": k.upper(), "title": k} for k in "abcd"])
    mpd.playlists = {"Road trip": ["b", "d", "gone.mp3"], "Hits x": ["a"], "Discover copy": ["d"]}
    monkeypatch.setattr(hits, "HIDDEN", tmp_path / "music-data" / "hits-hidden.jsonl")
    musicdb.write_jsonl(identity.registry_path(),
                        [{"id": f"id-{k}", "path": k, "paths": [k], "state": "live"} for k in "abcd"])
    identity._cache.clear()
    songs = {k: {"artist": k.upper(), "title": k, "file": k, "year": 1987, "years": [1987], "mbid": None,
                 "artist_mbid": None, "tags": [], "listens": 0, "hidden": False, "liked": k == "c"} for k in "abcd"}
    monkeypatch.setattr(hits, "library_songs", lambda: songs)
    plays = {"a": 4, "b": 3, "c": 2, "d": 1}
    monkeypatch.setattr(musicdb, "counted", lambda db, lib, *rest: (plays, {}, None, None))
    tag = lambda name, f, action="add": {"ts": "2026-10-10T00:00:00", "list": name, "action": action,
                                         "song": {"key": tags.key_of_file(f), "file": f}}
    tags.append(musicdb.DATA / "collections.jsonl", [tag("God", "a"), tag("God", "c"), tag("Road: trip, live", "b"),
                                                     tag("Gone", "a"), tag("Gone", "a", "remove")])
    sub = {"id": "yt-PL1", "title": "Discover Weekly", "playlist": "Discover copy", "items": {
        "v1": {"position": 1, "active": True, "decision": "accepted", "job": "ready", "path": "d"},
        "v2": {"position": 2, "active": True, "decision": "pending", "path": None}}}
    (musicdb.DATA / "liveplaylists").mkdir()
    (musicdb.DATA / "liveplaylists" / "yt-PL1.json").write_text(json.dumps(sub))
    return songs


def run(argv, monkeypatch, capsys=None):
    monkeypatch.setattr(sys, "argv", ["hits", *argv])
    hits.main()
    return capsys.readouterr().out if capsys else None


def result(argv, monkeypatch, tmp_path):
    out = tmp_path / "result.json"
    run([*argv, "--json", str(out)], monkeypatch)
    return json.loads(out.read_text())


def files(data):
    return [r["file"] for r in data["rows"]]


# ---------------------------------------------------------------- each kind

def test_a_tag_list_is_a_set_its_name_case_ignored(lib, monkeypatch, tmp_path):
    data = result(["--set", "+tag:god", *LIBRARY], monkeypatch, tmp_path)
    assert files(data) == ["a", "c"]
    assert data["args"]["sets"] == ["+tag:God"]  # stored in its first spelling
    assert data["args"]["set_names"] == {"tag:God": "Tag God"}
    assert data["formula"].startswith("Tag God")
    assert files(result(["--set", "-tag:God", *LIBRARY], monkeypatch, tmp_path)) == ["b", "d"]


def test_a_name_with_spaces_colons_and_commas(lib, monkeypatch, tmp_path):
    data = result(["--set", "+tag:Road: trip, live", "--set", "+likes", *LIBRARY], monkeypatch, tmp_path)
    assert files(data) == ["b", "c"]
    assert data["formula"].startswith("(Tag Road: trip, live ∪ Likes)")


def test_a_stored_playlist_is_a_set(lib, monkeypatch, tmp_path):
    data = result(["--set", "+playlist:Road trip", *LIBRARY], monkeypatch, tmp_path)
    assert files(data) == ["b", "d"]  # a file MPD no longer has is no song
    assert data["formula"].startswith("Playlist Road trip")
    assert files(result(["--set", "+tag:God", "--set", "-playlist:Hits x", *LIBRARY], monkeypatch, tmp_path)) == ["c"]


def test_a_live_playlist_is_a_set_by_id_or_name(lib, monkeypatch, tmp_path):
    data = result(["--set", "+live:yt-PL1", *LIBRARY], monkeypatch, tmp_path)
    assert files(data) == ["d"]  # accepted and ready only
    assert data["args"]["set_names"] == {"live:yt-PL1": "Live Discover copy"}
    assert result(["--set", "+live:Discover Weekly", *LIBRARY], monkeypatch, tmp_path)["args"]["sets"] == [
        "+live:yt-PL1"]


def test_a_smart_list_is_a_set_with_its_own_exceptions(lib, monkeypatch, tmp_path):
    run(["lists", "create", "Top two", *LIBRARY, "--top", "1-50"], monkeypatch)  # a, b
    run(["except", "exclude", "--scope", "list:Top two", "--file", "a"], monkeypatch)  # the list's own fix
    lid = sl.find("Top two")["id"]
    data = result(["--set", "+list:top two", *LIBRARY], monkeypatch, tmp_path)
    assert files(data) == ["b"]
    assert data["args"]["sets"] == [f"+list:{lid}"]  # by id: a rename keeps it
    assert data["formula"].startswith("Smart Top two")
    run(["lists", "rename", "Top two", "Best"], monkeypatch)
    assert result(["--set", f"+list:{lid}", *LIBRARY], monkeypatch, tmp_path)["formula"].startswith("Smart Best")
    assert files(result(["--set", f"-list:{lid}", *LIBRARY], monkeypatch, tmp_path)) == ["a", "c", "d"]


def test_smart_lists_nest_and_store_named_sets(lib, monkeypatch, tmp_path):
    run(["lists", "create", "Godly", "--set", "+tag:GOD", *LIBRARY], monkeypatch)
    godly = sl.find("Godly")
    assert godly["rules"]["sets"] == {"tag:God": 1}
    run(["lists", "create", "Outer", "--set", "+list:Godly", "--set", "-likes", *LIBRARY], monkeypatch)
    outer = sl.find("Outer")
    assert outer["rules"]["sets"] == {f"list:{godly['id']}": 1, "likes": -1}
    assert files(result(["--list", "Outer"], monkeypatch, tmp_path)) == ["a"]
    [row] = [r for r in sl.listing() if r["name"] == "Outer"]
    assert row["formula"] == "Smart Godly − Likes" and row["error"] is None
    assert row["args"]["set_names"] == {f"list:{godly['id']}": "Smart Godly"}


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("token, error", [
    ("+tag:Nope", "no tag list 'Nope'"),
    ("+tag:Gone", "no tag list 'Gone'"),  # every song taken off: the list is gone
    ("-playlist:Nope", "no MPD playlist 'Nope'"),
    ("+live:yt-nope", "no Live playlist 'yt-nope'"),
    ("+list:Nope", "no smart list 'Nope'"),
    ("+mood:sad", "unknown set kind 'mood'"),
    ("+tag:", "give a name after tag:"),
])
def test_a_missing_set_is_a_clear_error(lib, monkeypatch, token, error):
    with pytest.raises(SystemExit, match=error):
        run(["--set", token, *LIBRARY], monkeypatch)


def test_a_cycle_is_refused_on_update_and_an_error_when_run(lib, monkeypatch, tmp_path):
    run(["lists", "create", "A", *LIBRARY], monkeypatch)
    run(["lists", "create", "B", "--set", "+list:A", *LIBRARY], monkeypatch)
    with pytest.raises(SystemExit, match="smart list cycle: A → B → A"):
        run(["lists", "update", "A", "--set", "+list:B", *LIBRARY], monkeypatch)
    with pytest.raises(SystemExit, match="smart list cycle: A → A"):
        run(["lists", "update", "A", "--set", "-list:A", *LIBRARY], monkeypatch)
    # a cycle that came in anyway (a git merge of two machines' logs): an error in the picker and the run
    a, b = sl.find("A")["id"], sl.find("B")["id"]
    sl.record("update", a, schema=1, rules=dict(sl.find("A")["rules"], sets={f"list:{b}": 1}))
    rows = {r["name"]: r for r in sl.listing()}
    assert rows["A"]["error"] == "smart list cycle: A → B → A"
    assert rows["B"]["error"] == "smart list cycle: B → A → B"
    with pytest.raises(SystemExit, match="^hits: in smart list 'B': smart list cycle: A → B → A$"):
        run(["--list", "A"], monkeypatch)
    with pytest.raises(SystemExit, match="in smart list 'A': smart list cycle: B → A → B"):
        run(["--set", f"+list:{b}", *LIBRARY], monkeypatch)
    assert {r["name"]: r["error"] for r in hs.listing() if r["kind"] == "list"} == {
        "A": "smart list cycle: A → B → A", "B": "smart list cycle: B → A → B"}
    assert sl.export(out=lambda *_: None) == ["A: hits: in smart list 'B': smart list cycle: A → B → A",
                                               "B: hits: in smart list 'A': smart list cycle: B → A → B"]


def test_the_open_list_cannot_use_itself(lib, monkeypatch):
    run(["lists", "create", "A", *LIBRARY], monkeypatch)
    lid = sl.find("A")["id"]
    with pytest.raises(SystemExit, match="smart list cycle: A → A"):
        run(["--set", f"+list:{lid}", *LIBRARY, "--open-list", lid], monkeypatch)


# ---------------------------------------------------------------- exceptions scoped to a named set

def test_an_exception_scoped_to_a_named_set_applies_only_while_it_is_plus(lib, monkeypatch, tmp_path):
    run(["except", "exclude", "--scope", "set:tag:god", "--file", "a"], monkeypatch)
    run(["except", "pin", "--scope", "set:playlist:Road trip", "--file", "c"], monkeypatch)
    assert sorted(e["scope"] for e in hx.fold().values()) == ["set:playlist:Road trip", "set:tag:God"]
    assert files(result(["--set", "+tag:God", *LIBRARY], monkeypatch, tmp_path)) == ["c"]
    assert files(result(["--set", "-tag:God", *LIBRARY], monkeypatch, tmp_path)) == ["b", "d"]
    assert files(result(LIBRARY, monkeypatch, tmp_path)) == ["a", "b", "c", "d"]
    assert files(result(["--set", "+playlist:Road trip", *LIBRARY], monkeypatch, tmp_path)) == ["b", "d", "c"]
    names = {r["scope"]: r["scope_name"] for r in hx.listing()}
    assert names == {"set:tag:God": "Tag God", "set:playlist:Road trip": "Playlist Road trip"}


def test_an_exception_scoped_to_a_smart_list_set_follows_its_id(lib, monkeypatch, tmp_path):
    run(["lists", "create", "Liked", "--set", "+likes", *LIBRARY], monkeypatch)
    lid = sl.find("Liked")["id"]
    run(["except", "pin", "--scope", "set:list:Liked", "--file", "d"], monkeypatch)
    assert [e["scope"] for e in hx.fold().values()] == [f"set:list:{lid}"]
    assert files(result(["--set", f"+list:{lid}", *LIBRARY], monkeypatch, tmp_path)) == ["c", "d"]
    assert files(result(["--list", "Liked"], monkeypatch, tmp_path)) == ["c"]  # open is not + as a set


# ---------------------------------------------------------------- the picker

def test_hits_sets_lists_every_named_set_per_kind(lib, monkeypatch, capsys):
    run(["lists", "create", "Mine", *LIBRARY], monkeypatch)
    lid = sl.find("Mine")["id"]
    capsys.readouterr()
    data = json.loads(run(["sets", "--json"], monkeypatch, capsys))
    assert data["version"] == 1
    assert [(r["key"], r["name"], r["songs"], r["error"]) for r in data["sets"]] == [
        ("tag:God", "God", 2, None), ("tag:Road: trip, live", "Road: trip, live", 1, None),
        ("playlist:Road trip", "Road trip", 3, None),  # generated and Live playlists are offered elsewhere
        ("live:yt-PL1", "Discover copy", 1, None),
        (f"list:{lid}", "Mine", None, None)]  # not exported yet: no count
    assert {r["kind"] for r in data["sets"]} == {"tag", "playlist", "live", "list"}
