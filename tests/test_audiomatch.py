"""Audio fingerprints in Versions groups: copies of one recording are suggested for merging (never merged), the
scores come from the cache only, and fpcalc is never needed here (fake fingerprints)."""
import os, random

from rormpc_tools import audiomatch, musicdb, versions

OFFICIAL = "Pop/Song (Remix).mp3"
REUPLOAD = "Pop/Song (Full Remix).mp3"
LIVE = "Pop/Song (Live).mp3"
SONGS = [{"file": OFFICIAL, "artist": "Singer", "title": "Song (Remixer remix)", "duration": "139"},
         {"file": REUPLOAD, "artist": "Singer", "title": "Song (Remixer full remix)", "duration": "137"},
         {"file": LIVE, "artist": "Singer", "title": "Song (Live)", "duration": "250"}]


def fingerprint(seed, n=950):
    r = random.Random(seed)
    return [r.getrandbits(32) for _ in range(n)]


def noisy(fp, share, seed=1):
    """fp with about `share` of its bits flipped (a re-encode, a bass boost)."""
    r = random.Random(seed)
    return [x ^ sum(1 << b for b in range(32) if r.random() < share) for x in fp]


def test_scores_copies_high_over_the_overlap_only():
    a = fingerprint(1)
    assert audiomatch.compare(a, a)[:2] == (1.0, 0)
    score, shift, n = audiomatch.compare(a, [7] * 20 + a)  # a 2.5 s intro on the second file
    assert (score, shift) == (1.0, -20) and n == len(a)
    assert audiomatch.compare(a, a[:500])[0] == 1.0  # a trimmed end does not lower the score
    assert audiomatch.compare(a, noisy(a, 0.07))[0] > audiomatch.SAME
    assert audiomatch.compare(a, fingerprint(2))[0] < audiomatch.SIMILAR  # another recording: about half the bits
    assert audiomatch.compare(a, a[:100]) == (0.0, 0, 0)  # too short to say anything


def files_on_disk(tmp_path, monkeypatch):
    music = tmp_path / "music"
    for s in SONGS:
        (music / s["file"]).parent.mkdir(parents=True, exist_ok=True)
        (music / s["file"]).write_bytes(s["file"].encode())
    monkeypatch.setattr(musicdb, "MUSIC", music)
    monkeypatch.setattr(audiomatch, "CACHE", tmp_path / "fingerprints.json")
    return music


def fake_fpcalc(calls):
    base = fingerprint(1)
    fps = {OFFICIAL: base, REUPLOAD: noisy(base, 0.07), LIVE: fingerprint(3)}

    def run(path):
        rel = str(path.relative_to(musicdb.MUSIC))
        calls.append(rel)
        return fps.get(rel)
    return run


def no_tags(monkeypatch, channels=None):
    channels = channels or {}
    monkeypatch.setattr(versions, "channel_info", lambda rel: (channels.get(rel, ""), "", 128))


def test_json_reads_the_cache_only_and_lists_what_is_missing(env, tmp_path, monkeypatch):
    env(SONGS)
    files_on_disk(tmp_path, monkeypatch)
    no_tags(monkeypatch)
    monkeypatch.setattr(audiomatch, "run_fpcalc", lambda p: 1 / 0)  # a reader must never fingerprint
    g, = versions.groups(include_all=True)
    assert sorted(g["audio"]["missing"]) == sorted(s["file"] for s in SONGS)
    assert g["audio"]["same"] == [] and g["audio"]["pairs"] == []


def test_a_copy_is_suggested_with_the_file_to_keep_and_why(env, tmp_path, monkeypatch):
    env(SONGS)
    files_on_disk(tmp_path, monkeypatch)
    no_tags(monkeypatch, {OFFICIAL: "Remixer", REUPLOAD: "Some_Reuploads"})
    calls = []
    assert audiomatch.compute([s["file"] for s in SONGS], fpcalc=fake_fpcalc(calls)) == (3, 0)
    g, = versions.groups(include_all=True)
    assert g["audio"]["missing"] == []
    s, = g["audio"]["same"]
    assert s["files"] == sorted([OFFICIAL, REUPLOAD]) and s["keep"] == OFFICIAL
    assert s["keep_reason"] == "official channel (Remixer)"  # the remixer's own upload, though not the longest
    assert s["reason"].startswith(f"audio match {round(s['score'] * 100)}% over the first")
    assert all(LIVE not in (p["a"], p["b"]) for p in g["audio"]["pairs"])  # another performance: nothing
    # cached: a second run fingerprints nothing; a changed file is fingerprinted again
    audiomatch.compute([s["file"] for s in SONGS], fpcalc=fake_fpcalc(calls))
    assert len(calls) == 3
    p = musicdb.MUSIC / REUPLOAD
    os.utime(p, (1, 1))
    assert REUPLOAD in versions.groups(include_all=True)[0]["audio"]["missing"]
    audiomatch.compute([s["file"] for s in SONGS], fpcalc=fake_fpcalc(calls))
    assert calls[3:] == [REUPLOAD]


def test_an_unreadable_file_is_not_retried_until_it_changes(env, tmp_path, monkeypatch):
    env(SONGS)
    files_on_disk(tmp_path, monkeypatch)
    calls = []
    run = lambda p: calls.append(p) and None
    assert audiomatch.compute([LIVE], fpcalc=run) == (0, 1)
    assert audiomatch.compute([LIVE], fpcalc=run) == (0, 0)
    assert len(calls) == 1
    assert audiomatch.cached([LIVE]) == ({}, [], [LIVE])


def test_keep_prefers_official_then_longer_then_bitrate_then_plays(monkeypatch):
    rows = [{"file": "a.mp3", "artist": "Singer", "title": "Song", "duration_s": 200, "mbid": None, "plays": 9},
            {"file": "b.mp3", "artist": "Singer", "title": "Song", "duration_s": 230, "mbid": None, "plays": 0}]
    kbps = {"a.mp3": 320, "b.mp3": 128}
    monkeypatch.setattr(versions, "channel_info", lambda rel: ("", "", kbps[rel]))
    assert versions.keeper(rows) == ("b.mp3", "longer: 230 s vs 200 s")
    rows[1]["duration_s"] = 201  # about the same length: the bitrate decides
    assert versions.keeper(rows) == ("a.mp3", "higher bitrate: 320 vs 128 kbps")
    kbps["a.mp3"] = 128
    assert versions.keeper(rows) == ("a.mp3", "more plays: 9 vs 0")
    rows[1]["mbid"] = "mb-1"
    assert versions.keeper(rows) == ("b.mp3", "has a MusicBrainz id")
    rows[1].update(mbid=None, duration_s=800)  # a loop with the same start is not an untrimmed copy
    assert versions.keeper(rows) == ("a.mp3", "more plays: 9 vs 0")


def test_official_channel():
    assert versions.official("Singer - Topic", "", "Singer", "Song")
    assert versions.official("SingerVEVO", "", "Other", "Song")
    assert versions.official("Remixer", "", "Singer", "Song (Remixer remix)")
    assert versions.official("Singer_Official", "", "Singer", "Song")
    assert versions.official("", "Provided to YouTube by Label", "Singer", "Song")
    assert not versions.official("Some_Reuploads", "", "Singer", "Song (Remixer remix)")
    assert not versions.official("", "", "Singer", "Song")


def pair(a, b, score):
    return {"a": a, "b": b, "score": score, "kind": audiomatch.kind(score), "shift_s": 0.0, "overlap_s": 117}


def test_files_labelled_as_different_versions_are_not_suggested_as_one(monkeypatch):
    monkeypatch.setattr(versions, "channel_info", lambda rel: ("", "", 128))
    rows = [{"file": f, "artist": "Singer", "title": "Song", "duration_s": d, "mbid": None, "plays": 0, "version": v}
            for f, d, v in (("a.mp3", 200, "original"), ("b.mp3", 205, "live"), ("c.mp3", 840, None))]
    assert versions.same_suggestions([pair("a.mp3", "b.mp3", 0.95)], rows) == []
    s, = versions.same_suggestions([pair("a.mp3", "c.mp3", 0.93)], rows)
    assert "lengths differ a lot (200 s vs 840 s)" in s["reason"]
    assert versions.same_suggestions([pair("a.mp3", "c.mp3", 0.8)], rows) == []  # similar only: no merge
