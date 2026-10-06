"""musicdb versions: plays of a name shared by several files go where a decision says, and the state is clean
only when nothing is undecided."""
import argparse, json

from rormpc_tools import doctor, musicdb, versions

LIVE = "Mix/Adagio For Strings (Live).mp3"
ORIG = "Mix/Adagio For Strings.mp3"
URI = "spotify:track:adagio"
SONGS = [{"file": LIVE, "artist": "Tiësto", "title": "Adagio For Strings (Live)", "duration": "218"},
         {"file": ORIG, "artist": "Tiësto", "title": "Adagio For Strings", "duration": "443"}]


def spotify(ts, uri=URI, ms=440000):
    return ("spotify", ts, None, None, uri, "Tiësto", "Adagio For Strings", ms, None)


def plays():
    p, *_ = musicdb.counted(musicdb.db(), musicdb.library())
    return dict(p)


def run(*argv):
    versions.main(list(argv))


def test_undecided_plays_go_nowhere_and_are_open(env):
    env(SONGS)
    musicdb.add_events([spotify("2015-01-01T10:00:00"), spotify("2015-01-02T10:00:00")])
    assert plays() == {}
    g, = versions.groups()
    assert [(t["track"], t["plays"], t["decision"]) for t in g["tracks"]] == [(URI, 2, None)]
    assert g["tracks"][0]["suggest"]["file"] == ORIG  # longest play 440 s, no "live" in the Spotify title
    assert sum(x["plays"] for x in doctor.check()["ambiguous-names"]) == 2


def test_a_decision_credits_past_and_future_plays_of_that_track(env):
    env(SONGS)
    musicdb.add_events([spotify("2015-01-01T10:00:00")])
    run("set", "spotify", URI, ORIG)
    musicdb.add_events([spotify("2015-01-03T10:00:00")])
    assert plays() == {ORIG: 2}
    run("label", ORIG, "original"); run("label", LIVE, "live")
    out = doctor.check()
    assert out["ambiguous-names"] == [] and out["versions-open"] == []


def test_not_owned_is_decided_but_credits_nothing(env):
    env(SONGS)
    musicdb.add_events([spotify("2015-01-01T10:00:00", uri="spotify:track:radio-edit")])
    run("none", "spotify", "spotify:track:radio-edit")
    assert plays() == {}
    assert doctor.check()["ambiguous-names"] == []


def test_a_decision_comes_back_when_its_file_is_gone_or_the_group_changes(env):
    m = env(SONGS)
    musicdb.add_events([spotify("2015-01-01T10:00:00")])
    run("set", "spotify", URI, ORIG)
    m.songs.append({"file": "New/Adagio For Strings (Extended).mp3", "artist": "Tiësto",
                    "title": "Adagio For Strings (Extended)", "duration": "600"})  # a download
    g, = versions.groups()
    assert g["tracks"][0]["decision"]["group_changed"] and f"track spotify {URI}" in g["pending"]
    m.songs = [s for s in m.songs if s["file"] != ORIG]
    g, = versions.groups()
    assert g["tracks"][0]["decision"]["stale"]
    assert plays() == {}  # a stale decision credits nothing rather than the wrong file


def test_same_recording_merges_into_the_kept_file(env, tmp_path, monkeypatch):
    from rormpc_tools import dedupe
    music = tmp_path / "music"
    for f in (LIVE, ORIG):
        (music / f).parent.mkdir(parents=True, exist_ok=True)
        (music / f).write_bytes(b"")
    m = env(SONGS)
    monkeypatch.setattr(musicdb, "MUSIC", music)
    monkeypatch.setattr(dedupe, "QUARANTINE", tmp_path / "q")
    monkeypatch.setattr(dedupe, "mpd_update", lambda: m.songs.remove(SONGS[0]) if SONGS[0] in m.songs else None)
    monkeypatch.setattr(dedupe, "tag_map", lambda p: (None, {}))
    monkeypatch.setattr(musicdb, "push_feedback", lambda c, s: 0)
    musicdb.add_events([("local", "2026-09-26T10:00:00", None, None, None, None, None, None, json.dumps({"file": LIVE})),
                        spotify("2015-01-01T10:00:00")])
    versions.same(argparse.Namespace(keep=ORIG, others=[LIVE]))
    assert not (music / LIVE).exists() and (tmp_path / "q").exists()
    assert plays() == {ORIG: 2}  # the scrobbler's listen through the alias, the Spotify play by the now unique name
    assert versions.groups() == []


def test_shared_ok_silences_a_reviewed_shared_id(env):
    env([{**SONGS[1], "musicbrainz_trackid": "mb-1"}, {**SONGS[1], "file": "Video/Adagio.mp3", "musicbrainz_trackid": "mb-1"}])
    assert [d["id"] for d in doctor.check()["shared-ids"]] == ["mb:mb-1"]
    run("shared-ok", "mb:mb-1")
    assert doctor.check()["shared-ids"] == []


def test_markers_read_file_names_with_underscores():
    assert versions.markers("Tiësto_-_Adagio_For_Strings_(Live)") == ["live"]
    assert versions.markers("Deorro_-_Five_Hours_(Original_Mix)") == []


def test_update_leaves_a_doctor_summary_for_rormpc(env, monkeypatch, tmp_path):
    env(SONGS)
    monkeypatch.setattr(doctor, "SUMMARY", tmp_path / "doctor.json")
    monkeypatch.setattr(doctor, "identity_problems", lambda files: [])  # no audio files here; see test_identity
    musicdb.add_events([spotify("2015-01-01T10:00:00")])
    assert doctor.write_summary().startswith("doctor: ambiguous-names 1")
    s = json.loads((tmp_path / "doctor.json").read_text())
    assert s["clean"] is False and s["counts"]["versions-open"] == 1
    run("set", "spotify", URI, ORIG); run("label", ORIG, "original"); run("label", LIVE, "live")
    assert doctor.write_summary() == "doctor: clean"


def test_history_of_deleted_songs_is_not_stale_and_fix_cleans_their_playlist_lines(env):
    env(SONGS)
    gone = "Old/Bugi, Bugi.mp3"
    musicdb.write_jsonl(musicdb.DONE, [{"file": gone}])
    c = musicdb.db(); c.execute("INSERT INTO skips VALUES (?,?,?,?,?,?)", ("2026-09-27T10:00:00", gone, "", 1, 2, 1)); c.commit()
    musicdb.PLAYLISTS.mkdir()
    (musicdb.PLAYLISTS / "MacBook 2010.m3u").write_text(f"{ORIG}\n{gone}\n")
    assert [s["where"] for s in doctor.check()["stale-paths"]] == ["playlist MacBook 2010"]
    assert doctor.fix() == 1
    assert (musicdb.PLAYLISTS / "MacBook 2010.m3u").read_text() == f"{ORIG}\n"
    assert doctor.check()["stale-paths"] == []


def test_no_suggestion_when_lengths_and_markers_disagree():
    rows = [{"file": LIVE, "duration_s": 218, "markers": ["live"], "version": None},
            {"file": ORIG, "duration_s": 443, "markers": [], "version": None}]
    t = {"title": "Adagio For Strings", "album": "A State Of Trance - 15 Years", "plays": 26, "longest_s": 207}
    assert versions.suggest(t, rows) is None  # unmarked title says original, 26 plays <= 3:27 say not 7:23
    t = {**t, "longest_s": 440}
    assert versions.suggest(t, rows)["file"] == ORIG
    t = {**t, "title": "Adagio For Strings (Live)", "longest_s": 216}
    assert versions.suggest(t, rows)["file"] == LIVE
