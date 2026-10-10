"""hits' selection rules (hits_rules.py): the ± set algebra, the rank population rule, --source as a shorthand,
Top % refused without a rank, and the printed formula."""
from types import SimpleNamespace

import pytest

from rormpc_tools import hits_rules as hr


def cand(key, *, file=True, release=1985, chart=(), peak=0, plays=0, hidden=False):
    return {"key": key, "file": key if file else None, "artist": key.upper(), "title": key, "release": release,
            "chart_years": list(chart), "peak": peak, "points": peak, "plays": plays, "hidden": hidden,
            "order": (1, key, key)}


def rules(sets=None, rank=None, years_of=None, source=None):
    return hr.resolve(source, sets, rank, years_of)


def keys(rows):
    return [r["key"] for r in rows]


# ---------------------------------------------------------------- set algebra

def test_union_of_plus_minus_union_of_minus():
    cands = {k: cand(k) for k in "abcde"}
    members = {"billboard": {"a", "b"}, "likes": {"b", "c"}, "playlists": {"c", "d"}}
    r = rules(["+billboard", "+likes", "-playlists"], rank="none")
    assert sorted(hr.base_keys(cands, members, r.sets)) == ["a", "b"]  # (a b ∪ b c) − c d


def test_no_plus_set_means_the_whole_library_minus_the_minus_sets():
    cands = {k: cand(k) for k in "abc"} | {"chart:x": cand("chart:x", file=False)}
    members = {"likes": {"a"}}
    r = rules(["-likes"], rank="none")
    assert sorted(hr.base_keys(cands, members, r.sets)) == ["b", "c"]  # a missing chart row is not library


def test_a_later_set_value_wins_and_aliases_are_read():
    assert hr.parse_sets(["+likes", "-likes", "recs"]) == {"likes": -1, "recommended": 1}
    assert hr.parse_sets(["+billboard,-likes"]) == {"billboard": 1, "likes": -1}


@pytest.mark.parametrize("tok, parsed", [
    ("+tag:Christmas", ("tag:Christmas", 1)), ("-playlist:Road  trip ", ("playlist:Road trip", -1)),
    ("+live:yt-x", ("live:yt-x", 1)), ("list:1", ("list:1", 1)), ("-TAG:a: b, c", ("tag:a: b, c", -1))])
def test_named_sets_keep_their_name(tok, parsed):
    assert hr.parse_set(tok) == parsed


def test_a_named_set_is_one_set_value_commas_and_all():
    assert hr.parse_sets(["+billboard,-likes", "+tag:Rock, Pop", "-playlist:a:b"]) == {
        "billboard": 1, "likes": -1, "tag:Rock, Pop": 1, "playlist:a:b": -1}
    with pytest.raises(ValueError, match="give a name"):
        hr.parse_set("+tag:  ")


def test_the_formula_names_named_sets():
    r = SimpleNamespace(sets={"billboard": 1, "tag:God": 1, "list:L1": -1, "playlist:Road trip": -1})
    assert hr.formula(r, names={"list:L1": "Smart 80s"}) == (
        "(Billboard ∪ Tag God) − (Smart 80s ∪ Playlist Road trip)")


def test_unknown_sets_are_refused():
    with pytest.raises(ValueError, match="unknown set"):
        hr.parse_set("+charts2")
    with pytest.raises(ValueError, match="unknown set kind"):
        hr.parse_set("+genre:rock")


# ---------------------------------------------------------------- rank population

def billboard_world():
    """Four chart songs of 1985 (a best) and two library songs that never charted."""
    cands = {"a": cand("a", chart=[1985], peak=100), "b": cand("b", chart=[1985], peak=90),
             "c": cand("c", chart=[1985], peak=80, file=False), "d": cand("d", chart=[1985], peak=70),
             "x": cand("x"), "y": cand("y")}
    members = {"billboard": {"a", "b", "c", "d"}, "likes": {"b", "d", "x"}}
    return cands, members


def test_rank_never_depends_on_the_sets_or_filters():
    cands, members = billboard_world()
    r = rules(["+likes"], rank="billboard", years_of="chart")
    rows, info = hr.select(cands, members, r, wanted={1985})
    # b and d keep their chart ranks (2nd and 4th of 4); x is liked but has no chart year in the period
    assert [(c["key"], c["rank"], c["cohort"]) for c in rows] == [("b", 2, 4), ("d", 4, 4)]
    assert info == {"candidates": 3, "cohort": 4, "pool": info["pool"]}
    # the same with a genre-like filter that drops b: d is still 4th
    rows, _ = hr.select(cands, members, r, wanted={1985}, genre_ok=lambda c: c["key"] != "b")
    assert [(c["key"], c["rank"]) for c in rows] == [("d", 4)]


def test_top_percent_is_cut_in_the_population_before_the_sets():
    cands, members = billboard_world()
    r = rules(["+likes"], rank="billboard", years_of="release")
    rows, _ = hr.select(cands, members, r, wanted={1985}, top=[(1, 50)])
    assert keys(rows) == ["b"]  # top 50% of 4 chart songs = a, b; of them only b is liked
    rows, _ = hr.select(cands, members, r, wanted={1985})
    assert [(c["key"], c["ranked"]) for c in rows] == [("b", True), ("d", True), ("x", False)]  # x: no rank


def test_hidden_songs_stay_in_the_population():
    cands, members = billboard_world()
    cands["a"]["hidden"] = True
    r = rules(["+billboard"], rank="billboard")
    rows, _ = hr.select(cands, members, r, wanted={1985}, top=[(1, 50)])
    assert [(c["key"], c["rank"]) for c in rows] == [("b", 2)]
    rows, _ = hr.select(cands, members, r, wanted={1985}, top=[(1, 50)], show_hidden=True)
    assert keys(rows) == ["a", "b"]


def test_plays_population_is_the_library_of_the_period():
    cands = {"a": cand("a", plays=5), "b": cand("b", plays=9, release=1990), "c": cand("c", plays=1),
             "m": cand("m", file=False, plays=0)}
    r = rules([], rank="plays", years_of="release")
    rows, info = hr.select(cands, {}, r, wanted={1985})
    assert [(c["key"], c["rank"], c["cohort"]) for c in rows] == [("a", 1, 2), ("c", 2, 2)]  # b: 1990, m: no file


def test_listened_population_is_the_songs_played_in_those_years():
    cands = {"a": cand("a") | {"listened": {2016: 2, 2017: 5}}, "b": cand("b") | {"listened": {2016: 3}},
             "c": cand("c")}
    r = rules([], rank="plays", years_of="listened")
    rows, _ = hr.select(cands, {}, r, wanted={2016})
    assert [(c["key"], c["score"], c["rank"]) for c in rows] == [("b", 3, 1), ("a", 2, 2)]


def test_rank_none_refuses_top_percent_but_takes_everything():
    r = rules(["+recommended"])
    assert r.rank == "none" and r.years_of == "release"
    assert hr.top_for(r, "1-100") is None and hr.top_for(r, None) is None
    with pytest.raises(ValueError, match="needs a rank"):
        hr.top_for(r, "1-10")


def test_unranked_rows_keep_their_order_and_get_unique_numbers():
    cands = {"r1": cand("r1", file=False) | {"order": (0, 1)}, "r0": cand("r0", file=False) | {"order": (0, 0)}}
    members = {"recommended": {"r0", "r1"}}
    rows, _ = hr.select(cands, members, rules(["+recommended"]))
    assert [(c["key"], c["rank"], c["ranked"]) for c in rows] == [("r0", 1, False), ("r1", 2, False)]


def test_n_cuts_only_without_top():
    cands = {k: cand(k, plays=i) for i, k in enumerate("abcd")}
    r = rules([], rank="plays", years_of="release")
    assert keys(hr.select(cands, {}, r, n=2)[0]) == ["d", "c"]
    assert keys(hr.select(cands, {}, r, n=2, top=[(1, 100)])[0]) == ["d", "c", "b", "a"]


# ---------------------------------------------------------------- --source as a shorthand

@pytest.mark.parametrize("source, sort, sets, rank, years_of", [
    ("billboard", "plays", {"billboard": 1}, "billboard", "chart"),
    ("likes", "rediscover", {"likes": 1}, "rediscover", "release"),
    ("library", "plays", {}, "plays", "release"),
    ("playlists", "plays", {"playlists": 1}, "plays", "release"),
    ("mine", "plays", {}, "plays", "listened"),
    ("recs", "plays", {"recommended": 1}, "none", "release"),
])
def test_source_maps_onto_sets_rank_and_years_of(source, sort, sets, rank, years_of):
    r = hr.resolve(source, None, None, None, sort)
    assert (r.sets, r.rank, r.years_of, r.source) == (sets, rank, years_of, source)
    assert hr.legacy_source(r) == source


def test_no_options_is_billboard_and_old_rank_values_still_read():
    r = hr.resolve(None, None, None, None)
    assert (r.sets, r.rank, r.years_of) == ({"billboard": 1}, "billboard", "chart")
    r = hr.resolve(None, None, "listens", None)
    assert (r.rank, r.order, r.source) == ("billboard", "listens", "billboard")
    assert hr.resolve(None, None, "chart", None).rank == "billboard"


def test_years_of_follows_the_rank_unless_given():
    assert hr.resolve(None, ["+likes"], "billboard", None).years_of == "chart"
    assert hr.resolve(None, ["+likes"], "plays", None).years_of == "listened"
    assert hr.resolve(None, ["+likes"], "rediscover", None).years_of == "release"
    assert hr.resolve(None, ["+likes"], "plays", "release").years_of == "release"
    assert hr.resolve(None, ["+likes"], None, None).rank == "plays"
    assert hr.resolve(None, ["+billboard", "+likes"], None, None).rank == "billboard"
    assert hr.legacy_source(hr.resolve(None, ["+billboard", "+likes"], None, None)) is None


def test_source_and_set_together_are_refused():
    with pytest.raises(ValueError, match="use one"):
        hr.resolve("likes", ["+billboard"], None, None)


# ---------------------------------------------------------------- formula

def test_formula_reads_the_rules_in_order():
    r = rules(["+billboard", "+likes", "-recommended"], rank="billboard")
    text = hr.formula(r, period="1980-1989", top=[(1, 10)], genre="+rock, -country", artist="+Queen; +Toto",
                      owned=True)
    assert text == ("(Billboard ∪ Likes) − Recommended ∩ 1980-1989 ∩ Top 1-10% ∩ rock − country ∩ (Queen ∪ Toto)"
                    " ∩ owned")
    assert hr.formula(rules([], rank="plays")) == "Library"
    assert hr.formula(rules(["-likes", "-playlists"], rank="plays"), genre="rock -country") == (
        "Library − (Likes ∪ Playlists) ∩ rock − country")
    assert hr.summary("Library", 1204, 8312) == "Library · 1,204 of 8,312"
