"""`hits genres of`: what rormpc's "Pin genre…" and the playlist-name suggestions read."""
import argparse, json

from rormpc_tools import genres, hits

REC = {"file": "a/rec.mp3", "musicbrainz_trackid": "r1", "musicbrainz_artistid": "a1"}
ART = {"file": "a/art.mp3", "musicbrainz_artistid": "a2"}


def test_genres_of_keeps_order_provenance_and_pins(env, monkeypatch, capsys, tmp_path):
    env([REC, ART])
    monkeypatch.setattr(genres, "PINS", tmp_path / "hits-genres.json")
    monkeypatch.setattr(hits, "lb_metadata", lambda mbids: {"r1": {"tag": {"recording": [
        {"tag": "synth-pop", "count": 3, "genre_mbid": "g"}, {"tag": "pop", "count": 1, "genre_mbid": "g"}]}}})
    monkeypatch.setattr(hits, "cached", lambda name, fn: {"genres": [{"name": "rap", "count": 2}]} if name == "a-a2" else {})
    genres.save_pins({"version": 1, "pins": ["hip hop"], "aliases": genres.DEFAULT_ALIASES})
    genres.cmd_of(argparse.Namespace(files=["a/art.mp3", "missing.mp3", "a/rec.mp3"], json=True))
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == 1
    assert out["songs"] == [
        {"file": "a/art.mp3", "genres": ["hip hop"], "source": "artist", "pinned": ["hip hop"]},  # rap -> hip hop
        {"file": "a/rec.mp3", "genres": ["synth-pop"], "source": "recording", "pinned": []},  # 1-vote pop dropped
    ]


def test_soundtrack_tags_count_as_a_genre(env, monkeypatch, capsys, tmp_path):
    """MusicBrainz lists no soundtrack genre: a film composer's "soundtrack" tag (3 votes) beside the genre
    "classical" (2 votes) must reach the explorer and the Hits filter."""
    env([ART])
    monkeypatch.setattr(genres, "PINS", tmp_path / "hits-genres.json")
    monkeypatch.setattr(hits, "lb_metadata", lambda mbids: {})
    artist = {"genres": [{"name": "classical", "count": 2}, {"name": "jazz", "count": 1}],
              "tags": [{"name": "classical", "count": 2}, {"name": "soundtrack", "count": 3},
                       {"name": "film composer", "count": 1}]}
    monkeypatch.setattr(hits, "cached", lambda name, fn: artist if name == "a-a2" else {})
    genres.cmd_of(argparse.Namespace(files=["a/art.mp3"], json=True))
    song = json.loads(capsys.readouterr().out)["songs"][0]
    assert song["genres"] == ["classical", "soundtrack"]
    assert song["pinned"] == ["classical", "soundtrack"]  # a default checkbox
    assert hits.artist_genres("a2") == ["soundtrack", "classical"]
    assert hits.genre_filter("film score")(["soundtrack"])  # the alias finds it under its other name


def test_tag_genres_keep_the_input_shape_and_the_most_votes():
    out = genres.with_tag_genres([{"tag": "rock", "count": 2, "genre_mbid": "g"}],
                                 [{"tag": "Film Score", "count": 1}, {"tag": "soundtrack", "count": 4}, {"tag": "epic", "count": 9}])
    assert out == [{"tag": "rock", "count": 2, "genre_mbid": "g"}, {"name": "soundtrack", "count": 4}]


def test_an_artist_soundtrack_tag_counts_only_when_it_defines_the_artist():
    band = [{"name": "rock", "count": 12}]
    assert genres.with_tag_genres(band, [{"name": "soundtrack", "count": 2}], artist=True) == band
    assert genres.with_tag_genres(band, [{"name": "soundtrack", "count": 12}], artist=True)[-1]["name"] == "soundtrack"
