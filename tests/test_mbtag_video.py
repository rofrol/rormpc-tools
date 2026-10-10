"""resolve() prefers the audio recording over a MusicBrainz music-video recording of the same song: the video's
first release is often a later video compilation, which then became the song's year (a 1983 song tagged 2000).
With no audio alternative among the candidates, the video (or DJ-mix segment) is swapped to its work's same-length
audio recording. No network: MusicBrainz answers come from test_years' FakeMB."""
import pytest

from rormpc_tools import mbtag
from test_years import FakeMB, rec, release

VIDEO = {"id": "v", "title": "Song", "video": True, "first-release-date": "2000",
         "artist-credit": [{"name": "Artist", "artist": {"id": "a"}}]}
AUDIO = {"id": "s", "title": "Song", "video": False, "first-release-date": "1983-01-04",
         "artist-credit": [{"name": "Artist", "artist": {"id": "a"}}]}


def evidence(alt="s", title="Song", artist="artist-a"):
    return {"ytid": "y", "channel": artist, "yt_title": f"{artist} - {title} (Official Video)", "duration": 216,
            "clean": f"{artist} - {title}", "provided": None, "cands": [(artist, title)], "shazam": {},
            "mb_url": [{"recording": "v"}], "acoustid": {"results": [
                {"score": 0.95, "mbid": alt, "title": title, "artists": [artist], "duration": 216}]}, "lb": []}


@pytest.fixture
def mb(tmp_path, monkeypatch):
    fake = FakeMB()
    monkeypatch.setattr(mbtag, "CACHE", tmp_path / "ytmb")
    (tmp_path / "ytmb").mkdir()
    monkeypatch.setattr(mbtag, "http", fake.http)
    monkeypatch.setattr(mbtag, "mb_recording", lambda i: fake.recs.get(i))
    return fake


def test_a_flagged_video_recording_gives_way_to_the_audio_one(monkeypatch):
    monkeypatch.setattr(mbtag, "mb_recording", {"v": VIDEO, "s": AUDIO}.get)
    row = mbtag.resolve(evidence())
    assert (row["mbid"], row["first_release"], row["video"]) == ("s", "1983-01-04", False)
    assert row["method"].endswith(">audio")
    assert row["swap"] == {"from": "v", "to": "s", "rule": "alternative"}


def test_a_video_without_an_audio_alternative_takes_the_works_same_length_recording(mb):
    """The 1983 single matched to its video; the remaster of the same length is only on a 2005 compilation, and
    another singer's recording of the work is no candidate."""
    mb.add(rec("v", "Song", 216_000, "2000", video=True, works=[("w", ())]),
           [release("2000", "Greatest Hits", "Album", ["Compilation"])])
    mb.add(rec("remaster", "Song (2005 remaster)", 216_500, "2005", works=[("w", ())]),
           [release("2005", "Ultimate", "Album", ["Compilation"])])
    mb.add(rec("audio", "Song", 215_000, "1983-01-04", works=[("w", ())]), [release("1983-01-04", "Song", "Single")])
    mb.add(rec("other", "Song", 216_000, "1979", artist="artist-b", works=[("w", ())]), [release("1979", "Song", "Single")])
    row = mbtag.resolve(evidence(alt="v"))
    assert (row["mbid"], row["video"], row["first_release"]) == ("audio", False, "1983-01-04")
    assert row["method"].endswith(">work") and row["status"] == "auto"
    assert row["swap"]["from"] == "v" and row["swap"]["to"] == "audio" and row["swap"]["rule"] == "work-same-length"


def test_a_video_keeps_its_match_when_no_audio_recording_is_within_10_s(mb):
    mb.add(rec("v", "Song", 260_000, "2003", video=True, works=[("w", ())]))
    mb.add(rec("audio", "Song", 216_000, "1990-03", works=[("w", ())]), [release("1990-03-01", "Song", "Single")])
    row = mbtag.resolve(evidence(alt="v"))
    assert (row["mbid"], row["video"]) == ("v", True)
    assert row["swap"]["to"] is None and "within 10 s" in row["swap"]["why"]


def test_a_dj_mix_segment_is_swapped_like_a_video(mb):
    mb.add(rec("v", "Song", 214_000, "2016-07-01", dis="part of a DJ-mix", works=[("w", ())]),
           [release("2016-07-01", "Festivals", "Album", ["Compilation", "DJ-mix"])])
    mb.add(rec("single", "Song", 216_000, "2015-03", works=[("w", ())]), [release("2015-03-20", "Song", "Single")])
    row = mbtag.resolve(evidence(alt="v"))
    assert (row["mbid"], row["first_release"]) == ("single", "2015-03")
    assert row["swap"]["rule"] == "work-same-length" and row["method"].endswith(">work")


def test_a_video_without_a_work_link_keeps_its_match_and_says_so(mb):
    mb.add(rec("v", "Song", 216_000, "2000", video=True))
    mb.add(rec("s", "Another Song", 216_000, "1983"))  # the only alternative is another song
    row = mbtag.resolve(evidence())
    assert (row["mbid"], row["video"]) == ("v", True)
    assert row["swap"] == {"from": "v", "to": None, "rule": None,
                           "why": "kept the match: no work relationship on MusicBrainz"}


def test_a_failed_work_lookup_keeps_the_match(mb, monkeypatch):
    mb.add(rec("v", "Song", 216_000, "2000", video=True, works=[("w", ())]))

    def broken(*_a, **_k):
        raise ValueError("unexpected answer")
    monkeypatch.setattr(mbtag, "http", broken)
    row = mbtag.resolve(evidence(alt="v"))
    assert row["mbid"] == "v" and "ValueError" in row["swap"]["why"]


def test_an_audio_match_is_not_looked_up_on_the_work(mb):
    mb.add(rec("v", "Song", 216_000, "1983-01-04", works=[("w", ())]))
    row = mbtag.resolve(evidence(alt="v"))
    assert row["mbid"] == "v" and "swap" not in row and mb.calls == []
