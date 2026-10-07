"""`musicdb update` with ListenBrainz broken: an invalid token (KeyError 'user_name' on 2026-10-03) or an outage
(an uncaught SystemExit stopped the hourly sync) must still count the local log, write stickers and export.
Only urllib.request.urlopen is faked: lb_user, http's retries and update's error handling run for real."""
import io, json, subprocess, urllib.error, urllib.request

import pytest

from rormpc_tools import doctor, identity, mbtag, musicdb

YT = "dQw4w9WgXcQ"
RICK = f"yt/001--Rick_Astley--{YT}--20091025.mp3"
SONGS = [{"file": RICK, "artist": "Rick Astley", "title": "Never Gonna Give You Up", "duration": "213",
          "musicbrainz_trackid": "mb-rick"}]


def invalid_token(req, timeout=None):
    assert req.full_url.endswith("/validate-token"), req.full_url  # nothing past the token check
    return io.BytesIO(json.dumps({"code": 200, "message": "Invalid access token.", "valid": False}).encode())


def outage(req, timeout=None):
    raise urllib.error.URLError("[Errno 8] nodename nor servname provided, or not known")


@pytest.mark.parametrize("urlopen, why", [(invalid_token, "token is not valid"), (outage, "nodename")])
def test_update_syncs_and_exports_when_listenbrainz_fails(env, monkeypatch, tmp_path, urlopen, why):
    m = env(SONGS)
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: calls.append(req.full_url) or urlopen(req))
    monkeypatch.setattr(mbtag.time, "sleep", lambda s: None)  # http's designed backoff between retries
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "test-token")
    monkeypatch.setattr(mbtag.settings, "LB_USER", None)  # the user comes from the token, as by default
    monkeypatch.setattr(musicdb, "LB_BACKOFF", tmp_path / "lb-backoff.json")
    monkeypatch.setattr(doctor, "SUMMARY", tmp_path / "doctor.json")
    monkeypatch.setattr(musicdb, "hits_background", lambda: None)  # MusicBrainz: not what this tests
    monkeypatch.setattr(musicdb, "versions_fingerprints", lambda: None)
    monkeypatch.setattr(identity, "sync", lambda *a, **k: {"new": [], "renamed": [], "gone": [], "tagged": [], "conflicts": []})
    subprocess.run(["git", "init", "-q", str(musicdb.DATA)], check=True)
    musicdb.LISTENS_LOG.write_text(json.dumps({"ts": musicdb.epoch_of("2026-09-26T10:00:00"), "file": RICK,
                                               "mbid": "mb-rick"}) + "\n")
    with pytest.raises(SystemExit) as e:
        musicdb.update(None)
    msg = str(e.value)
    assert [p.split(":")[0] for p in msg.removeprefix("failed: ").split("; ")] == ["import_lb", "lb_playlists"]
    assert why in msg and "KeyError" not in msg
    assert calls and all("api.listenbrainz.org" in u for u in calls)
    assert m.stickers[RICK]["playCount"] == "1"
    events = [json.loads(l) for l in (musicdb.DATA / "events.jsonl").read_text().splitlines()]
    assert [(r["source"], r["ts"]) for r in events] == [("local", "2026-09-26T10:00:00")]
    assert json.loads((tmp_path / "lb-backoff.json").read_text())["failures"] == 1
