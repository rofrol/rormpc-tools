"""liveplaylist's Omarchy Radio source, offline: playlist.json is a recorded copy (tests/fixtures), HTTP is a fake
that serves it and small MP3s, the music dir, MPD playlist dir, data dir and cache are temporary."""
import hashlib, json, pathlib, types

import mutagen
import pytest

from rormpc_tools import dedupe, hits_sets, identity, liveplaylist as lp, musicdb

FIXTURE = json.loads((pathlib.Path(__file__).parent / "fixtures" / "omarchy-playlist.json").read_text())
FILES = [t["file"] for t in FIXTURE["tracks"]]
SID = "omarchy-radio"
DIR = "LivePlaylists/Omarchy_Radio--omarchy-radio"
TRACKS = "https://radio.omarchy.org/tracks/"
FRAME = b"\xff\xfb\x90\x64"  # MPEG-1 layer III, 128 kbps, 44.1 kHz


def mp3(seed):
    """A tiny MP3 whose audio differs per seed."""
    return b"".join(FRAME + bytes([seed % 256]) * 413 for _ in range(20))


def fake_audio_hash(path):
    """dedupe.audio_hash without ffmpeg: the audio frames, tags left out."""
    data = pathlib.Path(path).read_bytes()
    return hashlib.md5(data[data.index(FRAME):]).hexdigest() if FRAME in data else None


@pytest.fixture
def radio(tmp_path, monkeypatch, env):
    music, playlists = tmp_path / "music", tmp_path / "playlists"
    music.mkdir()
    for name, value in {"MUSIC": music, "PLAYLISTS": playlists, "DATA": tmp_path / "music-data" / "liveplaylists",
                        "CACHE": tmp_path / "cache", "PAUSE": (0, 0)}.items():
        monkeypatch.setattr(lp, name, value)
    monkeypatch.setattr(lp, "mpd_update", lambda rel: None)
    monkeypatch.setattr(musicdb, "LOCK", tmp_path / "deletions.lock")
    monkeypatch.setattr(dedupe, "audio_hash", fake_audio_hash)
    ns = types.SimpleNamespace(music=music, playlists=playlists, tracks=json.loads(json.dumps(FIXTURE["tracks"])),
                               etag='"v1"', requests=[], bodies={})

    def fake_get(url, headers=None):
        ns.requests.append((url, dict(headers or {})))
        if url == lp.OMARCHY_LIST:
            if (headers or {}).get("If-None-Match") == ns.etag:
                return 304, {"ETag": ns.etag}, b""
            body = json.dumps({"station": "omarchy", "name": "Omarchy", "tracks": ns.tracks}).encode()
            return 200, {"ETag": ns.etag}, body
        if url in ns.bodies:
            return 200, {}, ns.bodies[url]
        if url.startswith(TRACKS):
            return 200, {}, mp3(FILES.index(url.removeprefix(TRACKS)) if url.removeprefix(TRACKS) in FILES else 99)
        raise RuntimeError(f"{url}: HTTP Error 404: Not Found")

    monkeypatch.setattr(lp, "http_get", fake_get)
    ns.downloads = lambda: [u.removeprefix(TRACKS) for u, _ in ns.requests if u != lp.OMARCHY_LIST]
    return ns


def run(*argv):
    with pytest.raises(SystemExit) as e:
        lp.main(list(argv) + ["--json"])
    return e.value.code


def call(capsys, *argv):
    code = run(*argv)
    return code, json.loads(capsys.readouterr().out)


def sub():
    return lp.load(SID)


def m3u(ns):
    f = ns.playlists / "radio.omarchy.org.m3u"
    return f.read_text().splitlines() if f.exists() else None


def deletion(file, **fields):
    rec = {"id": f"20261010-120000-{file.rsplit('/', 1)[-1]}", "file": file, "mode": "permanent",
           "queued_at": "2026-10-10T12:00:00", "events": [], "ops": {"local": "done"}, **fields}
    musicdb.write_jsonl(musicdb.DONE, [rec])
    return rec


def registry(*rows):
    musicdb.write_jsonl(identity.registry_path(), [{"id": f"id{i}", "paths": [r["path"] or r["old"]], **r}
                                                   for i, r in enumerate(rows)])
    identity._cache.clear()


def test_source_of_takes_any_radio_omarchy_url():
    for url in ("https://radio.omarchy.org/", "https://radio.omarchy.org/playlist/still-licensed",
                "http://RADIO.omarchy.org/tracks/"):
        assert lp.source_of(url) == ("omarchy", SID, lp.OMARCHY_LIST)
    assert lp.source_of("https://www.youtube.com/playlist?list=PLtest0000000000000001")[:2] == (
        "youtube", "yt-PLtest0000000000000001")
    with pytest.raises(ValueError, match="Omarchy Radio"):
        lp.source_of("https://omarchy.org/")


def test_add_lists_the_station_as_pending_and_downloads_nothing(radio, capsys):
    code, out = call(capsys, "add", "https://radio.omarchy.org/")
    assert code == 0 and out["added"] and out["check"]["new"] == FILES
    s = sub()
    assert s["kind"] == "omarchy" and s["title"] == "Omarchy Radio" and s["playlist"] == "radio.omarchy.org"
    assert s["dir"] == DIR and s["etag"] == '"v1"'
    assert {it["decision"] for it in s["items"].values()} == {"pending"}
    assert s["items"][FILES[3]]["explicit"] and s["items"][FILES[1]]["artist"] == "Michel Krapf"
    assert m3u(radio) == [] and radio.downloads() == []
    _, out = call(capsys, "list")
    [listed] = out["subscriptions"]
    assert "etag" not in listed
    assert [it["key"] for it in listed["items"]] == FILES
    assert listed["items"][0]["url"] == TRACKS + FILES[0] and listed["items"][0]["ytid"] is None


def test_accept_all_downloads_with_the_stations_names_in_its_order(radio, capsys):
    call(capsys, "add", "https://radio.omarchy.org/")
    radio.tracks = [radio.tracks[i] for i in (2, 0, 1, 3)]
    radio.etag = '"v2"'
    call(capsys, "check")
    code, out = call(capsys, "accept", SID, "--all")
    assert code == 0 and out["download"]["done"] == 4
    order = [FILES[i] for i in (2, 0, 1, 3)]
    assert radio.downloads() == order and m3u(radio) == [f"{DIR}/{f}" for f in order]
    it = sub()["items"][FILES[1]]
    assert it["job"] == "ready" and it["source"] == "download" and it["match"] == "publisher metadata"
    tags = mutagen.File(radio.music / it["path"])
    assert str(tags["TPE1"]) == "Michel Krapf" and str(tags["TIT2"]) == "Still Licensed"
    assert str(tags["TALB"]) == "Omarchy Radio" and str(tags["TXXX:rormpc Source"]) == TRACKS + FILES[1]
    assert not any((lp.CACHE / "staging" / SID).iterdir())


def test_an_unchanged_playlist_is_one_conditional_request(radio, capsys):
    call(capsys, "add", "https://radio.omarchy.org/")
    radio.requests.clear()
    code, out = call(capsys, "check")
    assert code == 0 and out["checks"][0]["ok"] and out["checks"][0]["new"] == []
    assert radio.requests == [(lp.OMARCHY_LIST, {"If-None-Match": '"v1"'})]
    assert all(it["active"] for it in sub()["items"].values())


@pytest.mark.parametrize("bad", [{"title": "Slug", "artist": "X", "file": "Artist - Title.mp3"},
                                 {"title": "Plain http", "artist": "X", "url": "http://example.com/a.mp3"},
                                 {"title": "No artist", "file": "no-artist.mp3"},
                                 None])  # None: a duplicate of the first track
def test_an_invalid_entry_is_skipped_and_nothing_is_marked_gone(radio, capsys, bad):
    call(capsys, "add", "https://radio.omarchy.org/")
    radio.tracks = radio.tracks[:2] + [bad or radio.tracks[0]]
    radio.etag = '"v2"'
    code, out = call(capsys, "check")
    c = out["checks"][0]
    assert code == 0 and c["partial"] and c["gone"] == [] and c["new"] == []
    assert all(it["active"] for it in sub()["items"].values()) and "etag" not in sub()


def test_removed_upstream_leaves_the_playlist_but_keeps_the_file(radio, capsys):
    call(capsys, "add", "https://radio.omarchy.org/")
    call(capsys, "accept", SID, "--all")
    radio.tracks = radio.tracks[1:]
    radio.etag = '"v2"'
    _, out = call(capsys, "check")
    assert out["checks"][0]["gone"] == [FILES[0]]
    assert m3u(radio) == [f"{DIR}/{f}" for f in FILES[1:]] and (radio.music / DIR / FILES[0]).exists()


def test_a_failed_check_changes_nothing(radio, capsys, monkeypatch):
    call(capsys, "add", "https://radio.omarchy.org/")

    def down(url, headers=None):
        raise RuntimeError(f"{url}: HTTP Error 503: Service Unavailable")
    monkeypatch.setattr(lp, "http_get", down)
    code, out = call(capsys, "check")
    assert code == 1 and "503" in out["checks"][0]["error"]
    assert all(it["active"] for it in sub()["items"].values()) and sub()["etag"] == '"v1"'


def test_a_deleted_track_is_blocked_by_its_path_before_any_download(radio, capsys):
    rec = deletion(f"{DIR}/{FILES[0]}")
    call(capsys, "add", "https://radio.omarchy.org/")
    assert sub()["items"][FILES[0]]["deleted"]["id"] == rec["id"]  # shown in the review
    call(capsys, "accept", SID, FILES[0], FILES[1])
    it = sub()["items"][FILES[0]]
    assert it["job"] == "blocked" and "musicdb deletions allow" in it["error"]
    assert radio.downloads() == [FILES[1]] and m3u(radio) == [f"{DIR}/{FILES[1]}"]


def test_a_deleted_track_is_blocked_by_its_audio_under_another_name(radio, capsys):
    registry({"path": None, "old": "Elsewhere/renamed.mp3", "state": "gone", "md5": fake_audio_hash_of(1)})
    deletion("Elsewhere/renamed.mp3")
    call(capsys, "add", "https://radio.omarchy.org/")
    call(capsys, "accept", SID, FILES[1])
    it = sub()["items"][FILES[1]]
    assert it["job"] == "blocked" and not (radio.music / DIR / FILES[1]).exists()
    assert not (lp.CACHE / "staging" / SID / FILES[1]).exists() and m3u(radio) == []


def test_the_same_audio_in_the_library_is_referenced_not_copied(radio, capsys):
    registry({"path": "Mine/still-licensed.mp3", "state": "live", "md5": fake_audio_hash_of(1)})
    call(capsys, "add", "https://radio.omarchy.org/")
    call(capsys, "accept", SID, FILES[1])
    it = sub()["items"][FILES[1]]
    assert it["path"] == "Mine/still-licensed.mp3" and it["source"] == "library" and it["match"] == "same audio"
    assert not (radio.music / DIR).exists() and m3u(radio) == ["Mine/still-licensed.mp3"]


def test_an_answer_that_is_not_an_mp3_fails_and_stays_out(radio, capsys):
    radio.bodies[TRACKS + FILES[0]] = b"<!DOCTYPE html><title>404</title>"
    call(capsys, "add", "https://radio.omarchy.org/")
    call(capsys, "accept", SID, FILES[0])
    it = sub()["items"][FILES[0]]
    assert it["job"] == "failed" and "not an MP3" in it["error"]
    assert not any((lp.CACHE / "staging" / SID).iterdir()) and m3u(radio) == []


def test_a_track_hosted_elsewhere_is_keyed_by_its_url(radio, capsys):
    url = "https://cdn.example.org/songs/Some%20Song.mp3"
    radio.tracks = [{"title": "Some Song", "artist": "Someone", "url": url, "file": "ignored.mp3",
                     "album": "An Album"}]
    radio.bodies[url] = mp3(7)
    call(capsys, "add", "https://radio.omarchy.org/")
    call(capsys, "accept", SID, url)
    it = sub()["items"][url]
    assert it["job"] == "ready" and it["path"] == f"{DIR}/Some_Song--{hashlib.sha1(url.encode()).hexdigest()[:8]}.mp3"
    assert str(mutagen.File(radio.music / it["path"])["TALB"]) == "An Album"


def test_hits_sets_see_the_radio_subscription(radio, capsys):
    call(capsys, "add", "https://radio.omarchy.org/")
    call(capsys, "accept", SID, FILES[2])
    subs = hits_sets.live_subs()
    assert list(subs) == [SID] and hits_sets.live_files(subs[SID]) == [f"{DIR}/{FILES[2]}"]


def fake_audio_hash_of(seed):
    return hashlib.md5(mp3(seed)).hexdigest()
