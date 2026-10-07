"""yt-mp3-mb --batch, offline: a fake yt-dlp download and fake MusicBrainz identification."""
import json, subprocess, sys, types

import pytest

from rormpc_tools import mbtag, yt_mp3_mb

A, B = "aaaaaaaaaa1", "bbbbbbbbbb2"
URL = "https://www.youtube.com/playlist?list=PLtest0000000000000001"


@pytest.fixture
def ytmb(tmp_path, monkeypatch):
    """yt_mp3_mb with a fake yt-dlp download and fake MusicBrainz identification."""
    music = tmp_path / "music"
    monkeypatch.setattr(yt_mp3_mb, "MUSIC", music)
    monkeypatch.setattr(yt_mp3_mb, "LOG", tmp_path / "log.jsonl")
    monkeypatch.setattr(yt_mp3_mb, "replaygain", lambda p: None)
    monkeypatch.setattr(yt_mp3_mb, "mpd_update", lambda: None)
    calls = types.SimpleNamespace(skip=None, statuses={})

    def download(urls, extra, skip_ids=()):
        calls.skip = set(skip_ids)
        infos = []
        for yid in ids_of(urls) - set(skip_ids):
            f = tmp_path / f"{yid}.mp3"
            f.write_bytes(b"mp3")
            infos.append({"filepath": str(f), "id": yid, "title": "X - Y", "channel": "X", "duration": 200,
                          "upload_date": "20240101"})
        return infos

    def resolve(d):
        st = calls.statuses.get(d["ytid"], "auto")
        return {"status": st, "artist": "MB Artist", "title": "MB Title", "mbid": "" if st == "nomatch" else "rec",
                "artist_mbids": [], "score": 0.8, "method": "lb", "alternatives": []}

    monkeypatch.setattr(yt_mp3_mb, "download", download)
    monkeypatch.setattr(mbtag, "collect", lambda path, yid, *a: {"ytid": yid, "provided": None, "channel": "X",
                                                                 "yt_title": "X - Y", "cands": [("YT Artist", "YT Title")]})
    monkeypatch.setattr(mbtag, "resolve", resolve)
    monkeypatch.setattr(mbtag, "write_tags", lambda path, row, album=None: calls.__dict__.setdefault("tags", []).append(row))
    monkeypatch.setattr(mbtag, "cover", lambda *a: None)
    return calls


def ids_of(urls):
    return {u.rsplit("=", 1)[1] for u in urls}


def test_batch_leaves_uncertain_matches_for_review_without_mbid(ytmb, tmp_path):
    ytmb.statuses = {B: "review"}
    r = yt_mp3_mb.batch([f"https://www.youtube.com/watch?v={A}", f"https://www.youtube.com/watch?v={B}"], "Out")
    assert r["failed"] == [], r
    by = {f["ytid"]: f for f in r["files"]}
    assert by[A]["mbid"] == "rec" and by[A]["status"] == "auto"
    assert by[B]["mbid"] == "" and (by[B]["artist"], by[B]["title"]) == ("YT Artist", "YT Title")
    assert [n["ytid"] for n in r["needs_review"]] == [B] and r["needs_review"][0]["proposal"]["mbid"] == "rec"
    assert all(t["mbid"] == "" for t in ytmb.tags if t["artist"] == "YT Artist")


def test_batch_resumes_without_downloading_present_files(ytmb, tmp_path):
    urls = [f"https://www.youtube.com/watch?v={A}", f"https://www.youtube.com/watch?v={B}"]
    yt_mp3_mb.batch(urls[:1], "Out")
    r = yt_mp3_mb.batch(urls, "Out")
    assert ytmb.skip == {A} and r["skipped"] == [A] and [f["ytid"] for f in r["files"]] == [B]
    ytmb.skip = None
    r = yt_mp3_mb.batch(urls, "Out")  # everything there: yt-dlp is not even started
    assert ytmb.skip is None and r["files"] == [] and r["skipped"] == [A, B]


def test_batch_json_cli(ytmb, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["yt-mp3-mb", "--batch", "--json", "-d", "Out",
                                      f"https://www.youtube.com/watch?v={A}"])
    with pytest.raises(SystemExit) as e:
        yt_mp3_mb.main()
    out = json.loads(capsys.readouterr().out)
    assert e.value.code == 0 and out["error"] is None and out["files"][0]["ytid"] == A


def test_flat_listing_reads_ytdlp_json(monkeypatch):
    fake = {"title": "T", "entries": [{"id": A, "title": "X - Y"}]}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, json.dumps(fake), ""))
    assert yt_mp3_mb.flat(URL) == fake
    monkeypatch.setattr(subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(
        cmd, 1, "", "WARNING: x\nERROR: [youtube:tab] Sign in to confirm you're not a bot\n"))
    with pytest.raises(RuntimeError, match="not a bot"):
        yt_mp3_mb.flat(URL)
