"""hits --artist: credit splitting, folding, include/exclude, and the picker's counts."""
from rormpc_tools import hits


def test_credit_members():
    assert hits.credit_members("Rihanna feat. Calvin Harris") == ["Rihanna", "Calvin Harris"]
    assert hits.credit_members("Mark Ronson (feat. Bruno Mars)") == ["Mark Ronson", "Bruno Mars"]
    assert hits.credit_members("Akon ft. Eminem") == ["Akon", "Eminem"]
    assert hits.credit_members("Queen") == ["Queen"]


def test_filter_includes_excludes_and_folds():
    ok = hits.artist_filter("+Queen -Madonna, Tiesto")
    assert ok("Queen") and ok("Tiësto") and ok("David Bowie & Queen")
    assert not ok("Queen Latifah")  # whole names, not substrings
    assert not ok("Madonna & Queen")  # an exclusion wins
    assert not ok("Toto")
    only_out = hits.artist_filter("-Rihanna")
    assert not only_out("Rihanna feat. Calvin Harris") and only_out("Toto")
    assert hits.artist_filter("")("anyone")


def test_count_artists_lists_members_and_duos():
    counts = hits.count_artists([{"artist": "Simon & Garfunkel"}, {"artist": "Rihanna feat. Calvin Harris"},
                                 {"artist": "Rihanna"}])
    assert counts["rihanna"] == ["Rihanna", 2]
    assert counts["simon & garfunkel"][1] == 1 and "simon" in counts
    assert "rihanna feat. calvin harris" not in counts


def test_semicolons_keep_commas_in_names():
    ok = hits.artist_filter("+Earth, Wind & Fire; -Toto")
    assert ok("Earth, Wind & Fire") and not ok("Toto") and not ok("Queen")
