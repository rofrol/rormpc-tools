"""musicdb lyrics transcribe-eval with a fake backend: WER, sync, the separating ranges, --from reuse and no writes
to the lyrics dir. No audio is decoded and no model runs."""
import hashlib, json, math, types

import pytest

from rormpc_tools import lyrics, settings, transcribe, transcribe_eval as ev, translation
from test_transcribe import VERSE, result, words_of


def test_wer_counts_substitutions_deletions_and_insertions():
    assert ev.wer("a b c d".split(), "a x c".split()) == 0.5  # b->x, d deleted
    assert ev.wer(["a"], ["a", "b", "c"]) == 2.0
    assert ev.wer(VERSE[0].split(), VERSE[0].split()) == 0
    assert ev.wer([], ["a"]) is None


def test_sync_median_and_share_within_one_second():
    s = ev.sync([10.0, 20.0, 31.5], [10.2, 19.0, 30.0, 60.0])
    assert s["median"] == pytest.approx(1.0) and s["within"] == pytest.approx(2 / 3) and s["lines"] == 3
    assert ev.sync([10.0], []) is None


def test_reference_in_time_order_normalized_with_offset(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics, "LYRICS", tmp_path)
    (tmp_path / "A").mkdir()
    (tmp_path / "A/s.lrc").write_text("[ar:X]\n[offset:+500]\n[00:20.50]Hello, World!\n[00:10.50][00:30.50]Oh, yeah\n"
                                      "[00:40.00]\n")
    assert ev.reference("A/s.mp3") == [(10.0, "oh yeah"), (20.0, "hello world"), (30.0, "oh yeah")]
    assert ev.reference("A/none.mp3") is None


def test_pad_needed_is_where_words_outside_speech_stops(monkeypatch):
    words = [{"start": t, "end": t} for t in (10.0, 11.0, 12.0, 21.0, 23.0)]  # two words 1 and 3 s after the region
    regions = [(9.0, 20.0)]
    pad = ev.pad_needed(words, regions)  # 0.25 of 5 words: one may lie outside, so the nearer one decides
    assert pad == pytest.approx(1.0)
    for p, fires in ((pad, False), (pad - 0.01, True)):
        monkeypatch.setattr(transcribe, "SPEECH_PAD", p)
        assert ("words_outside_speech" in transcribe.withheld_reasons([], words, regions)) is fires


def song(name, label, catches=(), **inputs):
    return {"file": name, "label": label, "inputs": dict.fromkeys(c[0] for c in ev.CHECKS) | inputs,
            "catches": list(catches)}


def test_separating_ranges():
    songs = [song("l1", "lyrics", outside_speech_max=0.1), song("l2", "lyrics", outside_speech_max=0.2),
             song("i1", "instrumental", ["words_outside_speech"], outside_speech_max=0.6),
             song("i2", "instrumental", ["words_outside_speech"], outside_speech_max=0.5),
             # caught by no_vocals anyway: no constraint
             song("i3", "instrumental", ["no_vocals_seconds"], outside_speech_max=0.05)]
    r = ev.separating(songs, "outside_speech_max", "gt", "words_outside_speech")
    assert r["range"] == [0.2, True, 0.5, False] and r["relied_on"] == 2
    assert ev.inside(r["range"], 0.2) and ev.inside(r["range"], 0.49) and not ev.inside(r["range"], 0.5)
    assert ev.interval(r["range"]) == "[0.2, 0.5)"
    blocked = ev.separating(songs + [song("l3", "lyrics", outside_speech_max=0.55)], "outside_speech_max", "gt",
                            "words_outside_speech")
    assert blocked["range"] is None and blocked["lyrics"] == ["l3"] and blocked["instrumentals"] == ["i2"]
    # fires below its input: density_min, lyrics bound it from above
    d = ev.separating([song("l1", "lyrics", density_min=1.0), song("l2", "lyrics", density_min=2.0),
                       song("i1", "instrumental", density_min=0.1)], "density_min", "lt", "low_word_density")
    assert d["range"] == [0.1, False, 1.0, True] and ev.interval(d["range"]) == "(0.1, 1]"
    # an integer threshold that fires at or above: (3, 6] is 4..6
    rp = ev.separating([song("l1", "lyrics", repeat_min=3), song("i1", "instrumental", repeat_min=6)], "repeat_min",
                       "ge", "repeated_line")
    assert rp["range"] == [3, False, 6, True] and not ev.inside(rp["range"], 3) and ev.inside(rp["range"], 6)
    # an instrumental this check cannot catch at all
    none = ev.separating([song("l1", "lyrics", repeat_min=3), song("i1", "instrumental")], "repeat_min", "ge",
                         "repeated_line")
    assert none["range"] is None and none["instrumentals"] == ["i1"] and none["relied_on"] == 1
    # nothing relies on it: only the lyrics bound it
    free = ev.separating([song("l1", "lyrics", density_max=3.0)], "density_max", "gt", "high_word_density")
    assert free["range"] == [3.0, True, math.inf, False] and free["relied_on"] == 0


def lrc_of(segments):
    return "".join(f"[{int(s['start'] // 60):02d}:{s['start'] % 60:05.2f}]{s['text'].strip()}\n" for s in segments)


@pytest.fixture
def ev_env(tmp_path, monkeypatch):
    lyr, music = tmp_path / "lyrics", tmp_path / "music"
    lyr.mkdir(); music.mkdir()
    monkeypatch.setattr(lyrics, "LYRICS", lyr)
    monkeypatch.setattr(lyrics, "INDEX", lyr / "index.json")
    monkeypatch.setattr(translation, "LYRICS", lyr)
    monkeypatch.setattr(settings, "MUSIC_DIR", music)
    monkeypatch.setattr(transcribe, "check_backend", lambda: None)
    monkeypatch.setattr(transcribe, "audio_identity",
                        lambda p: {"pcm_sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "samples": 1, "rate": 16000})
    ns = types.SimpleNamespace(lyrics=lyr, music=music, calls=[], outs={})

    def fake_backend(path, identity, language):
        rel = path.relative_to(music).as_posix()
        ns.calls.append(rel)
        return ns.outs[rel]

    monkeypatch.setattr(transcribe, "run_backend", fake_backend)
    segs = words_of(*VERSE)
    for rel in ("A/sung.mp3", "A/loop.mp3", "B/instrumental.mp3"):
        (music / rel).parent.mkdir(parents=True, exist_ok=True)
        (music / rel).write_bytes(rel.encode())
    (lyr / "A").mkdir()
    (lyr / "A/sung.lrc").write_text(lrc_of(segs))
    (lyr / "A/loop.lrc").write_text(lrc_of(segs))
    lyrics.save({"A/sung.mp3": {"state": "synced"}, "A/loop.mp3": {"state": "synced"}})
    ns.outs = {"A/sung.mp3": result(segments=segs),
               # sung words, but half of them after the VAD's last region
               "A/loop.mp3": result(segments=segs, regions=[(5.0, 20.0)]),
               "B/instrumental.mp3": result(segments=[], regions=[], skipped="no_vocals")}
    ns.list = tmp_path / "eval.tsv"
    ns.list.write_text("# a comment\nA/sung.mp3\tlyrics\n\nA/loop.mp3\tlyrics\nB/instrumental.mp3\tinstrumental\n")
    return ns


def listing(d):
    return sorted((p.relative_to(d).as_posix(), p.read_bytes()) for p in d.rglob("*") if p.is_file())


def run_json(capsys, *argv):
    lyrics.main(["transcribe-eval", *argv, "--json"])
    return json.loads(capsys.readouterr().out)


def test_report_wer_sync_states_and_no_lyrics_writes(ev_env, tmp_path, capsys):
    before = listing(ev_env.lyrics)
    out = tmp_path / "raw"
    rep = run_json(capsys, str(ev_env.list), "--out", str(out))
    assert listing(ev_env.lyrics) == before  # no sidecar, .lrc, .txt or index.json change
    by = {s["file"]: s for s in rep["songs"]}
    sung, loop, inst = by["A/sung.mp3"], by["A/loop.mp3"], by["B/instrumental.mp3"]
    assert sung["state"] == "shown" and sung["ok"] and sung["wer"] == 0
    assert sung["sync"]["median"] == pytest.approx(0, abs=0.006) and sung["sync"]["within"] == 1
    assert sung["measures"]["outside_share"] == 0 and sung["measures"]["speech_seconds"] == 115.0
    assert loop["state"] == "withheld" and loop["reasons"] == ["words_outside_speech"] and not loop["ok"]
    assert loop["measures"]["outside_share"] > transcribe.OUTSIDE_SPEECH_MAX and loop["wer"] == 0
    assert inst["state"] == "no_vocals" and inst["no_vocals"] and inst["ok"]
    assert rep["summary"]["lyrics_shown"] == 1 and rep["summary"]["instrumentals_caught"] == 1
    r = {x["threshold"]: x for x in rep["ranges"]}
    pad = r["speech_pad"]
    assert pad["range"][0] == pytest.approx(loop["measures"]["pad_needed"]) and not pad["today_inside"]
    assert r["outside_speech_max"]["range"][0] == pytest.approx(loop["measures"]["outside_share"])
    # the withheld song's 15 s of speech bound it; the instrumental is caught by the ratio as well, so not from below
    assert r["no_vocals_min_seconds"]["range"] == ["-inf", False, 15.0, True]
    assert r["no_vocals_min_seconds"]["today_inside"] and r["no_vocals_min_seconds"]["relied_on"] == 0
    assert sorted(p.name for p in out.iterdir()) == sorted(ev.raw_path(out, f).name for f in by)


def test_rerun_reuses_raw_results_and_from_needs_no_backend(ev_env, tmp_path, capsys, monkeypatch):
    out = tmp_path / "raw"
    first = run_json(capsys, str(ev_env.list), "--out", str(out))
    assert len(ev_env.calls) == 3
    assert run_json(capsys, str(ev_env.list), "--out", str(out))["songs"] == first["songs"]
    assert len(ev_env.calls) == 3  # same backend inputs: reused

    def no_backend(*a):
        raise AssertionError("the backend ran")

    monkeypatch.setattr(transcribe, "run_backend", no_backend)
    monkeypatch.setattr(transcribe, "check_backend", no_backend)
    monkeypatch.setattr(transcribe, "audio_identity", no_backend)
    again = run_json(capsys, str(ev_env.list), "--from", str(out))
    assert again["songs"] == first["songs"]
    # judging again with another threshold, from the same raw results
    monkeypatch.setattr(transcribe, "OUTSIDE_SPEECH_MAX", 0.9)
    assert {s["file"]: s["state"] for s in run_json(capsys, str(ev_env.list), "--from", str(out))["songs"]}[
        "A/loop.mp3"] == "shown"


def test_changed_backend_inputs_run_again(ev_env, tmp_path, capsys, monkeypatch):
    out = tmp_path / "raw"
    run_json(capsys, str(ev_env.list), "--out", str(out))
    monkeypatch.setattr(transcribe, "VAD_THRESHOLD", 0.4)
    run_json(capsys, str(ev_env.list), "--out", str(out))
    assert len(ev_env.calls) == 6


def test_from_missing_song_fails_after_reporting_the_rest(ev_env, tmp_path, capsys):
    out = tmp_path / "raw"
    run_json(capsys, str(ev_env.list), "--out", str(out))
    ev.raw_path(out, "A/loop.mp3").unlink()
    with pytest.raises(SystemExit) as e:
        lyrics.main(["transcribe-eval", str(ev_env.list), "--from", str(out)])
    assert e.value.code == 1
    text = capsys.readouterr().out
    assert "A/loop.mp3: failed: no saved result" in text and "lyrics: 1/1 shown" in text
    assert "separating ranges" in text and "speech_pad" in text


def test_text_table(ev_env, tmp_path, capsys):
    lyrics.main(["transcribe-eval", str(ev_env.list), "--out", str(tmp_path / "raw")])
    text = capsys.readouterr().out
    assert "A/loop.mp3: lyrics, withheld: words_outside_speech" in text
    assert "lyrics: 1/2 shown" in text and "instrumentals: 1/1 no_vocals or withheld (1 no_vocals)" in text
    assert "outside_speech_max" in text and "(today outside)" in text


def test_bad_list_line(tmp_path):
    p = tmp_path / "eval.tsv"
    p.write_text("A/song.mp3 lyrics\n")
    with pytest.raises(SystemExit) as e:
        ev.read_list(p)
    assert ":1:" in str(e.value)
    p.write_text("A/song.mp3\tsung\n")
    with pytest.raises(SystemExit):
        ev.read_list(p)
