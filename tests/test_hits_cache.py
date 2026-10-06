"""hits' cache: the seed shipped with the package fills a new cache once and never overwrites; raw searches are
kept compressed."""
import gzip, json

import pytest

from rormpc_tools import hits


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(hits, "CACHE", tmp_path / "hits")
    monkeypatch.setattr(hits, "_cache_db", None)
    seed = tmp_path / "seed.jsonl.gz"
    monkeypatch.setattr(hits, "SEED", seed)
    return seed


def write_seed(path, records):
    with gzip.open(path, "wt") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_seed_fills_a_new_cache_once_and_never_overwrites(cache):
    key = f"m{hits.MATCH_VERSION}-Toto-Africa-1983"
    write_seed(cache, [{"t": "mb", "k": key, "v": {"mbid": "seeded"}},
                       {"t": "lb", "k": "seeded", "v": 42, "at": 1000.0}])
    assert hits.cached(key, lambda: pytest.fail("the seed has it")) == {"mbid": "seeded"}
    assert hits.cache_db().execute("SELECT listens FROM lb_pop WHERE mbid = 'seeded'").fetchone() == (42,)
    # a local re-match is newer: a new seed revision adds only what is missing
    hits.cache_db().execute("UPDATE mb SET json = ? WHERE name = ?", (json.dumps({"mbid": "local"}), key))
    write_seed(cache, [{"t": "mb", "k": key, "v": {"mbid": "seeded2"}}, {"t": "mb", "k": "a-x", "v": ["rock"]}])
    hits._cache_db = None
    assert hits.cached(key, lambda: None) == {"mbid": "local"}
    assert hits.cached("a-x", lambda: None) == ["rock"]


def test_export_writes_matches_and_popularity_not_raw_searches(cache, tmp_path):
    hits.cached(f"m{hits.MATCH_VERSION}-A-B-1990", lambda: {"mbid": "m1"})
    hits.cached("s-A-B", lambda: {"recordings": [{"title": "B"}] * 50})
    hits.cache_db().execute("INSERT INTO lb_pop VALUES ('m1', 7, 5.0)")
    out = tmp_path / "out.jsonl.gz"
    hits.export_seed(out)
    recs = [json.loads(line) for line in gzip.open(out, "rt")]
    assert {(r["t"], r["k"]) for r in recs} == {("mb", f"m{hits.MATCH_VERSION}-A-B-1990"), ("lb", "m1")}




def test_raw_searches_round_trip_compressed(cache):
    v = {"recordings": [{"title": "x" * 100}] * 20}
    hits.cached("s-big", lambda: v)
    raw = hits.cache_db().execute("SELECT json FROM mb WHERE name = 's-big'").fetchone()[0]
    assert isinstance(raw, bytes) and len(raw) < 1000
    assert hits.cached("s-big", lambda: None) == v
