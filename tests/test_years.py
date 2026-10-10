"""musicdb years: the hybrid rule on fake MusicBrainz answers (the plan's sample cases), the report's stable ids and
decisions, and apply / rollback on temporary files. No network: mbtag.http answers from the dicts below."""
import json, shutil, subprocess, types, urllib.parse

import pytest
from mutagen.id3 import ID3, TDOR, TDRC, TXXX

from rormpc_tools import mbtag, musicdb, years

A, B = "artist-a", "artist-b"


def credit(artist):
    return [{"name": artist, "artist": {"id": artist}}]


def rec(rid, title, length, frd, artist=A, video=False, dis="", works=()):
    return {"id": rid, "title": title, "length": length, "first-release-date": frd, "video": video,
            "disambiguation": dis, "artist-credit": credit(artist),
            "relations": [{"type": "performance", "target-type": "work", "attributes": list(at), "work": {"id": w}}
                          for w, at in works]}


def release(date, title="Album", primary="Album", secondary=(), rg_date=None, status="Official"):
    return {"id": f"rel-{title}-{date}", "title": title, "date": date, "status": status,
            "release-group": {"id": f"rg-{title}", "primary-type": primary, "secondary-types": list(secondary),
                              "first-release-date": rg_date or date}}


class FakeMB:
    """Recordings (with their work links), works (their recordings), release lists; counts requests."""

    def __init__(self):
        self.recs, self.works, self.rels, self.calls = {}, {}, {}, []

    def add(self, r, rels=(), work_attrs=None):
        self.recs[r["id"]] = r
        self.rels[r["id"]] = {"releases": list(rels)}
        for rel in r["relations"]:
            w = self.works.setdefault(rel["work"]["id"], {"id": rel["work"]["id"], "relations": []})
            w["relations"].append({"type": "performance", "target-type": "recording",
                                   "attributes": rel["attributes"] if work_attrs is None else work_attrs,
                                   "recording": {k: r[k] for k in ("id", "title", "length", "video", "disambiguation")}})
        return r

    def http(self, url, **_):
        self.calls.append(url)
        u = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qs(u.query)
        parts = u.path.split("/")
        if parts[-2] == "recording" and "work-rels" in q["inc"][0]:
            r = self.recs.get(parts[-1])
            return json.loads(json.dumps(r)) if r else None
        if parts[-2] == "recording":
            return self.rels.get(parts[-1])
        if parts[-2] == "work":
            return self.works.get(parts[-1])
        if parts[-1] == "recording":
            ids = [x.strip().removeprefix("rid:") for x in q["query"][0].split(" OR ")]
            return {"recordings": [{k: v for k, v in self.recs[i].items() if k != "relations"} for i in ids if i in self.recs]}
        raise AssertionError(url)


@pytest.fixture
def mb(tmp_path, monkeypatch):
    fake = FakeMB()
    monkeypatch.setattr(mbtag, "CACHE", tmp_path / "ytmb")
    (tmp_path / "ytmb").mkdir()
    monkeypatch.setattr(mbtag, "http", fake.http)
    return fake


def sweet_dreams(mb):
    """A January 1983 single matched to its music-video recording, first released on a 2000 video compilation."""
    mb.add(rec("audio", "Sweet Dreams", 216_000, "1983-01-04", works=[("w1", ())]),
           [release("1983-01-04", "Sweet Dreams", "Single"), release("1983-01-04", "Sweet Dreams LP")])
    mb.add(rec("remaster", "Sweet Dreams (2005 remaster)", 216_500, "2005", works=[("w1", ())]),
           [release("2005", "Ultimate", "Album", ["Compilation"])])
    return mb.add(rec("video", "Sweet Dreams", 216_000, "2000", video=True, works=[("w1", ())]),
                  [release("2000", "Greatest Hits", "Album", ["Compilation"])])


def test_a_video_match_takes_the_same_length_audio_recordings_studio_release(mb):
    sweet_dreams(mb)
    c = years.compute("video")
    assert (c["tdor"], c["rule"], c["source"], c["tdrc_own"]) == ("1983-01-04", "same-length", "audio", "2000")
    assert "video" in c["classes"] and 'source `audio` "Sweet Dreams" on Single' in c["evidence"]
    assert years.confidence(c, 2000) == "high"


def test_a_music_video_dated_by_a_later_best_of_dvd_goes_back_to_the_single(mb):
    """Physical: the 1981 single tagged 2004 (its "music video" recording's first release, a best-of DVD)."""
    mb.add(rec("phys", "Physical", 223_000, "1981-09", works=[("w2", ())]),
           [release("1981-10-13", "Physical"), release("1981-09-26", "Physical", "Single"),
            release("1998", "Gold", "Album", ["Compilation"])])
    mb.add(rec("phys-v", "Physical", 224_000, "2004", video=True, dis="music video", works=[("w2", ())]),
           [release("2004", "Video Hits", "Album", ["Compilation"])])
    c = years.compute("phys-v")
    assert (c["tdor"], c["rule"], c["source"]) == ("1981-09-26", "same-length", "phys")


def test_a_video_longer_than_every_audio_take_falls_back_to_any_clean_in_review(mb):
    mb.add(rec("audio", "Song", 216_000, "1990-03", works=[("w", ())]), [release("1990-03-01", "Song", "Single")])
    mb.add(rec("video", "Song", 260_000, "2003", video=True, works=[("w", ())]))
    c = years.compute("video")
    assert (c["tdor"], c["rule"]) == ("1990-03-01", "any-clean")
    assert years.confidence(c, 2003) == "review"


def test_a_remix_keeps_its_own_release_not_the_originals(mb):
    mb.add(rec("orig", "Song", 200_000, "2012", works=[("w", ())]), [release("2012-02-01", "Song", "Single")])
    mb.add(rec("remix", "Song (Club remix)", 300_000, "2014", works=[("w", ())]),
           [release("2014-05-01", "Song (Remixes)", "Single", ["Remix"]),
            release("2013", "Dance 2013", "Album", ["Compilation"])])
    c = years.compute("remix")
    assert (c["tdor"], c["rule"], c["source"]) == ("2014-05-01", "own", "remix")
    assert "version" in c["classes"]


def test_a_cover_by_another_singer_is_not_collapsed_into_the_original(mb):
    mb.add(rec("orig", "Song", 200_000, "2010", works=[("w", ())]), [release("2010", "Original")])
    mb.add(rec("cover", "Song", 201_000, "2016", artist=B, works=[("w", ("cover",))]), [release("2016-06-01", "Covers")])
    c = years.compute("cover")
    assert (c["tdor"], c["rule"]) == ("2016-06-01", "own")


def test_a_cover_without_the_attribute_is_still_not_another_artists_original(mb):
    mb.add(rec("orig", "Song", 200_000, "2010", works=[("w", ())]), [release("2010", "Original")])
    mb.add(rec("cover", "Song", 201_000, "2016", artist=B, works=[("w", ())]), [release("2016-06-01", "Covers")])
    c = years.compute("cover")
    assert (c["tdor"], c["source"]) == ("2016-06-01", "cover")


def test_a_dj_mix_segment_of_a_cover_takes_the_artists_own_single(mb):
    mb.add(rec("orig", "Ain't Nobody", 280_000, "1983", works=[("w", ())]), [release("1983", "Original", "Single")])
    mb.add(rec("single", "Ain't Nobody", 186_000, "2015-03", artist=B, works=[("w", ("cover",))]),
           [release("2015-03-20", "Ain't Nobody", "Single")])
    mb.add(rec("seg", "Ain't Nobody", 180_000, "2016-07-01", artist=B, dis="part of a DJ-mix", works=[("w", ("cover",))]),
           [release("2016-07-01", "Festivals", "Album", ["Compilation", "DJ-mix"])])
    c = years.compute("seg")
    assert (c["tdor"], c["rule"], c["source"]) == ("2015-03-20", "same-length", "single")


def test_a_mix_of_the_same_audio_is_no_version(mb):
    assert years.version_word({"title": "Song (Dolby Atmos mix)"}) is None
    assert years.version_word({"title": "Song", "disambiguation": "radio edit"}) is None
    assert years.version_word({"title": "Song (2011 Remaster)"}) is None
    assert years.version_word({"title": "Song", "disambiguation": "live, 1985-07-13: Wembley"}) == "live"
    assert years.version_word({"title": "Song (Taylor's Version)"}) == "taylor's version"


def test_demos_and_compilations_do_not_date_an_album_track(mb):
    mb.add(rec("album", "Song", 240_000, "1991-09", works=[("w", ())]), [release("1991-09-24", "Album")])
    mb.add(rec("demo", "Song (demo)", 238_000, "1986", works=[("w", ())]), [release("1986", "Demos", "Album", ["Demo"])])
    mb.add(rec("boot", "Song", 241_000, "1987", dis="bootleg", works=[("w", ())]), [release("1987", "Boot", status="Bootleg")])
    mb.add(rec("v", "Song", 240_000, "2005", video=True, works=[("w", ())]))
    c = years.compute("v")
    assert (c["tdor"], c["source"]) == ("1991-09-24", "album")


def test_a_reissue_only_recording_shows_the_earlier_release_group_as_evidence(mb):
    mb.add(rec("re", "Song", 180_000, "1990", works=[("w", ())]),
           [release("1990", "Song (reissue)", rg_date="1963-03-22")])
    c = years.compute("re")
    assert c["tdor"] == "1990" and c["rg_date"] == "1963-03-22" and "rg-earlier" in c["classes"]
    assert years.confidence(c, 1995) == "review"
    assert "RG 1963-03-22" in c["evidence"]
    row = years.build_row("re.mp3", "re", {"TDRC": "1990", "TDOR": "1990"}, "md5", c)  # the year holds, still listed
    assert row["proposed"] is None and "keeps-year" in row["classes"]


def test_no_work_link_needs_an_mbid(mb):
    mb.add(rec("lonely", "Song", 200_000, "2000"), [release("2000")])
    c = years.compute("lonely")
    assert c["tdor"] is None and c["needs_mbid"] and "no-work" in c["classes"]


def test_a_later_proposal_is_never_high(mb):
    mb.add(rec("a", "Song", 200_000, "1972", works=[("w", ())]), [release("1972-05-01", "Song", "Single")])
    c = years.compute("a")
    assert years.confidence(c, 1970) == "review"


def test_a_second_run_reads_only_the_cache(mb):
    sweet_dreams(mb)
    first = years.compute("video")
    n = len(mb.calls)
    assert n > 0
    assert years.compute("video") == first
    assert len(mb.calls) == n


def test_a_download_gets_the_original_in_tdor_and_its_own_first_release_in_tdrc(mb):
    sweet_dreams(mb)
    t = ID3()
    mbtag.write_year(t, "2000", years.for_download({"mbid": "video", "first_release": "2000"}), "video")
    assert (str(t["TDRC"]), str(t["TDOR"])) == ("2000", "1983-01-04")
    assert (str(t["TXXX:DATE_SOURCE"]), str(t["TXXX:DATE_RULE"])) == ("musicbrainz:audio", "same-length")


def test_a_download_keeps_its_first_release_when_the_rule_proposes_a_later_one(mb):
    mb.add(rec("a", "Song", 200_000, "1970", works=[("w", ())]),
           [release("1972", "Song"), release("1970", "Hits", "Album", ["Compilation"])])
    assert years.for_download({"mbid": "a", "first_release": "1970"}) is None
    t = ID3()
    mbtag.write_year(t, "1970", None, "a")
    assert (str(t["TDOR"]), str(t["TXXX:DATE_RULE"]), str(t["TXXX:DATE_SOURCE"])) == ("1970", "first-release", "musicbrainz:a")


def test_hits_reads_originaldate_before_date(monkeypatch):
    from rormpc_tools import hits

    class C:
        def connect(self, *_): pass
        def sticker_find(self, *_): return []
        def listallinfo(self):
            return [{"file": "a.mp3", "date": "2000", "originaldate": "1983-01-04"}, {"file": "b.mp3", "date": "1999"}]
    monkeypatch.setitem(__import__("sys").modules, "mpd", types.SimpleNamespace(MPDClient=C))
    songs = hits.library_songs()
    assert (songs["a.mp3"]["year"], songs["b.mp3"]["year"]) == (1983, 1999)


# ------------------------------------------------------------------ report, decisions, apply, rollback

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def mp3(path, freq, date, mbid=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1", "-y", str(path)],
                   check=True)
    t = ID3()
    t.add(TDRC(text=[date])); t.add(TDOR(text=[date])); t.add(TXXX(desc="YouTube ID", text=["x"]))
    t.save(path)


@pytest.fixture
def lib(env, mb, tmp_path, monkeypatch):
    music = tmp_path / "music"
    monkeypatch.setattr(musicdb, "MUSIC", music)
    updates = []
    monkeypatch.setattr(years, "mpc_update", lambda dirs: updates.append(sorted(dirs)))
    sweet_dreams(mb)
    mb.add(rec("lonely", "Other", 200_000, "2001"), [release("2001")])
    mp3(music / "eu/Sweet Dreams.mp3", 440, "2000")
    mp3(music / "Other.mp3", 550, "2001")
    mp3(music / "Plain.mp3", 660, "1983")
    env([{"file": "eu/Sweet Dreams.mp3", "musicbrainz_trackid": "video"},
         {"file": "Other.mp3", "musicbrainz_trackid": "lonely"}, {"file": "Plain.mp3", "musicbrainz_trackid": "audio"},
         {"file": "Unknown.mp3"}])
    mp3(music / "Unknown.mp3", 770, "1999")
    return types.SimpleNamespace(music=music, updates=updates, mb=mb)


def run(*argv):
    years.main(list(argv))


def report():
    return json.loads(years.report_path().read_text())


def rows():
    return {r["file"]: r for r in report()["rows"]}


@needs_ffmpeg
def test_the_dry_run_report_starts_undecided_and_keeps_ids(lib):
    run("--dry-run")
    r = rows()
    assert set(r) == {"eu/Sweet Dreams.mp3", "Other.mp3", "Unknown.mp3"}  # Plain.mp3 keeps its year
    sd = r["eu/Sweet Dreams.mp3"]
    assert sd["proposed"] == {"TDRC": "2000", "TDOR": "1983-01-04", "DATE_SOURCE": "musicbrainz:audio",
                              "DATE_RULE": "same-length"}
    assert (sd["confidence"], sd["decision"], sd["needs_mbid"]) == ("high", "undecided", False)
    assert sd["current"]["TDRC"] == "2000" and sd["md5"]
    assert r["Other.mp3"]["needs_mbid"] and r["Unknown.mp3"]["needs_mbid"]
    assert all(x["decision"] == "undecided" for x in r.values())
    rep = report()
    assert rep["counts"]["confidence"]["high"] == 1 and rep["counts"]["class"]["video"] == 1
    assert "| class | rows |" in years.markdown_path().read_text()
    ids = {f: x["id"] for f, x in r.items()}
    run("--accept", str(ids["eu/Sweet Dreams.mp3"]))
    run("--dry-run")
    assert {f: x["id"] for f, x in rows().items()} == ids
    assert rows()["eu/Sweet Dreams.mp3"]["decision"] == "accepted"  # same proposal: the decision holds


@needs_ffmpeg
def test_a_row_without_a_proposal_cannot_be_accepted_and_an_mbid_by_hand_gives_one(lib):
    run("--dry-run")
    other = rows()["Other.mp3"]
    with pytest.raises(SystemExit):
        run("--accept", str(other["id"]))
    unknown, known = "aaaaaaaa-0000-0000-0000-000000000000", "bbbbbbbb-0000-0000-0000-000000000000"
    run("--mbid", str(other["id"]), unknown)
    assert "recording-not-found" in rows()["Other.mp3"]["classes"]  # an id MusicBrainz does not know: the row says so
    lib.mb.add(rec(known, "Other", 200_000, "1995", works=[("w9", ())]), [release("1995-02-02", "Other", "Single")])
    run("--mbid", str(other["id"]), known)
    o = rows()["Other.mp3"]
    assert (o["proposed"]["TDOR"], o["decision"], o["mbid_override"], o["id"]) == ("1995-02-02", "undecided", known,
                                                                                  other["id"])
    run("--dry-run")  # the hand-picked recording survives a rerun
    assert rows()["Other.mp3"]["proposed"]["TDOR"] == "1995-02-02"


@needs_ffmpeg
def test_apply_writes_only_accepted_unchanged_rows_and_rollback_restores(lib):
    run("--dry-run")
    sd = rows()["eu/Sweet Dreams.mp3"]
    path = lib.music / sd["file"]
    run("--apply")
    assert years.read_dates(path)["TDOR"] == "2000"  # undecided: nothing written
    run("--accept", str(sd["id"]))
    run("--apply")
    assert years.read_dates(path) == {"TDRC": "2000", "TDOR": "1983-01-04", "DATE_SOURCE": "musicbrainz:audio",
                                      "DATE_RULE": "same-length"}
    assert ID3(path)["TXXX:YouTube ID"].text == ["x"]  # other tags kept
    assert lib.updates == [["eu"]]
    assert not list(path.parent.glob(".*years-tmp"))
    backup = musicdb.jsonl(years.backup_path())
    assert backup[-1]["old"] == {"TDRC": "2000", "TDOR": "2000", "DATE_SOURCE": None, "DATE_RULE": None}
    assert rows()["eu/Sweet Dreams.mp3"]["applied"]
    run("--apply")  # applied once only
    assert len(musicdb.jsonl(years.backup_path())) == 1
    run("--rollback")
    assert years.read_dates(path) == {"TDRC": "2000", "TDOR": "2000", "DATE_SOURCE": None, "DATE_RULE": None}
    assert rows()["eu/Sweet Dreams.mp3"]["decision"] == "undecided"
    run("--rollback")  # nothing newer to undo: the file stays
    assert years.read_dates(path)["TDOR"] == "2000"


@needs_ffmpeg
def test_apply_skips_a_file_whose_tags_or_audio_changed(lib, capsys):
    run("--dry-run")
    sd = rows()["eu/Sweet Dreams.mp3"]
    run("--accept", str(sd["id"]))
    path = lib.music / sd["file"]
    t = ID3(path); t.delall("TDOR"); t.add(TDOR(text=["1984"])); t.save(path)
    capsys.readouterr()
    run("--apply", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["applied"] == [] and out["skipped"][0]["why"] == "date tags changed since the report"
    mp3(path, 880, "2000")  # other audio, the old tags
    run("--apply", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"][0]["why"] == "audio changed since the report"
    assert years.read_dates(path)["TDOR"] == "2000"


@needs_ffmpeg
def test_rollback_leaves_a_file_changed_after_the_apply(lib):
    run("--dry-run")
    sd = rows()["eu/Sweet Dreams.mp3"]
    run("--accept", str(sd["id"]))
    run("--apply")
    path = lib.music / sd["file"]
    t = ID3(path); t.delall("TDOR"); t.add(TDOR(text=["1982"])); t.save(path)
    run("--rollback")
    assert years.read_dates(path)["TDOR"] == "1982"


def test_flac_fields(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    p = tmp_path / "a.flac"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-y", str(p)], check=True)
    years.write_dates(p, {"TDRC": "2000", "TDOR": "1983", "DATE_SOURCE": "musicbrainz:x", "DATE_RULE": "own"})
    assert years.read_dates(p) == {"TDRC": "2000", "TDOR": "1983", "DATE_SOURCE": "musicbrainz:x", "DATE_RULE": "own"}
    years.write_dates(p, {"TDRC": "2000", "TDOR": None, "DATE_SOURCE": None, "DATE_RULE": None})
    assert years.read_dates(p)["TDOR"] is None
