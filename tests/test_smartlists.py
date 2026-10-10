"""Smart lists (smartlists.py, `hits lists`, `hits --list/--rules/--open-list`): the event log and its folding,
rules this version cannot read, list-scoped exceptions, the "Smart NAME" export and "my playlists" leaving it out."""
import json, sys

import pytest

from rormpc_tools import hits, hits_exceptions as hx, hits_rules as hr, identity, musicdb, smartlists as sl


@pytest.fixture
def lib(env, tmp_path, monkeypatch):
    """Library songs a, b, c (ids id-a ...; c is liked) with plays a 3, b 2, c 1, no network, and an empty hide
    log."""
    env([{"file": k, "artist": k.upper(), "title": k} for k in "abc"])
    monkeypatch.setattr(hits, "HIDDEN", tmp_path / "music-data" / "hits-hidden.jsonl")
    musicdb.write_jsonl(identity.registry_path(),
                        [{"id": f"id-{k}", "path": k, "paths": [k], "state": "live"} for k in "abc"])
    identity._cache.clear()
    songs = {k: {"artist": k.upper(), "title": k, "file": k, "year": 1987, "years": [1987], "mbid": None,
                 "artist_mbid": None, "tags": [], "listens": 0, "hidden": False, "liked": k == "c"} for k in "abc"}
    monkeypatch.setattr(hits, "library_songs", lambda: songs)
    plays = {"a": 3, "b": 2, "c": 1}
    monkeypatch.setattr(musicdb, "counted", lambda db, lib, *rest: (plays, {}, None, None))
    return songs


def run(argv, monkeypatch, capsys=None):
    monkeypatch.setattr(sys, "argv", ["hits", *argv])
    hits.main()
    return capsys.readouterr().out if capsys else None


def result(argv, monkeypatch, tmp_path):
    out = tmp_path / "result.json"
    run([*argv, "--json", str(out)], monkeypatch)
    return json.loads(out.read_text())


LIBRARY = ["--rank", "plays", "--years-of", "release"]  # every library song by my plays: a, b, c


# ---------------------------------------------------------------- the log

def test_create_rename_update_delete_fold_in_file_order(lib, monkeypatch, capsys):
    run(["lists", "create", "80s party", "--set", "-likes", *LIBRARY, "--years", "1980-1989", "--top", "1-10",
         "-g", "rock -country", "--artist", "+Queen; -Toto", "--json", "/ignored.json", "-n", "0"], monkeypatch)
    [lst] = sl.fold().values()
    assert lst["name"] == "80s party" and lst["blocked"] is None
    assert lst["rules"] == {"schema": 1, "sets": {"likes": -1}, "rank": "plays", "years_of": "release",
                            "period": "1980-1989", "top": "1-10", "genres": ["+rock", "-country"],
                            "artists": ["+Queen", "-Toto"], "owned": False}
    run(["lists", "rename", "80S PARTY", "Party"], monkeypatch)  # by name, case ignored
    run(["lists", "update", lst["id"], "--set", "+likes", *LIBRARY], monkeypatch)
    [lst2] = sl.fold().values()
    assert (lst2["id"], lst2["name"], lst2["rules"]["sets"], lst2["rules"]["period"]) == (lst["id"], "Party",
                                                                                         {"likes": 1}, None)
    run(["lists", "delete", "party"], monkeypatch)
    assert sl.fold() == {}
    evts = sl.events()
    assert [(e["event"], e["id"]) for e in evts] == [(k, lst["id"]) for k in ("create", "rename", "update", "delete")]
    assert evts[0]["schema"] == 1 and "rules" in evts[2]
    # a git merge may append another machine's events after a delete: they fold into nothing
    assert sl.fold(evts + [{"id": lst["id"], "event": "rename", "name": "Back"}]) == {}


@pytest.mark.parametrize("argv, error", [
    (["create", "a/b", *LIBRARY], "without '/'"),
    (["create", " ", *LIBRARY], "give a name"),
    (["create", "Mine", "--rank", "listens", "1980s"], "listens cannot be saved"),
    (["create", "Mine", "--set", "+recommended", "--top", "1-10"], "needs a rank"),
    (["create", "Mine", "--list", "x"], "not --list"),
    (["rename", "nope", "x"], "no smart list"),
])
def test_lists_refuse(lib, monkeypatch, argv, error):
    with pytest.raises(SystemExit, match=error):
        run(["lists", *argv], monkeypatch)
    assert not sl.log_path().exists()


def test_a_taken_name_is_refused_case_ignored(lib, monkeypatch):
    run(["lists", "create", "Mine", *LIBRARY], monkeypatch)
    run(["lists", "create", "Other", *LIBRARY], monkeypatch)
    for argv in (["create", "MINE", *LIBRARY], ["rename", "Other", "mine"], ["duplicate", "Other", "Mine"]):
        with pytest.raises(SystemExit, match="exists"):
            run(["lists", *argv], monkeypatch)
    run(["lists", "rename", "Mine", "MINE"], monkeypatch)  # its own name in another case is fine
    assert sorted(x["name"] for x in sl.fold().values()) == ["MINE", "Other"]


def test_rules_round_trip_through_options_and_args(lib):
    a = hits.parser().parse_args(hits.set_argv(["--set", "+billboard", "--set", "-likes", "--years", "1985-1992",
                                                "--top", "1-10,21-50", "-g", "hip hop, -country", "--owned"]))
    rules = sl.rules_of(a)
    b = hits.parser().parse_args([])
    sl.to_options(rules, b)
    assert sl.rules_of(b) == rules
    lst = {"id": "L", "name": "N", "rules": rules}
    assert sl.as_args(lst) == {"period": "1985-1992", "top": "1-10,21-50", "genre": "+hip hop, -country",
                               "artist": "", "owned": True, "rank": "billboard", "years_of": "chart",
                               "sets": ["+billboard", "-likes"], "show_excluded": False, "source": None,
                               "set_names": {}, "open_list": "L", "open_list_name": "N"}
    assert sl.as_args({**lst, "rules": rules | {"top": None}})["top"] == "1-100"  # no Top % in Play's filters
    assert sl.formula(rules) == "Billboard − Likes ∩ 1985-1992 ∩ Top 1-10,21-50% ∩ hip hop − country ∩ owned"


# ---------------------------------------------------------------- what this version cannot read

def newer(lst_id="L1", **change):
    rules = {"schema": 1, "sets": {}, "rank": "plays", "years_of": "release", "period": None, "top": None,
             "genres": [], "artists": [], "owned": False}
    return {"id": lst_id, "event": "create", "name": "Newer", "schema": 1, "rules": rules | change}


@pytest.mark.parametrize("evts, why", [
    ([newer(mood="happy")], "unknown rule field mood"),
    ([newer(schema=2)], "rules schema 2"),
    ([newer(sets={"tag:": 1})], "set 'tag:'"),
    ([newer(sets={"mood:sad": 1})], "set 'mood:sad'"),
    ([newer(rank="vibes")], "rank 'vibes'"),
    ([newer(), {"id": "L1", "event": "pin", "schema": 1}], "event 'pin'"),
    ([newer() | {"schema": 2}], "event schema 2"),
])
def test_a_rule_this_version_cannot_read_blocks_the_list(evts, why):
    [lst] = sl.fold(evts).values()
    assert lst["blocked"] == f"{sl.NEWER} ({why})"


def test_a_blocked_list_never_runs_and_keeps_its_last_export(lib, monkeypatch, tmp_path, capsys):
    sl.log_path().write_text(json.dumps(newer(mood="happy")) + "\n")
    with pytest.raises(SystemExit, match="made by a newer rormpc-tools, update it"):
        run(["--list", "Newer"], monkeypatch)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": newer(mood="happy")["rules"]}))
    with pytest.raises(SystemExit, match="made by a newer rormpc-tools"):
        run(["--rules", str(rules)], monkeypatch)
    musicdb.PLAYLISTS.mkdir(parents=True)
    old = musicdb.PLAYLISTS / "Smart Newer.m3u"
    old.write_text("a\n")
    assert sl.export(out=lambda *_: None) == [f"Newer: {sl.NEWER} (unknown rule field mood)"]
    assert old.read_text() == "a\n"
    [row] = sl.listing()
    assert row["blocked"] and row["args"] is None and row["formula"] is None
    with pytest.raises(RuntimeError, match="newer rormpc-tools"):
        musicdb.smart_lists()  # `musicdb update` reports it among the failed steps


def test_list_and_rules_take_no_filter_options(lib, monkeypatch):
    run(["lists", "create", "Mine", *LIBRARY], monkeypatch)
    with pytest.raises(SystemExit, match="drop --years, --top"):
        run(["--list", "Mine", "--years", "1980-1989", "--top", "1-10"], monkeypatch)


# ---------------------------------------------------------------- list-scoped exceptions

def test_list_exceptions_apply_only_while_that_list_is_open(lib, monkeypatch, tmp_path):
    run(["lists", "create", "Mine", *LIBRARY], monkeypatch)
    run(["lists", "create", "Other", *LIBRARY], monkeypatch)
    mine, other = sl.find("Mine")["id"], sl.find("Other")["id"]
    run(["except", "exclude", "--scope", "list:Mine", "--file", "a"], monkeypatch)  # a name resolves to the id
    assert [e["scope"] for e in hx.fold().values()] == [f"list:{mine}"]
    files = lambda data: [r["file"] for r in data["rows"]]
    assert files(result(LIBRARY, monkeypatch, tmp_path)) == ["a", "b", "c"]  # no list open
    assert files(result([*LIBRARY, "--open-list", other], monkeypatch, tmp_path)) == ["a", "b", "c"]
    data = result(["--list", "Mine"], monkeypatch, tmp_path)
    assert files(data) == ["b", "c"] and data["counts"]["excluded"] == 1
    assert (data["args"]["open_list"], data["args"]["open_list_name"]) == (mine, "Mine")
    data = result([*LIBRARY, "--open-list", mine], monkeypatch, tmp_path)  # Play: the list open, filters changed
    assert files(data) == ["b", "c"]
    shown = result([*LIBRARY, "--open-list", mine, "--show-excluded"], monkeypatch, tmp_path)
    assert [e["applies"] for e in shown["rows"][0]["exceptions"]] == [True]
    assert [e["applies"] for e in result(LIBRARY + ["--show-excluded"], monkeypatch, tmp_path)["rows"][0]["exceptions"]] == [False]


def test_the_pure_rule_reads_the_open_list():
    r = hr.resolve(None, [], "plays", "release")
    e = {"scope": "list:L1"}
    assert not hr.exception_applies(e, r)
    r.list = "L2"
    assert not hr.exception_applies(e, r)
    r.list = "L1"
    assert hr.exception_applies(e, r) and not hr.exception_applies({"scope": "list:"}, r)


def test_delete_takes_its_exceptions_along_and_duplicate_copies_them(lib, monkeypatch, capsys):
    run(["lists", "create", "Mine", *LIBRARY], monkeypatch)
    run(["except", "pin", "--scope", "list:Mine", "--file", "b"], monkeypatch)
    run(["except", "exclude", "--scope", "list:Mine", "--file", "a"], monkeypatch)
    run(["except", "exclude", "--file", "c"], monkeypatch)  # library: stays
    run(["lists", "duplicate", "Mine", "Copy"], monkeypatch)
    copy = sl.find("Copy")["id"]
    assert sorted((e["action"], e["song"]) for e in hx.fold().values() if e["scope"] == f"list:{copy}") == [
        ("exclude", "id-a"), ("pin", "id-b")]
    assert [r["exceptions"] for r in sl.listing()] == [{"pins": 1, "exclusions": 1}] * 2
    mine = sl.find("Mine")["id"]
    capsys.readouterr()
    assert run(["lists", "delete", "Mine"], monkeypatch, capsys) == "deleted Mine and its 2 exceptions\n"
    assert sorted(e["scope"] for e in hx.fold().values()) == ["library", f"list:{copy}", f"list:{copy}"]
    with pytest.raises(SystemExit, match="no smart list"):
        run(["except", "pin", "--scope", f"list:{mine}", "--file", "b"], monkeypatch)
    names = {r["scope"]: r["scope_name"] for r in hx.listing()}
    assert names == {"library": None, f"list:{copy}": "Copy"}


# ---------------------------------------------------------------- export

def test_export_writes_smart_name_playlists_and_drops_stale_ones(lib, monkeypatch, capsys):
    run(["lists", "create", "Mine", *LIBRARY], monkeypatch)
    run(["lists", "create", "Liked", "--set", "+likes", *LIBRARY], monkeypatch)
    run(["except", "exclude", "--scope", "list:Mine", "--file", "b"], monkeypatch)
    musicdb.PLAYLISTS.mkdir(parents=True)
    (musicdb.PLAYLISTS / "Smart Gone.m3u").write_text("a\n")  # a list deleted on another machine
    (musicdb.PLAYLISTS / "Tag God.m3u").write_text("a\n")
    capsys.readouterr()
    run(["lists", "export"], monkeypatch)
    assert sorted(p.name for p in musicdb.PLAYLISTS.iterdir()) == ["Smart Liked.m3u", "Smart Mine.m3u",
                                                                   "Tag God.m3u"]
    assert (musicdb.PLAYLISTS / "Smart Mine.m3u").read_text() == "a\nc\n"  # its exclusion applies
    assert (musicdb.PLAYLISTS / "Smart Liked.m3u").read_text() == "c\n"
    row = next(r for r in sl.listing() if r["name"] == "Mine")
    assert row["exported"] and row["exported_songs"] == 2 and row["playlist"] == "Smart Mine"
    # rormpc's picker parses a copy of this shape (rormpc_smartlists.rs, test parses_the_lists_json)
    assert set(row) == {"id", "name", "rules", "blocked", "error", "args", "formula", "exceptions", "playlist", "exported",
                        "exported_songs", "created", "updated"}
    run(["lists", "rename", "Mine", "Ours"], monkeypatch)  # the export follows the name
    assert (musicdb.PLAYLISTS / "Smart Ours.m3u").read_text() == "a\nc\n"
    assert not (musicdb.PLAYLISTS / "Smart Mine.m3u").exists()
    run(["lists", "delete", "Ours"], monkeypatch)
    assert not (musicdb.PLAYLISTS / "Smart Ours.m3u").exists()
    musicdb.smart_lists()  # what `musicdb update` runs: no error with readable lists


def test_my_playlists_leave_the_smart_exports_out(lib):
    mpd = musicdb.mpd()
    mpd.playlists = {"Smart 80s": ["a"], "Road trip": ["b"]}
    files, skipped = hits.my_playlists()
    assert files == {"b": ["Road trip"]} and skipped == ["Smart 80s"]
    assert "Smart " in hits.GENERATED_PLAYLISTS
