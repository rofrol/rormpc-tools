"""resolve() prefers the audio recording over a MusicBrainz music-video recording of the same song: the video's
first release is often a later video compilation, which then became the song's year (a 1983 song tagged 2000)."""
from rormpc_tools import mbtag

VIDEO = {"id": "v", "title": "Song", "video": True, "first-release-date": "2000",
         "artist-credit": [{"name": "Artist", "artist": {"id": "a"}}]}
AUDIO = {"id": "s", "title": "Song", "video": False, "first-release-date": "1983-01-04",
         "artist-credit": [{"name": "Artist", "artist": {"id": "a"}}]}


def evidence():
    return {"ytid": "y", "channel": "Artist", "yt_title": "Artist - Song (Official Video)", "duration": 216,
            "clean": "Artist - Song", "provided": None, "cands": [("Artist", "Song")], "shazam": {},
            "mb_url": [{"recording": "v"}], "acoustid": {"results": [
                {"score": 0.95, "mbid": "s", "title": "Song", "artists": ["Artist"], "duration": 216}]}, "lb": []}


def test_a_flagged_video_recording_gives_way_to_the_audio_one(monkeypatch):
    monkeypatch.setattr(mbtag, "mb_recording", {"v": VIDEO, "s": AUDIO}.get)
    row = mbtag.resolve(evidence())
    assert (row["mbid"], row["first_release"], row["video"]) == ("s", "1983-01-04", False)
    assert row["method"].endswith(">audio")


def test_a_video_recording_stays_when_no_audio_alternative_matches(monkeypatch):
    other = dict(AUDIO, title="Another Song")
    monkeypatch.setattr(mbtag, "mb_recording", {"v": VIDEO, "s": other}.get)
    row = mbtag.resolve(evidence())
    assert (row["mbid"], row["video"]) == ("v", True)
