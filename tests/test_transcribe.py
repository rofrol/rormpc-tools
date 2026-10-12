"""musicdb lyrics transcribe with a fake backend: which songs qualify, the sidecar, the checks and the lines.
No audio is decoded and no model runs: the backend and the audio identity are replaced."""
import hashlib, json, os, shutil, struct, types, wave

import pytest

from rormpc_tools import lyrics, settings, transcribe, translation


def words_of(*phrases, start=10.0, step=0.4, gap=1.5):
    """One Whisper segment per phrase, words `step` apart, phrases `gap` apart."""
    segs, t = [], start
    for phrase in phrases:
        ws = []
        for w in phrase.split():
            ws.append({"word": " " + w, "start": round(t, 3), "end": round(t + step - 0.05, 3), "probability": 0.9})
            t += step
        segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": " " + phrase, "words": ws})
        t += gap
    return segs


VERSE = ["I walk the empty road tonight", "the city lights are fading out", "and every step is calling you",
         "hold on, hold on to me", "I walk the empty road tonight", "and every step is calling you"]


def result(segments=None, regions=((5.0, 120.0),), duration=180.0, language=("en", "vote"), skipped=None):
    return {"backend": "mlx-whisper", "model": transcribe.MODEL, "versions": {"mlx-whisper": "0"},
            "duration": duration, "vad": {"regions": [list(r) for r in regions],
                                          "speech_seconds": sum(e - s for s, e in regions)},
            "language": {"code": language[0], "how": language[1], "votes": {language[0]: 3}},
            "skipped": skipped, "segments": words_of(*VERSE) if segments is None else segments}


@pytest.fixture
def tx(tmp_path, monkeypatch):
    """Lyrics dir, music dir and a fake backend; returns a namespace to set the backend's answer and see its calls."""
    lyr, music = tmp_path / "lyrics", tmp_path / "music"
    lyr.mkdir(); music.mkdir()
    monkeypatch.setattr(lyrics, "LYRICS", lyr)
    monkeypatch.setattr(lyrics, "INDEX", lyr / "index.json")
    monkeypatch.setattr(translation, "LYRICS", lyr)
    monkeypatch.setattr(settings, "MUSIC_DIR", music)
    monkeypatch.setattr(transcribe, "check_backend", lambda: None)
    monkeypatch.setattr(transcribe, "audio_identity",
                        lambda p: {"pcm_sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "samples": 1, "rate": 16000})
    ns = types.SimpleNamespace(lyrics=lyr, music=music, calls=[], out=result())

    def fake_backend(path, identity, language):
        ns.calls.append((path.relative_to(music).as_posix(), language))
        if isinstance(ns.out, Exception):
            raise ns.out
        return ns.out

    monkeypatch.setattr(transcribe, "run_backend", fake_backend)

    def song(rel, audio=b"audio", state=None):
        (music / rel).parent.mkdir(parents=True, exist_ok=True)
        (music / rel).write_bytes(audio)
        if state:
            index = lyrics.load()
            index[rel] = {"state": state}
            lyrics.save(index)
        return rel

    ns.song = song
    return ns


def run(*argv):
    lyrics.main(["transcribe", *argv])


def listing(d):
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())


def test_only_songs_without_lyrics(tx):
    for st in ("synced", "plain", "instrumental"):
        tx.song(f"A/{st}.mp3", state=st)
    tx.song("A/none.mp3", state="none")
    tx.song("A/untagged.mp3", state="untagged")
    tx.song("A/unchecked.mp3")
    tx.song("A/stray.mp3")
    (tx.lyrics / "A").mkdir(exist_ok=True)
    (tx.lyrics / "A/stray.txt").write_text("real words\n")  # a lyrics file without an index entry still counts
    run(*(f"A/{n}.mp3" for n in ("synced", "plain", "instrumental", "none", "untagged", "unchecked", "stray")))
    assert sorted(c[0] for c in tx.calls) == ["A/none.mp3", "A/unchecked.mp3", "A/untagged.mp3"]
    for n in ("synced", "plain", "instrumental", "stray"):
        assert not (tx.lyrics / f"A/{n}.transcript.json").exists()
    run("--force", "A/synced.mp3")  # --force never transcribes a song with real lyrics
    assert len(tx.calls) == 3


def test_never_touches_lyrics_files_or_index(tx):
    rel = tx.song("A/song.mp3", state="none")
    before = lyrics.INDEX.read_text()
    run(rel)
    assert lyrics.INDEX.read_text() == before
    assert listing(tx.lyrics) == ["A/song.transcript.json", "index.json"]


def test_sidecar_shown_with_label_lines_and_raw_words(tx):
    rel = tx.song("A/song.mp3")
    run(rel)
    rec = transcribe.load(rel)
    assert rec["state"] == "shown" and rec["reasons"] == []
    assert rec["label"] == "machine-transcribed by Whisper (large-v3), may be wrong"
    assert rec["language"] == {"code": "en", "how": "vote", "votes": {"en": 3}}
    assert [l["text"] for l in rec["lines"]] == VERSE
    assert len(rec["words"]) == sum(len(v.split()) for v in VERSE)
    first = rec["lines"][1]
    assert first["start"] == rec["words"][first["words"][0]]["start"]  # a line starts at its first word
    assert rec["audio"]["pcm_sha256"] == hashlib.sha256(b"audio").hexdigest()
    assert rec["thresholds"]["vad_threshold"] == transcribe.VAD_THRESHOLD


def test_atomic_write_leaves_old_sidecar_on_failure(tx, monkeypatch):
    rel = tx.song("A/song.mp3")
    run(rel)
    old = transcribe.sidecar(rel).read_text()

    def boom(rec, f, **kw):
        f.write("{partial")
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(OSError):
        transcribe.save(rel, {"state": "shown"})
    assert transcribe.sidecar(rel).read_text() == old
    assert listing(tx.lyrics / "A") == ["song.transcript.json"]  # no temporary file left behind


def test_same_audio_skipped_new_audio_and_force_redo(tx):
    rel = tx.song("A/song.mp3")
    run(rel)
    run(rel)
    assert len(tx.calls) == 1
    run("--force", rel)
    assert len(tx.calls) == 2
    tx.song(rel, audio=b"re-encoded")
    run(rel)
    assert len(tx.calls) == 3


def test_failed_is_retried_and_exits_nonzero(tx):
    rel = tx.song("A/song.mp3")
    tx.out = RuntimeError("backend exited with 1")
    with pytest.raises(SystemExit) as e:
        run(rel)
    assert e.value.code == 1
    assert transcribe.load(rel)["state"] == "failed"
    tx.out = result()
    run(rel)
    assert transcribe.load(rel)["state"] == "shown" and len(tx.calls) == 2


def test_language_override_first(tx):
    rel = tx.song("A/song.mp3")
    translation.set_lang(rel, "pl")
    run(rel)
    assert tx.calls == [(rel, "pl")]
    assert transcribe.load(rel)["language"] == {"code": "pl", "how": "override"}
    other = tx.song("A/other.mp3")
    run(other)
    assert tx.calls[-1] == (other, None)


def test_no_vocals(tx):
    rel = tx.song("A/instrumental.mp3")
    tx.out = result(segments=[], regions=[], skipped="no_vocals")
    run(rel)
    rec = transcribe.load(rel)
    assert rec["state"] == "no_vocals" and rec["lines"] == []
    # too little singing by the thresholds, even when the backend transcribed it
    tx.out = result(regions=[(30.0, 33.0)])
    run("--force", rel)
    assert transcribe.load(rel)["state"] == "no_vocals"


def judge(segments, regions=((5.0, 120.0),)):
    return transcribe.judge(result(segments=segments, regions=regions), {"pcm_sha256": "x"}, None)


def test_withheld_words_outside_speech():
    rec = judge(words_of(*VERSE), regions=[(5.0, 20.0), (60.0, 120.0)])
    assert rec["state"] == "withheld" and rec["reasons"] == ["words_outside_speech"]
    assert rec["lines"] and rec["words"]  # kept, only not shown


def test_withheld_repeated_line_but_not_a_chorus():
    rec = judge(words_of("Thank you.", *VERSE[:3], "Thank you.", "Thank you.", "Thank you.", "Thank you."))
    assert "repeated_line" in rec["reasons"]
    chorus = ["hold on to me", "never let me go", "hold on to me", "never let me go"] * 2
    assert judge(words_of(*VERSE, *chorus))["state"] == "shown"


def test_withheld_credit_line_only_outside_speech():
    segs = words_of(*VERSE[:3], "Subtitles by the Amara.org community", start=10.0)
    end = segs[-1]["words"][-1]["end"]
    regions = [(5.0, segs[2]["words"][-1]["end"]), (end + 5, end + 60)]
    rec = judge(segs, regions=regions)
    assert "credit_line_outside_speech" in rec["reasons"]
    assert "credit_line_outside_speech" not in judge(segs, regions=[(5.0, end + 1)])["reasons"]


def test_withheld_word_density():
    sparse = judge(words_of("hello there"), regions=[(5.0, 120.0)])
    assert sparse["reasons"] == ["low_word_density"]
    many = " ".join(["la"] * 80)
    dense = judge(words_of(many, step=0.1), regions=[(10.0, 15.0), (100.0, 105.0)])
    assert "high_word_density" in dense["reasons"]


def test_regroup_on_pauses_punctuation_and_length():
    one = {"words": []}
    t = 0.0
    for w, gap in [("Over", 0.05), ("the", 0.05), ("hills.", 0.05), ("We", 0.05), ("run,", 0.05), ("we", 0.05),
                   ("hide", 1.0), ("and", 0.05), ("we", 0.05), ("sing", 0.05), ("oh", 0.05), ("yeah", 0.0)]:
        one["words"].append({"word": " " + w, "start": t, "end": t + 0.3})
        t += 0.3 + gap
    words = transcribe.flat_words([one])
    lines = transcribe.make_lines(transcribe.regroup([one], words), words)
    assert [l["text"] for l in lines] == ["Over the hills.", "We run, we hide", "and we sing oh yeah"]
    assert lines[2]["start"] == words[7]["start"]
    long = {"words": [{"word": f" w{i}", "start": i * 0.3 + (0.2 if i == 9 else 0), "end": i * 0.3 + 0.25}
                      for i in range(20)]}
    words = transcribe.flat_words([long])
    groups = transcribe.regroup([long], words)
    assert all(len(g) <= transcribe.MAX_LINE_WORDS for g in groups) and groups[1][0] == 9


def test_short_fragment_merged_after_short_pause():
    seg = {"words": [{"word": " Take", "start": 0.0, "end": 0.3}, {"word": " me", "start": 0.35, "end": 0.6}]}
    tail = {"words": [{"word": " home", "start": 0.8, "end": 1.1}]}
    words = transcribe.flat_words([seg, tail])
    lines = transcribe.make_lines(transcribe.regroup([seg, tail], words), words)
    assert [l["text"] for l in lines] == ["Take me home"]


def test_playlist_and_argument_check(tx, env):
    a, b = tx.song("A/a.mp3"), tx.song("A/b.mp3", state="synced")
    m = env()
    m.playlists["radio"] = [a, b]
    run("--playlist", "radio")
    assert [c[0] for c in tx.calls] == [a]
    with pytest.raises(SystemExit):
        run()


def test_missing_uv_says_how_to_install(monkeypatch):
    monkeypatch.setattr(transcribe, "uv", lambda: None)
    with pytest.raises(SystemExit) as e:
        transcribe.check_backend()
    assert "uv not found" in str(e.value) and "install" in str(e.value).lower()


def test_missing_environment_says_how_to_install(monkeypatch):
    monkeypatch.setattr(transcribe, "uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(transcribe.subprocess, "run",
                        lambda *a, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr="No module named torch"))
    with pytest.raises(SystemExit) as e:
        transcribe.check_backend()
    assert "musicdb lyrics transcribe --install" in str(e.value)


def test_backend_script_is_shipped_with_inline_dependencies():
    text = transcribe.BACKEND.read_text()
    assert text.startswith("# /// script") and '"demucs==4.0.1"' in text and '"torch==2.5.1"' in text
    assert transcribe.MODEL == "mlx-community/whisper-large-v3-mlx"
    assert transcribe.label(transcribe.MODEL) == "machine-transcribed by Whisper (large-v3), may be wrong"
    assert os.path.basename(transcribe.BACKEND) == "transcribe_backend.py"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_audio_identity_is_the_pcm_not_the_file(tmp_path):
    pcm = b"".join(struct.pack("<h", (i * 37) % 3000 - 1500) for i in range(16000))
    paths = []
    for name, comment in (("a.wav", b""), ("b.wav", b"tagged")):
        p = tmp_path / name
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)
        if comment:  # a metadata chunk changes the file's bytes, not its audio
            data = p.read_bytes() + b"LIST" + struct.pack("<I", 4 + 8 + len(comment)) + b"INFOICMT" + \
                struct.pack("<I", len(comment)) + comment
            p.write_bytes(data[:4] + struct.pack("<I", len(data) - 8) + data[8:])
        paths.append(p)
    a, b = (transcribe.audio_identity(p) for p in paths)
    assert a == b and a["samples"] == 16000 and a["pcm_sha256"] == hashlib.sha256(pcm).hexdigest()
