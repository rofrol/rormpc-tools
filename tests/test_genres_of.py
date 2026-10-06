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
