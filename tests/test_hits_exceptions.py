"""hits' exceptions (hits_exceptions.py, hits_rules.apply_exceptions): pins and exclusions with a scope, their
precedence, the event log, song ids that follow a merge, and `hits hide` read as a Billboard exclusion."""
import json, sys

import pytest

from rormpc_tools import hits, hits_exceptions as hx, hits_rules as hr, identity, musicdb

from test_hits_rules import cand, keys, rules


@pytest.fixture
def reg(env, tmp_path, monkeypatch):
    """A registry with songs a, b, c (ids id-a ...) and an empty hide log; returns a function to rewrite it."""
    monkeypatch.setattr(hits, "HIDDEN", tmp_path / "music-data" / "hits-hidden.jsonl")

    def write(rows=None):
        rows = rows or [{"id": f"id-{k}", "path": k, "paths": [k], "state": "live"} for k in "abc"]
        musicdb.write_jsonl(identity.registry_path(), rows)
        identity._cache.clear()
    write()
    return write


def run(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hits", *argv])
    hits.main()


def exc(action, scope="library", song=None, chart_key=None, via=None):
    return {"id": f"{action}-{song or chart_key}", "action": action, "scope": scope, "song": song,
            "chart_key": chart_key, "via": via}


# ---------------------------------------------------------------- precedence and scope (pure)

def world():
    """Billboard 1985: a (best), b, c; library songs x, y never charted; x is liked."""
    cands = {"a": cand("a", chart=[1985], peak=100), "b": cand("b", chart=[1985], peak=90),
             "c": cand("c", chart=[1985], peak=80), "x": cand("x"), "y": cand("y")}
    return cands, {"billboard": {"a", "b", "c"}, "likes": {"x"}}


def selected(by_key, sets=("+billboard",), top=None, show_excluded=False, genre_ok=lambda c: True):
    cands, members = world()
    r = rules(list(sets), rank="billboard", years_of="chart")
    rows, _ = hr.select(cands, members, r, wanted={1985}, top=top, genre_ok=genre_ok)
    return hr.apply_exceptions(rows, cands, r, by_key, show_excluded=show_excluded)


def test_a_pin_is_added_after_the_ranked_rows_without_a_rank():
    rows, info = selected({"y": [exc("pin", song="id-y")]}, top=[(1, 33)])
    assert [(c["key"], c["ranked"], c["pinned"]) for c in rows] == [("a", True, False), ("y", False, True)]
    assert rows[1]["rank"] == 2 and rows[1]["cohort"] == 0  # a unique row number, not a rank
    assert info == {"pinned": 1, "excluded": 0}


def test_a_pinned_song_the_rules_select_keeps_its_rank():
    rows, info = selected({"b": [exc("pin", song="id-b")]})
    assert [(c["key"], c["rank"], c["pinned"]) for c in rows] == [("a", 1, False), ("b", 2, True), ("c", 3, False)]
    assert info["pinned"] == 0  # counts the rows a pin added, not the pinned rows the rules selected


def test_an_exclusion_beats_a_pin_whatever_their_scopes():
    by_key = {"b": [exc("pin", song="id-b"), exc("exclude", "set:billboard", song="id-b")],
              "y": [exc("pin", "set:billboard", song="id-y"), exc("exclude", song="id-y")]}
    rows, info = selected(by_key)
    assert keys(rows) == ["a", "c"] and info == {"pinned": 0, "excluded": 1}
    rows, _ = selected(by_key, show_excluded=True)  # shown, marked; the excluded pin is still not added
    assert [(c["key"], c["excluded"]) for c in rows] == [("a", False), ("b", True), ("c", False)]


def test_a_pin_beats_minus_sets_and_genres():
    by_key = {"x": [exc("pin", song="id-x")], "b": [exc("pin", song="id-b")]}
    rows, _ = selected(by_key, sets=("+billboard", "-likes"), genre_ok=lambda c: c["key"] != "b")
    assert keys(rows) == ["a", "c", "b", "x"]  # b and x come back as pins, after the ranked rows


def test_a_set_scoped_exception_applies_only_while_the_set_is_plus():
    by_key = {"a": [exc("exclude", "set:likes", song="id-a")], "y": [exc("pin", "set:likes", song="id-y")]}
    rows, info = selected(by_key)  # likes off: nothing applies
    assert keys(rows) == ["a", "b", "c"] and info == {"pinned": 0, "excluded": 0}
    assert rows[0]["exceptions"][0]["applies"] is False  # still listed for the details, as not applying
    rows, _ = selected(by_key, sets=("+billboard", "-likes"))
    assert keys(rows) == ["a", "b", "c"]  # - is not +
    rows, _ = selected(by_key, sets=("+billboard", "+likes"))
    assert keys(rows) == ["b", "c", "y"]  # x is liked but has no chart year in 1985


def test_exceptions_never_move_a_rank_or_the_top_cut():
    rows, _ = selected({"a": [exc("exclude", song="id-a")]}, top=[(1, 66)])
    assert [(c["key"], c["rank"]) for c in rows] == [("b", 2)]  # a's slot stays a's: c does not move up


def test_a_pin_needs_an_owned_file():
    cands, members = world()
    cands["m"] = cand("m", file=False)
    r = rules(["+billboard"], rank="billboard")
    rows, _ = hr.select(cands, members, r, wanted={1985})
    rows, info = hr.apply_exceptions(rows, cands, r, {"m": [exc("pin", chart_key="m|m")]})
    assert "m" not in keys(rows) and info["pinned"] == 0


# ---------------------------------------------------------------- the log and the CLI

def test_except_writes_events_and_the_later_line_wins(reg, monkeypatch, capsys):
    run(["except", "pin", "--file", "a", "--artist", "A", "--title", "a"], monkeypatch)
    run(["except", "exclude", "--scope", "set:billboard", "--id", "id-a"], monkeypatch)
    run(["except", "exclude", "--file", "a"], monkeypatch)  # replaces the library pin
    evts = musicdb.jsonl(hx.log_path())
    assert [(e["action"], e["scope"], e["song"]) for e in evts] == [
        ("pin", "library", "id-a"), ("exclude", "set:billboard", "id-a"), ("exclude", "library", "id-a")]
    assert len({e["id"] for e in evts}) == 3
    assert sorted((e["action"], e["scope"]) for e in hx.fold().values()) == [
        ("exclude", "library"), ("exclude", "set:billboard")]
    run(["except", "remove", "--scope", "library", "--id", "id-a"], monkeypatch)
    assert [(e["action"], e["scope"]) for e in hx.fold().values()] == [("exclude", "set:billboard")]
    with pytest.raises(SystemExit, match="no exception"):
        run(["except", "remove", "--scope", "library", "--id", "id-a"], monkeypatch)


@pytest.mark.parametrize("argv, error", [
    (["pin", "--chart-key", "toto|africa"], "needs an owned file"),
    (["pin", "--file", "not-synced.mp3"], "not in songs.jsonl"),
    (["exclude", "--id", "id-zzz"], "unknown song id"),
    (["exclude", "--scope", "set:charts2", "--id", "id-a"], "unknown set"),
    (["exclude", "--scope", "list:1", "--id", "id-a"], "no smart list"),
])
def test_except_refuses(reg, monkeypatch, argv, error):
    with pytest.raises(SystemExit, match=error):
        run(["except", *argv], monkeypatch)
    assert not hx.log_path().exists()


def test_a_pin_follows_a_merge_and_a_gone_file_stays_listed(reg, monkeypatch, capsys):
    run(["except", "pin", "--file", "a", "--artist", "A", "--title", "a"], monkeypatch)
    # musicdb dedupe merged a into c: the old id leads to the kept song, nothing in the log is rewritten
    reg([{"id": "id-a", "path": None, "paths": ["a"], "state": "merged", "into": "id-c"},
         {"id": "id-c", "path": "c", "paths": ["c"], "state": "live"},
         {"id": "id-b", "path": None, "paths": ["b"], "state": "gone"}])
    cands = {"a2": cand("c") | {"key": "a2"}, "b": cand("b")}
    assert {k: [e["song"] for e in v] for k, v in hx.by_candidate(cands, hx.active(), hits.hide_key).items()} == {
        "a2": ["id-a"]}
    capsys.readouterr()
    run(["except", "pin", "--id", "id-c"], monkeypatch)  # the survivor's own pin replaces the merged one
    assert [e["song"] for e in hx.fold().values()] == ["id-c"]
    with pytest.raises(SystemExit, match="needs an owned file"):
        run(["except", "pin", "--id", "id-b"], monkeypatch)
    musicdb.write_jsonl(hx.log_path(), musicdb.jsonl(hx.log_path()) + [
        {"id": "e-gone", "ts": "2026-10-10T00:00:00", "action": "pin", "scope": "library", "song": "id-b",
         "chart_key": None, "artist": "B", "title": "b", "file": "b"}])
    capsys.readouterr()
    run(["exceptions", "--json"], monkeypatch)
    listed = json.loads(capsys.readouterr().out)
    assert listed["version"] == 1
    assert [(r["song"], r["file"], r["gone"]) for r in listed["exceptions"]] == [("id-c", "c", False),
                                                                                  ("id-b", None, True)]


def test_hide_is_a_billboard_exclusion_by_chart_key(reg, monkeypatch, capsys):
    run(["hide", "--artist", "Toto feat. X", "--title", "Africa"], monkeypatch)
    key = hits.hide_key("Toto", "Africa")
    cands = {"t": cand("t") | {"artist": "Toto", "title": "Africa", "chart_years": [1985], "peak": 50},
             "rec:1": cand("r", file=False) | {"key": "rec:1", "artist": "Toto", "title": "Africa"}}
    by_key = hx.by_candidate(cands, hx.active(), hits.hide_key)
    assert [(e["scope"], e["via"], e["chart_key"]) for e in by_key["t"]] == [("set:billboard", "hide", key)]
    assert by_key["rec:1"][0]["scope"] == "set:recommended"  # a recommendation is no chart row
    rows, info = hr.apply_exceptions([cands["t"]], cands, rules(["+billboard"]), {"t": by_key["t"]})
    assert rows == [] and info["excluded"] == 1
    rows, _ = hr.apply_exceptions([cands["t"]], cands, rules(["+likes"]), {"t": by_key["t"]})
    assert keys(rows) == ["t"] and rows[0]["hidden"] is False  # Billboard is not + here
    rows, _ = hr.apply_exceptions([cands["t"]], cands, rules(["+billboard"]), {"t": by_key["t"]}, show_excluded=True)
    assert rows[0]["hidden"] and rows[0]["excluded"]
    capsys.readouterr()
    run(["exceptions", "--json"], monkeypatch)
    [r] = json.loads(capsys.readouterr().out)["exceptions"]
    assert (r["action"], r["scope"], r["chart_key"], r["via"]) == ("exclude", "set:billboard", key, "hide")
    # removing it from the exceptions list unhides it (the hide log keeps both lines)
    run(["except", "remove", "--scope", "set:billboard", "--chart-key", key], monkeypatch)
    assert hx.active() == [] and [e["action"] for e in musicdb.jsonl(hits.HIDDEN)] == ["hide", "unhide"]


def test_hits_json_reports_pins_and_exclusions(reg, monkeypatch, tmp_path, capsys):
    """End to end: `hits --json` with a pin beating `-likes` and a library exclusion, as rormpc's pane reads it."""
    songs = {f: {"artist": f.upper(), "title": f, "file": f, "year": 1987, "years": [1987], "mbid": None,
                 "artist_mbid": None, "tags": [], "listens": 0, "hidden": False, "liked": f == "b"} for f in "abc"}
    monkeypatch.setattr(hits, "library_songs", lambda: songs)
    run(["except", "pin", "--file", "b"], monkeypatch)
    run(["except", "exclude", "--file", "c"], monkeypatch)
    out = tmp_path / "current.json"
    argv = ["--years", "1980-1989", "--set", "-likes", "--rank", "plays", "--years-of", "release", "-n", "0",
            "--json", str(out)]
    run(argv, monkeypatch)
    data = json.loads(out.read_text())
    assert [(r["file"], r["ranked"], r["pinned"], r["excluded"], r["song_id"]) for r in data["rows"]] == [
        ("a", True, False, False, "id-a"), ("b", False, True, False, "id-b")]
    assert (data["counts"]["pinned"], data["counts"]["excluded"]) == (1, 1)
    assert data["summary"] == "Library − Likes ∩ 1980-1989 · 1 of 2 · +1 pinned · 1 excluded"
    run([*argv, "--show-excluded"], monkeypatch)
    data = json.loads(out.read_text())
    c = next(r for r in data["rows"] if r["file"] == "c")
    assert c["excluded"] and c["exceptions"][0] | {"id": None} == {
        "id": None, "action": "exclude", "scope": "library", "applies": True, "via": None}
    assert data["args"]["show_excluded"] is True
