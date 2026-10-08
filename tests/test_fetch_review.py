"""`hits fetch` review decisions: accept as the chart song, a missing staged file, rejected videos skipped on retry,
and the JSON rormpc's review reads. Offline: a synthetic MP3 in a temp dir, no YouTube, no MPD."""
import argparse, json

import pytest
from mutagen.id3 import ID3, TPE1, TIT2, TXXX, UFID

from rormpc_tools import fetch


def mp3(path, seconds=10, artist="some uploader", title="ARTIST ❖ song (official video)"):
    """Silent MPEG-1 Layer III frames (128 kb/s, 44.1 kHz) plus ID3 tags, as yt-dlp + mbtag leave a download."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413
    path.write_bytes(frame * round(seconds * 44100 / 1152))
    t = ID3()
    t.add(TPE1(encoding=3, text=[artist])); t.add(TIT2(encoding=3, text=[title]))
    t.add(TXXX(encoding=3, desc="YouTube Channel", text=[artist]))
    t.add(UFID(owner="http://musicbrainz.org", data=b"other-recording"))
    t.save(path)
    return path


@pytest.fixture
def q(tmp_path, monkeypatch):
    state = tmp_path / "state"
    for name, value in {"STATE_DIR": state, "QUEUE": state / "queue.json", "STAGING": state / "staging",
                        "LOCK": state / "queue.lock", "MUSIC": tmp_path / "music"}.items():
        monkeypatch.setattr(fetch, name, value)
    monkeypatch.setattr(fetch.subprocess, "run", lambda *a, **k: None)  # no `mpc update`
    (state / "staging").mkdir(parents=True)

    def item(**fields):
        it = {"key": "k1", "artist": "Captain & Tennille", "title": "Love Will Keep Us Together", "year": 1975,
              "mbid": "k1", "state": "review", "reason": "no MusicBrainz match",
              "candidate": "some uploader · ARTIST ❖ song (official video) · 226 s",
              "url": "https://www.youtube.com/watch?v=vid1", **fields}
        with fetch.locked():
            fetch.save({"version": 1, "items": [it]})
        return it
    return item


def args(*keys, as_chart=False):
    return argparse.Namespace(keys=list(keys), as_chart=as_chart)


def stored():
    return fetch.load()["items"][0]


def test_accept_as_chart_writes_chart_names_and_youtube_comment_never_an_mbid(q, tmp_path):
    staged = mp3(fetch.STAGING / "some_uploader--ARTIST_song--vid1--20121020.mp3")
    q(file=str(staged))
    fetch.decide(args("k1", as_chart=True), "accept")
    it = stored()
    dest = tmp_path / "music/Hits/1970s/Captain_&_Tennille--Love_Will_Keep_Us_Together--vid1--20121020.mp3"
    assert it["state"] == "ok" and it["file"] == str(dest) and dest.exists() and not staged.exists()
    t = ID3(dest)
    assert str(t["TPE1"]) == "Captain & Tennille" and str(t["TIT2"]) == "Love Will Keep Us Together"
    assert not t.getall("UFID") and not t.getall("TXXX:MusicBrainz Artist Id")
    assert str(t["TXXX:YouTube Channel"]) == "some uploader"
    comment = str(t.getall("COMM")[0])
    assert comment == "YouTube: some uploader · ARTIST ❖ song (official video) · https://www.youtube.com/watch?v=vid1"


def test_plain_accept_keeps_the_current_tags(q, tmp_path):
    staged = mp3(fetch.STAGING / "some_uploader--ARTIST_song--vid1--.mp3")
    q(file=str(staged))
    fetch.decide(args("k1"), "accept")
    dest = tmp_path / "music/Hits/1970s" / staged.name
    assert stored()["state"] == "ok" and str(ID3(dest)["TPE1"]) == "some uploader"


def test_accept_fails_and_keeps_review_when_the_staged_file_is_gone(q):
    q(file=str(fetch.STAGING / "gone.mp3"))
    with pytest.raises(SystemExit) as e:
        fetch.decide(args("k1", as_chart=True), "accept")
    assert "staged file is gone" in str(e.value)
    assert stored()["state"] == "review"


def test_reject_remembers_the_video_and_retry_takes_the_next_candidate(q, monkeypatch):
    staged = mp3(fetch.STAGING / "x--y--vid1--.mp3")
    q(file=str(staged))
    fetch.decide(args("k1"), "reject")
    it = stored()
    assert it["state"] == "rejected" and it["rejected_ids"] == ["vid1"] and not staged.exists()
    fetch.decide(args("k1"), "retry")
    assert stored()["state"] == "queued"

    ranked = [(3, {"id": "vid1", "channel": "some uploader", "title": "a"}, "first"),
              (2, {"id": "vid2", "channel": "Captain & Tennille - Topic", "title": "b"}, "second")]
    monkeypatch.setattr(fetch, "candidates", lambda item: (ranked, 226, []))
    monkeypatch.setattr(fetch.yt_mp3_mb, "download", lambda urls, extra: [])  # stop after the choice
    fields = fetch.fetch_one(stored())
    it = stored()
    assert it["url"] == "https://www.youtube.com/watch?v=vid2" and it["channel"] == "Captain & Tennille - Topic"
    assert it["chart_length"] == 226 and fields["state"] == "failed"

    with fetch.locked():
        data = fetch.load()
        data["items"][0]["rejected_ids"] = ["vid1", "vid2"]
        fetch.save(data)
    fields = fetch.fetch_one(stored())
    assert fields["state"] == "failed" and "all 2 were rejected" in fields["error"]


def test_another_candidate_drops_the_staged_file_and_queues_again(q):
    staged = mp3(fetch.STAGING / "x--y--vid1--.mp3")
    q(file=str(staged))
    fetch.decide(args("k1"), "another")
    it = stored()
    assert it["state"] == "queued" and it["rejected_ids"] == ["vid1"] and it["file"] is None and not staged.exists()


def test_status_json_carries_what_the_review_shows(q, capsys):
    staged = mp3(fetch.STAGING / "x--y--vid1--.mp3", seconds=10)
    q(file=str(staged), chart_length=203)
    fetch.status(argparse.Namespace(json=True))
    it = json.loads(capsys.readouterr().out)["items"][0]
    assert (it["channel"], it["video_title"], it["video_id"]) == ("some uploader", "ARTIST ❖ song (official video)", "vid1")
    assert it["chart_length"] == 203
    assert it["staged"]["exists"] and it["staged"]["artist"] == "some uploader" and it["staged"]["length"] == 10
    assert it["tags_from_channel"] is True
    with fetch.locked():  # the artist's own channel: the tags are the artist's, not an uploader's
        data = fetch.load()
        data["items"][0]["candidate"] = "Captain & Tennille · Love Will Keep Us Together · 226 s"
        fetch.save(data)
    mp3(staged, artist="Captain & Tennille")
    fetch.status(argparse.Namespace(json=True))
    assert json.loads(capsys.readouterr().out)["items"][0]["tags_from_channel"] is False
    staged.unlink()
    fetch.status(argparse.Namespace(json=True))
    assert json.loads(capsys.readouterr().out)["items"][0]["staged"] == {"exists": False}
