"""liveplaylist, offline: yt-dlp's flat listing and yt-mp3-mb's downloads are fakes, the music dir, MPD playlist
dir, data dir and cache are temporary."""
import json, types

import pytest

from rormpc_tools import identity, liveplaylist as lp, mbtag, musicdb, yt_mp3_mb

LIST = "PLtest0000000000000001"
URL = f"https://www.youtube.com/playlist?list={LIST}"
SID = f"yt-{LIST}"
A, B, C, D = "aaaaaaaaaa1", "bbbbbbbbbb2", "cccccccccc3", "dddddddddd4"


def entry(yid, title=None):
    return {"id": yid, "title": title or f"Artist - Song {yid}", "channel": "Some Channel", "duration": 200}


def listing(*ids, title="Test list", count=None):
    out = {"title": title, "entries": [entry(i) if isinstance(i, str) else i for i in ids]}
    if count is not None:
        out["playlist_count"] = count
    return out


@pytest.fixture
def live(tmp_path, monkeypatch, env):
    """Temp dirs, a settable listing, a fake yt-mp3-mb batch (records calls); returns a namespace."""
    music, playlists = tmp_path / "music", tmp_path / "playlists"
    music.mkdir()
    for name, value in {"MUSIC": music, "PLAYLISTS": playlists, "DATA": tmp_path / "music-data" / "liveplaylists",
                        "CACHE": tmp_path / "cache", "PAUSE": (0, 0)}.items():
        monkeypatch.setattr(lp, name, value)
    monkeypatch.setattr(lp, "mpd_update", lambda rel: None)
    monkeypatch.setattr(mbtag, "mb_url", lambda yid: ns.mb_links.get(yid, []))
    ns = types.SimpleNamespace(music=music, playlists=playlists, listing=listing(A, B, C), downloads=[],
                               uncertain=set(), mb_links={}, registry=[])

    def fake_listing(url):
        if isinstance(ns.listing, Exception):
            raise ns.listing
        return ns.listing

    def fake_batch(urls, dir_arg, extra, known):
        [yid] = known[1]
        ns.downloads.append(yid)
        stage = yt_mp3_mb.pathlib.Path(dir_arg)
        stage.mkdir(parents=True, exist_ok=True)
        f = stage / f"Artist--Song_{yid}--{yid}--20240101.mp3"
        f.write_bytes(b"mp3")
        sure = yid not in ns.uncertain
        report = {"files": [{"path": str(f), "ytid": yid, "status": "auto" if sure else "review", "artist": "Artist",
                             "title": f"Song {yid}", "mbid": "m-" + yid if sure else ""}],
                  "needs_review": [] if sure else [{"path": str(f), "ytid": yid, "proposal": {"mbid": "maybe"}}],
                  "skipped": [], "failed": [], "error": None}
        return report

    monkeypatch.setattr(lp, "listing", fake_listing)
    monkeypatch.setattr(yt_mp3_mb, "batch", fake_batch)

    def registry(*rows):
        musicdb.write_jsonl(identity.registry_path(), [{"id": f"id{i}", "state": "live", "paths": [r["path"]], **r}
                                                       for i, r in enumerate(rows)])
        identity._cache.clear()

    ns.set_registry = registry
    return ns


def run(*argv):
    """liveplaylist ARGV --json; returns the exit status."""
    with pytest.raises(SystemExit) as e:
        lp.main(list(argv) + ["--json"])
    return e.value.code


def call(capsys, *argv):
    code = run(*argv)
    return code, json.loads(capsys.readouterr().out)


def sub():
    return lp.load(SID)


def m3u(ns):
    f = ns.playlists / "Test list.m3u"
    return f.read_text().splitlines() if f.exists() else None


def test_list_id_accepts_youtube_playlists_only():
    assert lp.list_id(URL) == LIST
    assert lp.list_id(f"https://music.youtube.com/playlist?list={LIST}") == LIST
    assert lp.list_id(f"https://www.youtube.com/watch?v={A}&list={LIST}&index=2") == LIST
    for bad in ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", f"https://www.youtube.com/watch?v={A}",
                f"https://www.youtube.com/watch?v={A}&list=RD{A}", f"https://evil.example/?list={LIST}"):
        with pytest.raises(ValueError):
            lp.list_id(bad)


def test_add_lists_everything_as_pending_and_downloads_nothing(live, capsys):
    code, out = call(capsys, "add", URL)
    assert code == 0 and out["added"] and out["check"]["new"] == [A, B, C]
    s = sub()
    assert {it["decision"] for it in s["items"].values()} == {"pending"}
    assert {it["job"] for it in s["items"].values()} == {None}
    assert s["playlist"] == "Test list" and s["dir"] == f"LivePlaylists/Test_list--{LIST}"
    assert m3u(live) == [] and live.downloads == []
    code, out = call(capsys, "add", URL)  # twice: the same subscription, checked
    assert not out["added"] and out["check"]["new"] == []


def test_add_picks_a_free_playlist_name(live, capsys):
    live.playlists.mkdir()
    (live.playlists / "Test list.m3u").write_text("someone/else.mp3\n")
    call(capsys, "add", URL)
    assert sub()["playlist"] == "Test list (2)"
    assert (live.playlists / "Test list.m3u").read_text() == "someone/else.mp3\n"


def test_add_of_an_unlistable_playlist_subscribes_nothing(live, capsys):
    live.listing = RuntimeError("ERROR: [youtube:tab] The playlist does not exist")
    code, out = call(capsys, "add", URL)
    assert code == 1 and "does not exist" in out["error"] and lp.all_ids() == []


def test_accept_all_downloads_and_publishes_in_playlist_order(live, capsys):
    call(capsys, "add", URL)
    live.listing = listing(C, A, B)
    call(capsys, "check")
    code, out = call(capsys, "accept", SID, "--all")
    assert code == 0 and out["queued"] == [C, A, B] and out["download"]["done"] == 3
    assert live.downloads == [C, A, B]
    rel = f"LivePlaylists/Test_list--{LIST}"
    assert m3u(live) == [f"{rel}/Artist--Song_{y}--{y}--20240101.mp3" for y in (C, A, B)]
    assert all((live.music / p).exists() for p in m3u(live))
    assert {it["job"] for it in sub()["items"].values()} == {"ready"}
    status = json.loads((lp.CACHE / "status.json").read_text())
    assert not status["running"] and status["done"] == 3


def test_uncertain_match_waits_outside_the_library_until_accepted(live, capsys):
    live.uncertain = {B}
    call(capsys, "add", URL)
    call(capsys, "accept", SID, "--all")
    it = sub()["items"][B]
    assert it["job"] == "needs_match" and it["review"]["mbid"] == "maybe"
    assert not str(it["path"]).startswith(str(live.music)) and len(m3u(live)) == 2
    code, out = call(capsys, "accept", SID, B)
    assert out["ready"] == [B] and live.downloads.count(B) == 1
    assert len(m3u(live)) == 3 and (live.music / sub()["items"][B]["path"]).exists()


def test_library_songs_are_referenced_only_on_a_confirmed_match(live, capsys):
    live.set_registry({"path": "old/same-video.mp3", "ytid": A},
                      {"path": "old/recording.mp3", "ytid": "zzzzzzzzzz9", "mbid": "rec-b"},
                      {"path": "old/same-title.mp3", "ytid": "yyyyyyyyyy8", "mbid": "rec-x"})
    live.mb_links = {B: [{"recording": "rec-b"}], C: [{"recording": "rec-x"}, {"recording": "rec-y"}]}
    call(capsys, "add", URL)
    call(capsys, "accept", SID, "--all")
    items = sub()["items"]
    assert items[A]["path"] == "old/same-video.mp3" and items[A]["source"] == "library"
    assert items[B]["path"] == "old/recording.mp3" and "rec-b" in items[B]["match"]
    # two recordings linked to the video: not confirmed, downloaded
    assert items[C]["source"] == "download" and live.downloads == [C]


def test_reject_is_durable_and_never_downloaded(live, capsys):
    call(capsys, "add", URL)
    call(capsys, "reject", SID, B)
    call(capsys, "accept", SID, "--all")
    assert live.downloads == [A, C]
    live.listing = listing(A, C)  # B leaves upstream ...
    call(capsys, "check")
    live.listing = listing(A, B, C)  # ... and comes back: active again, still rejected
    _, out = call(capsys, "check")
    assert out["checks"][0]["back"] == [B]
    it = sub()["items"][B]
    assert it["active"] and it["decision"] == "rejected" and it["job"] is None
    call(capsys, "accept", SID, "--all")
    assert live.downloads == [A, C]


def test_removed_upstream_leaves_the_playlist_but_keeps_the_file(live, capsys):
    call(capsys, "add", URL)
    call(capsys, "accept", SID, "--all")
    gone = live.music / sub()["items"][B]["path"]
    live.listing = listing(A, C)
    _, out = call(capsys, "check")
    assert out["checks"][0]["gone"] == [B]
    assert len(m3u(live)) == 2 and gone.exists()
    live.listing = listing(B, A, C)
    call(capsys, "check")
    assert m3u(live)[0] == str(gone.relative_to(live.music))


@pytest.mark.parametrize("bad", [RuntimeError("HTTP Error 403: Forbidden"), listing(A, count=3),
                                 listing(A, None), listing(A, entry(B, "[Deleted video]"), C),
                                 listing()])
def test_failed_or_partial_check_never_removes(live, capsys, bad):
    call(capsys, "add", URL)
    live.listing = bad
    code, out = call(capsys, "check")
    assert {it["ytid"] for it in sub()["items"].values() if it["active"]} == {A, B, C}
    if isinstance(bad, Exception):
        assert code == 1 and sub()["last_check"]["error"] == "HTTP Error 403: Forbidden"


def test_unavailable_new_entries_are_not_added(live, capsys):
    live.listing = listing(A, entry(D, "[Private video]"))
    call(capsys, "add", URL)
    assert set(sub()["items"]) == {A}


def test_interrupted_download_resumes_without_a_second_download(live, capsys):
    call(capsys, "add", URL)
    call(capsys, "accept", SID, "--all", "--no-download")
    with lp.locked():  # a worker died: one item still downloading, its file already moved in
        s = sub()
        s["items"][A]["job"] = "downloading"
        lp.save(s)
    target = live.music / s["dir"]
    target.mkdir(parents=True)
    (target / f"Artist--Song--{A}--20240101.mp3").write_bytes(b"mp3")
    code, out = call(capsys, "download")
    assert code == 0 and out["done"] == 3 and live.downloads == [B, C]
    assert sub()["items"][A]["path"] == f"{s['dir']}/Artist--Song--{A}--20240101.mp3"


def test_failed_download_keeps_the_decision_and_retries_on_accept(live, capsys, monkeypatch):
    call(capsys, "add", URL)
    ok = yt_mp3_mb.batch
    monkeypatch.setattr(yt_mp3_mb, "batch", lambda *a, **k: {"files": [], "needs_review": [], "skipped": [],
                                                              "failed": [], "error": "yt-dlp failed (1)"})
    call(capsys, "accept", SID, A)
    it = sub()["items"][A]
    assert it["decision"] == "accepted" and it["job"] == "failed" and it["error"] == "yt-dlp failed (1)"
    monkeypatch.setattr(yt_mp3_mb, "batch", ok)
    call(capsys, "accept", SID, A)
    assert sub()["items"][A]["job"] == "ready"


def test_a_second_worker_exits(live, capsys):
    call(capsys, "add", URL)
    lp.CACHE.mkdir(parents=True, exist_ok=True)
    held = open(lp.CACHE / "worker.lock", "w")
    lp.fcntl.flock(held, lp.fcntl.LOCK_EX | lp.fcntl.LOCK_NB)
    code, out = call(capsys, "download")
    assert code != 0 and "already running" in out["error"]


def test_list_json_shape(live, capsys):
    call(capsys, "add", URL)
    _, out = call(capsys, "list")
    [s] = out["subscriptions"]
    assert s["id"] == SID and s["counts"]["pending"] == 3 and [it["ytid"] for it in s["items"]] == [A, B, C]
    assert {"decision", "job", "active", "position", "title", "path"} <= set(s["items"][0])
    assert s["items"][0]["key"] == A and s["items"][0]["url"] == f"https://www.youtube.com/watch?v={A}"
    with lp.locked():  # items stored before radio sources have no "key": their video id is the key
        old = sub()
        for it in old["items"].values():
            del it["key"]
        lp.save(old)
    _, out = call(capsys, "list")
    assert [it["key"] for it in out["subscriptions"][0]["items"]] == [A, B, C]
    assert call(capsys, "reject", SID, B)[1]["rejected"] == [B]


def test_cancel_requeues_the_item_in_progress(live, capsys, monkeypatch):
    call(capsys, "add", URL)
    ok = yt_mp3_mb.batch

    def cancelled_at_b(urls, dir_arg, extra, known):
        if B in known[1]:
            raise lp.Cancelled()  # what SIGTERM raises inside the download
        return ok(urls, dir_arg, extra, known)

    monkeypatch.setattr(yt_mp3_mb, "batch", cancelled_at_b)
    code, out = call(capsys, "accept", SID, "--all")
    items = sub()["items"]
    assert code == 1 and out["download"]["state"] == "cancelled"
    assert (items[A]["job"], items[B]["job"], items[C]["job"]) == ("ready", "queued", "queued")
    assert json.loads((lp.CACHE / "status.json").read_text())["state"] == "cancelled"


def test_items_accepted_while_the_worker_runs_are_downloaded_too(live, capsys, monkeypatch):
    call(capsys, "add", URL)
    ok = yt_mp3_mb.batch

    def accept_b_meanwhile(urls, dir_arg, extra, known):
        if A in known[1]:
            assert run("accept", SID, B, "--no-download") == 0
        return ok(urls, dir_arg, extra, known)

    monkeypatch.setattr(yt_mp3_mb, "batch", accept_b_meanwhile)
    assert run("accept", SID, A) == 0  # prints two JSON lines: the inner accept's and its own
    assert live.downloads == [A, B] and sub()["items"][B]["job"] == "ready"
