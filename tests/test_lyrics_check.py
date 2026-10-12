"""Timing flags of synced lyrics (lyrics timed for another cut): the reasons and their boundaries, the record that
fetch/use write, `status --json` and `check`. Offline: fake MPD, fake LRCLIB, temporary lyrics dir."""
import json

import pytest

from rormpc_tools import lyrics, lyrics_check as lc


def lrc(*stamps, text="la la"):
    """An .lrc body with one sung line per stamp (seconds) and an empty closing line after the last."""
    body = [f"[{int(t // 60):02d}:{t % 60:05.2f}] {text}" for t in stamps]
    return "\n".join(body) + "\n"


def reasons(text, length, others=()):
    return [x["reason"] for x in lc.reasons(text, length, others)]


@pytest.mark.parametrize("length, ok_last, early_last", [
    (100, 75.0, 74.9),    # 20% of 100 s is 20 s, below the 25 s floor
    (600, 510.0, 509.9),  # 20% of 10 min is 120 s, above the 90 s cap
    (300, 240.0, 239.9),  # 20% of 5 min: 60 s
])
def test_ends_early_boundaries(length, ok_last, early_last):
    assert reasons(lrc(10, ok_last), length) == []
    [rec] = lc.reasons(lrc(10, early_last), length)
    assert rec["reason"] == "ends-early" and rec["label"] == "lyrics end early"
    assert rec["limit"] == min(max(0.2 * length, 25), 90) and rec["tail"] == round(length - early_last, 1)


def test_normal_entry_with_long_outro_is_not_flagged():
    assert reasons(lrc(12, 60, 120, 200) + "[03:25.00] \n", 240) == []  # 40 s outro on a 4 min song


def test_empty_lines_do_not_count_as_the_last_line():
    assert reasons(lrc(10, 100) + "[03:50.00] \n", 240) == ["ends-early"]


def test_past_end_boundary():
    assert reasons(lrc(10, 180, 202.0), 200) == []  # within the 2 s slack
    [rec] = lc.reasons(lrc(10, 180, 202.1), 200)
    assert rec == {"reason": "past-end", "line": 202.1, "length": 200, "over": 2.1, "lines": 1,
                   "label": "lyrics run past the end"}


def test_past_end_ignores_an_empty_line():
    assert reasons(lrc(10, 180) + "[03:30.00]\n", 200) == []


def test_unordered():
    [rec] = lc.reasons(lrc(10, 50, 40, 190), 200)
    assert (rec["reason"], rec["count"], rec["at"], rec["then"]) == ("unordered", 1, 50, 40)


def test_offset_applied():
    # 200 s song, limit 40 s: the last line at 163 s leaves 37 s
    assert reasons(lrc(10, 163), 200) == []
    assert reasons("[offset:+3000]\n" + lrc(10, 163), 200) == []  # positive: lyrics earlier, 160 s leaves 40 s
    assert reasons("[offset:+3100]\n" + lrc(10, 163), 200) == ["ends-early"]  # 159.9 s leaves 40.1 s
    # negative offset: lyrics later, past the end
    assert reasons("[offset:-5000]\n" + lrc(10, 198), 200) == ["past-end"]


def test_multi_timestamp_lines():
    text = ("[00:10.00][01:20.00][02:50.00]chorus\n"
            "[00:20.00]verse one\n"
            "[01:30.00]verse two\n")
    assert reasons(text, 200) == []  # compressed LRC is in order; its 02:50 repeat is the last line
    assert lc.last_line(text) == 170
    assert reasons("[00:10.00][00:05.00]chorus\n[00:20.00]x\n[02:50.00]y\n", 200) == ["unordered"]
    assert reasons("[00:10.00][03:30.00]chorus\n[00:20.00]x\n[02:50.00]y\n", 200) == ["past-end"]


def test_header_tags_are_not_lines():
    text = "[ar:A]\n[ti:T]\n[re:lrclib.net #1]\n[length:03:20]\n" + lrc(10, 180)
    assert reasons(text, 200) == []


def test_candidates_are_supporting_evidence_only():
    early = lrc(10, 100)
    assert reasons(lrc(10, 190), 200, [100, 102]) == []  # disagreeing alone never flags
    recs = lc.reasons(early, 200, [190, 193, 150])
    assert [x["reason"] for x in recs] == ["ends-early", "candidates"]
    assert recs[1] == {"reason": "candidates", "supporting": True, "others": 2, "their_last": 191.5,
                            "this_last": 100, "delta": -91.5, "limit": 20, "label": "other entries end elsewhere"}
    assert lc.flagged(recs) == ["ends-early"]
    assert reasons(early, 200, [190]) == ["ends-early"]  # one other candidate is not enough
    assert reasons(early, 200, [190, 196]) == ["ends-early"]  # two that disagree with each other neither
    assert reasons(early, 200, [115, 118]) == ["ends-early"]  # within max(20 s, 10%) of this entry


# --- the commands ---------------------------------------------------------------------------------------------

@pytest.fixture
def lyr(env, tmp_path, monkeypatch):
    d = tmp_path / "lyrics"
    monkeypatch.setattr(lyrics, "LYRICS", d)
    monkeypatch.setattr(lyrics, "INDEX", d / "index.json")
    songs = [{"file": f"a/{n}.mp3", "artist": "A", "title": n, "duration": "200.0"}
             for n in ("early", "fine", "plain", "inst")]
    env(songs)
    cands = {
        "early": [{"id": 1, "duration": 200, "syncedLyrics": lrc(10, 100)},
                  {"id": 2, "duration": 201, "syncedLyrics": lrc(10, 190)},
                  {"id": 3, "duration": 199, "syncedLyrics": lrc(10, 192)},
                  {"id": 4, "duration": 260, "syncedLyrics": lrc(10, 100)}],
        "fine": [{"id": 5, "duration": 200, "syncedLyrics": lrc(10, 190)}],
        "plain": [{"id": 6, "duration": 200, "plainLyrics": "la la\n"}],
        "inst": [{"id": 7, "duration": 200, "instrumental": True}],
    }
    monkeypatch.setattr(lyrics, "search", lambda artist, title: cands[title])
    monkeypatch.setattr(lyrics, "best", lambda cs, duration: cs[0])
    by_id = {c["id"]: c for cs in cands.values() for c in cs}
    monkeypatch.setattr(lyrics.mbtag, "http", lambda url, **kw: by_id.get(int(url.rsplit("/", 1)[1])))
    return d


def test_fetch_records_flags_and_evidence(lyr, capsys):
    lyrics.main(["fetch", "a/early.mp3", "a/fine.mp3", "a/plain.mp3", "a/inst.mp3"])
    index = lyrics.load()
    assert [x["reason"] for x in index["a/early.mp3"]["suspect"]] == ["ends-early", "candidates"]
    assert index["a/early.mp3"]["suspect"][1]["others"] == 2  # entry 4 is another cut: not counted
    assert (lyr / "a/early.lrc").exists()  # flagged, still written
    for n in ("fine", "plain", "inst"):
        assert "suspect" not in index[f"a/{n}.mp3"]
    capsys.readouterr()
    lyrics.main(["status", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["a/early.mp3"]["state"] == "synced" and out["a/early.mp3"]["lrclib_id"] == 1
    assert out["a/early.mp3"]["suspect"][0] == {"reason": "ends-early", "tail": 100, "limit": 40, "last_line": 100,
                                                "length": 200, "label": "lyrics end early"}
    lyrics.main(["status"])
    assert "(1 synced flagged: lyrics end early, check timing)" in capsys.readouterr().out


def test_use_flags_without_evidence(lyr, capsys):
    lyrics.main(["use", "a/fine.mp3", "1"])
    assert lc.flagged(lyrics.load()["a/fine.mp3"]["suspect"]) == ["ends-early"]
    assert [x["reason"] for x in lyrics.load()["a/fine.mp3"]["suspect"]] == ["ends-early"]
    assert "lyrics end early, check timing (ends-early)" in capsys.readouterr().out
    lyrics.main(["use", "a/fine.mp3", "5"])
    assert "suspect" not in lyrics.load()["a/fine.mp3"]


def test_candidates_show_their_flags(lyr, capsys):
    lyrics.main(["candidates", "a/early.mp3"])
    out = {c["id"]: c for c in json.loads(capsys.readouterr().out)["candidates"]}
    assert lc.flagged(out[1]["suspect"]) == ["ends-early"] and out[2]["suspect"] == []


def test_check_recomputes_and_dry_run(lyr, capsys):
    lyrics.main(["fetch", "a/early.mp3", "a/fine.mp3", "a/plain.mp3"])
    (lyr / "a/fine.lrc").write_text("[ar:A]\n[offset:-15000]\n" + lrc(10, 190))  # nudged 15 s later: past the end
    (lyr / "a/early.lrc").write_text(lrc(10, 190))  # fixed by hand
    before = lyrics.INDEX.read_text()
    capsys.readouterr()
    lyrics.main(["check", "--dry-run"])
    out = capsys.readouterr().out
    assert "past-end: a/fine.mp3" in out and "checked 2 synced, 1 flagged, 1 past-end (dry run" in out
    assert lyrics.INDEX.read_text() == before
    lyrics.main(["check"])
    index = lyrics.load()
    assert "suspect" not in index["a/early.mp3"] and lc.flagged(index["a/fine.mp3"]["suspect"]) == ["past-end"]
    assert not lyrics.INDEX.with_suffix(".tmp").exists()
    lyrics.main(["check", "a/plain.mp3"])
    assert "a/plain.mp3: no synced lyrics" in capsys.readouterr().err
