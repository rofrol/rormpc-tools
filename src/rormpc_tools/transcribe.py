"""Transcripts of songs that have no lyrics, made locally by Whisper and kept beside the lyrics, labelled
machine-transcribed (docs/transcribe-lyrics-plan.md).

  musicdb lyrics transcribe FILE ...          # paths relative to the music dir
  musicdb lyrics transcribe --current         # the playing song
  musicdb lyrics transcribe --playlist NAME   # every song of an MPD playlist
  musicdb lyrics transcribe --force ...       # transcribe again, even the same audio
  musicdb lyrics transcribe --install         # set up the backend's environment and models (about 3 GB)

Only songs whose LRCLIB state is none or untagged, or not checked yet, and that have no .lrc/.txt: a song with real
lyrics is never transcribed, --force included. The heavy work (ffmpeg, demucs, Silero VAD, Whisper large-v3) runs in
data/transcribe_backend.py, a script with inline dependencies run through `uv run --script`, so torch and the Whisper
backend never enter rormpc-tools' environment. This module picks the songs, checks the result and writes the sidecar.

Storage: <lyrics_dir>/<song path stem>.transcript.json, written atomically; never .lrc/.txt, never index.json:
  version         1
  state           shown (passed the checks) | withheld (kept, not shown by default; `reasons`) | no_vocals (too
                  little singing on the vocal stem: nothing to show; not "instrumental", which stays LRCLIB's or
                  the user's statement) | failed (`note`)
  label           "machine-transcribed by Whisper (<model>), may be wrong"
  reasons         why a transcript is withheld: words_outside_speech, repeated_line, credit_line_outside_speech,
                  low_word_density, high_word_density
  audio           {"pcm_sha256", "samples", "rate"}: the identity of the 16 kHz mono PCM; a song with a sidecar for
                  the same identity is skipped unless --force (a tag edit or a move keeps it, a new encoding not)
  language        {"code", "how": override (`musicdb lyrics lang`) | vote (Whisper over VAD-started windows), ...}
  backend, model, versions   which recognizer made it
  vad             {"regions": [[start, end], ...], "speech_seconds", "duration"}
  lines           [{"start", "end", "text", "words": [first, last]}]: regrouped lines, indexes into `words`
  words           [{"word", "start", "end", "probability"}]: Whisper's raw words, so lines can be regrouped later
                  without transcribing again
  thresholds      the check values this transcript was judged with
  transcribed_at  when it was made
"""
import datetime as dt, hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile, unicodedata

from . import lyrics, settings, translation

BACKEND = pathlib.Path(__file__).with_name("data") / "transcribe_backend.py"
MODEL = "mlx-community/whisper-large-v3-mlx"  # decided by the user 2026-10-11: large-v3, not the measured turbo
CACHE = settings.XDG_CACHE / "rormpc-tools" / "transcribe" / "vocals"  # vocal stems by audio identity
RATE = 16000
QUALIFY = {None, "none", "untagged"}  # LRCLIB states that may be transcribed (None: not checked yet)

# Provisional: every value below is to be calibrated on the evaluation set (plan, build order step 1); until then
# they follow the plan's reasoning and the 2026-10-11 measurements.
# Silero VAD on the vocal stem: its default speech probability, a short sung word as the shortest speech, and a
# breath between phrases kept inside one region.
VAD_THRESHOLD = 0.5
VAD_MIN_SPEECH_MS = 250
VAD_MIN_SILENCE_MS = 300
# no_vocals below this much singing: Silero found nothing on the instrumental's stem, songs sing for minutes
NO_VOCALS_MIN_SECONDS = 10.0
NO_VOCALS_MIN_RATIO = 0.05
LANG_WINDOWS = 5  # 30 s windows voting on the language
# withheld checks (plan step 6)
SPEECH_PAD = 0.5  # seconds a word may stray outside a VAD region (Whisper's word times drift that much)
OUTSIDE_SPEECH_MAX = 0.25  # share of words outside speech; the hallucinated "Thank you." lies entirely outside
REPEAT_MIN = 4  # a line this often, and REPEAT_FACTOR times the next most repeated line: a loop, not a chorus,
REPEAT_FACTOR = 3  # whose lines repeat together
DENSITY_MIN = 0.3  # words per second of speech; sung lyrics run about 1-3
DENSITY_MAX = 6.0
# Whisper's subtitle-credit hallucinations; one counts only when it also falls outside speech ("Thank you" can be
# a lyric, so this is never a blind blocklist)
CREDIT_LINES = re.compile(r"subtit|untertitel|napisy|sous-titr|teksting|tekstet|amara\.org|transcri|caption|"
                          r"thanks? (you )?for watching|subscribe|www\.|\.com\b", re.I)
# line regrouping (plan step 7, stable-ts' default regrouping): split on a pause, after a sentence end, after a
# comma once a line has words enough; merge a short fragment after a short pause; cap the length
LINE_GAP = 0.5
COMMA_MIN_WORDS = 4
MERGE_GAP = 0.3
MERGE_MAX_WORDS = 3
MAX_LINE_WORDS = 12

INSTALL_HINT = ("the transcription backend is not installed: run `musicdb lyrics transcribe --install` "
                "(uv sync --script for torch, demucs, Silero VAD and mlx-whisper, then the models, about 3 GB)")


def sidecar(rel):
    return (lyrics.LYRICS / rel).with_suffix(".transcript.json")


def load(rel):
    try:
        return json.loads(sidecar(rel).read_text())
    except (OSError, ValueError):
        return None


def save(rel, rec):
    """Write the sidecar atomically: a temporary file in the same directory, then rename."""
    p = sidecar(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except BaseException:
        os.unlink(tmp)
        raise


def label(model):
    name = model.rsplit('/', 1)[-1].removeprefix('whisper-').removesuffix('-mlx')
    return f"machine-transcribed by Whisper ({name}), may be wrong"


# ---------------------------------------------------------------- which songs

def qualifies(rel, index):
    """None when the song may be transcribed, else why not."""
    state = (index.get(rel) or {}).get("state")
    if state not in QUALIFY:
        return f"has lyrics state {state}"
    lrc, txt = lyrics.paths(rel)
    if lrc.exists() or txt.exists():
        return "has a lyrics file"
    return None


def audio_identity(path):
    """sha256 of the 16 kHz mono PCM ffmpeg decodes, and its length."""
    h, n = hashlib.sha256(), 0
    with subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
                           "-ar", str(RATE), "-"], stdout=subprocess.PIPE) as p:
        for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    if p.returncode:
        raise RuntimeError(f"ffmpeg could not decode {path}")
    return {"pcm_sha256": h.hexdigest(), "samples": n // 2, "rate": RATE}


def language_override(rel):
    """The code set with `musicdb lyrics lang FILE CODE`, or None."""
    rec = translation.load(rel) or {}
    return rec.get("original_lang") if rec.get("original_lang_manual") else None


# ---------------------------------------------------------------- the backend

def uv():
    return shutil.which("uv")


def backend_cmd(*args):
    return ["uv", "run", "--quiet", "--script", str(BACKEND), *args]


def check_backend():
    """Exit with how to install it when uv or the script's environment is missing."""
    if not uv():
        sys.exit("musicdb lyrics transcribe: uv not found on PATH; the transcription backend runs through it. "
                 "Install: https://docs.astral.sh/uv/getting-started/installation/")
    p = subprocess.run(["uv", "run", "--quiet", "--offline", "--script", str(BACKEND), "check"],
                       capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"musicdb lyrics transcribe: {INSTALL_HINT}")


def install_backend():
    if not uv():
        sys.exit("musicdb lyrics transcribe: uv not found on PATH. Install: "
                 "https://docs.astral.sh/uv/getting-started/installation/")
    subprocess.run(["uv", "sync", "--script", str(BACKEND)], check=True)
    subprocess.run(backend_cmd("prepare", "--model", MODEL), check=True)
    print("transcription backend ready")


def run_backend(path, identity, language):
    """The backend's JSON for one song (see transcribe_backend.py); raises RuntimeError when it fails."""
    args = ["run", "--audio", str(path), "--key", identity["pcm_sha256"], "--cache-dir", str(CACHE),
            "--model", MODEL, "--vad-threshold", str(VAD_THRESHOLD), "--vad-min-speech-ms", str(VAD_MIN_SPEECH_MS),
            "--vad-min-silence-ms", str(VAD_MIN_SILENCE_MS), "--min-speech-seconds", str(NO_VOCALS_MIN_SECONDS),
            "--min-speech-ratio", str(NO_VOCALS_MIN_RATIO), "--lang-windows", str(LANG_WINDOWS)]
    if language:
        args += ["--language", language]
    p = subprocess.run(backend_cmd(*args), stdout=subprocess.PIPE, text=True)  # its progress goes to stderr
    if p.returncode:
        raise RuntimeError(f"backend exited with {p.returncode}")
    try:
        return json.loads(p.stdout)
    except ValueError:
        raise RuntimeError("backend printed no JSON") from None


# ---------------------------------------------------------------- checks and lines

def norm(text):
    s = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[^\w']+", " ", s).split())


def flat_words(segments):
    return [{"word": w["word"], "start": w["start"], "end": w["end"], "probability": w.get("probability")}
            for s in segments for w in s.get("words", []) if w.get("word", "").strip()]


def in_speech(word, regions):
    mid = (word["start"] + word["end"]) / 2
    return any(s - SPEECH_PAD <= mid <= e + SPEECH_PAD for s, e in regions)


def _ends_sentence(word):
    return word["word"].rstrip().endswith((".", "?", "!", "…", "。", "？", "！"))


def _split_long(line, words):
    """Split a line longer than MAX_LINE_WORDS at its longest pause (of equal ones, the one nearest the middle),
    recursively."""
    if len(line) <= MAX_LINE_WORDS:
        return [line]
    i = max(range(1, len(line)), key=lambda k: (round(words[line[k]]["start"] - words[line[k - 1]]["end"], 2),
                                                -abs(2 * k - len(line))))
    return _split_long(line[:i], words) + _split_long(line[i:], words)


def regroup(segments, words):
    """Lines as lists of word indexes: Whisper's segments split on pauses and punctuation, short fragments merged."""
    lines, k = [], 0
    for seg in segments:
        n = sum(1 for w in seg.get("words", []) if w.get("word", "").strip())
        cur = []
        for i in range(k, k + n):
            if cur and (words[i]["start"] - words[cur[-1]]["end"] > LINE_GAP or _ends_sentence(words[cur[-1]])
                        or (words[cur[-1]]["word"].rstrip().endswith((",", "，")) and len(cur) >= COMMA_MIN_WORDS)):
                lines.append(cur)
                cur = []
            cur.append(i)
        if cur:
            lines.append(cur)
        k += n
    merged = []
    for line in lines:
        prev = merged[-1] if merged else None
        if (prev and len(line) <= MERGE_MAX_WORDS and not _ends_sentence(words[prev[-1]])
                and words[line[0]]["start"] - words[prev[-1]]["end"] < MERGE_GAP
                and len(prev) + len(line) <= MAX_LINE_WORDS):
            prev.extend(line)
        else:
            merged.append(list(line))
    return [part for line in merged for part in _split_long(line, words)]


def make_lines(groups, words):
    return [{"start": words[g[0]]["start"], "end": words[g[-1]]["end"],
             "text": "".join(words[i]["word"] for i in g).strip(), "words": [g[0], g[-1]]} for g in groups]


def withheld_reasons(lines, words, regions):
    reasons = []
    if not words:
        return reasons
    outside = [not in_speech(w, regions) for w in words]
    if sum(outside) / len(words) > OUTSIDE_SPEECH_MAX:
        reasons.append("words_outside_speech")
    counts = {}
    for line in lines:
        key = norm(line["text"])
        if key:
            counts[key] = counts.get(key, 0) + 1
    top = sorted(counts.values(), reverse=True) + [0]
    if top[0] >= REPEAT_MIN and top[0] >= REPEAT_FACTOR * max(top[1], 1):
        reasons.append("repeated_line")
    for line in lines:
        a, b = line["words"]
        if CREDIT_LINES.search(line["text"]) and all(outside[a:b + 1]):
            reasons.append("credit_line_outside_speech")
            break
    speech = sum(e - s for s, e in regions)
    density = len(words) / speech if speech else float("inf")
    if density < DENSITY_MIN:
        reasons.append("low_word_density")
    elif density > DENSITY_MAX:
        reasons.append("high_word_density")
    return reasons


def thresholds():
    return {"vad_threshold": VAD_THRESHOLD, "vad_min_speech_ms": VAD_MIN_SPEECH_MS,
            "vad_min_silence_ms": VAD_MIN_SILENCE_MS, "no_vocals_min_seconds": NO_VOCALS_MIN_SECONDS,
            "no_vocals_min_ratio": NO_VOCALS_MIN_RATIO, "speech_pad": SPEECH_PAD,
            "outside_speech_max": OUTSIDE_SPEECH_MAX, "repeat_min": REPEAT_MIN, "repeat_factor": REPEAT_FACTOR,
            "density_min": DENSITY_MIN, "density_max": DENSITY_MAX}


def judge(out, identity, override):
    """The sidecar record for one backend result."""
    regions = out.get("vad", {}).get("regions", [])
    speech = sum(e - s for s, e in regions)
    duration = out.get("duration") or 0
    rec = {"version": 1, "audio": identity, "backend": out.get("backend"), "model": out.get("model") or MODEL,
           "versions": out.get("versions", {}),
           "vad": {"regions": regions, "speech_seconds": round(speech, 3), "duration": duration},
           "thresholds": thresholds(), "transcribed_at": dt.datetime.now().isoformat(timespec="seconds")}
    rec["label"] = label(rec["model"])
    if (out.get("skipped") == "no_vocals" or speech < NO_VOCALS_MIN_SECONDS
            or (duration and speech / duration < NO_VOCALS_MIN_RATIO)):
        return rec | {"state": "no_vocals", "reasons": [], "language": None, "lines": [], "words": []}
    lang = dict(out.get("language") or {})
    if override:
        lang = {"code": override, "how": "override"}
    segments = out.get("segments", [])
    words = flat_words(segments)
    lines = make_lines(regroup(segments, words), words)
    reasons = withheld_reasons(lines, words, regions)
    return rec | {"state": "withheld" if reasons or not lines else "shown",
                  "reasons": reasons or ([] if lines else ["no_words"]), "language": lang,
                  "lines": lines, "words": words}


# ---------------------------------------------------------------- the command

def transcribe_one(rel, index, force=False):
    """(outcome, record or None): outcome is a sidecar state, "skipped: <why>" or "same audio"."""
    why = qualifies(rel, index)
    if why:
        return f"skipped: {why}", None
    path = settings.MUSIC_DIR / rel
    if not path.exists():
        return "skipped: no such file", None
    identity = audio_identity(path)
    old = load(rel)
    if not force and old and old.get("audio") == identity and old.get("state") != "failed":
        return "same audio", old
    override = language_override(rel)
    try:
        out = run_backend(path, identity, override)
    except RuntimeError as e:
        rec = {"version": 1, "state": "failed", "note": str(e), "audio": identity, "model": MODEL,
               "transcribed_at": dt.datetime.now().isoformat(timespec="seconds")}
    else:
        rec = judge(out, identity, override)
    save(rel, rec)
    return rec["state"], rec


def describe(rec):
    """One status line for a sidecar."""
    if rec is None:
        return "no transcript"
    s = rec["state"]
    if s == "shown":
        return f"{rec['label']} ({len(rec['lines'])} lines, {rec['language']['code']})"
    if s == "withheld":
        return f"transcript withheld: {', '.join(rec['reasons'])}"
    if s == "no_vocals":
        return f"no vocals found ({rec['vad']['speech_seconds']:.0f} s of singing)"
    return f"transcription failed: {rec.get('note')}"


def songs(a):
    if a.current:
        return [lyrics.current_file()]
    if a.playlist:
        from .musicdb import mpd
        return list(mpd().listplaylist(a.playlist))
    return a.files


def cmd(a):
    if a.install:
        return install_backend()
    if sum(map(bool, (a.files, a.current, a.playlist))) != 1:
        sys.exit("give FILE ..., --current or --playlist NAME")
    files = songs(a)
    check_backend()
    index, counts, failed = lyrics.load(), {}, False
    for rel in files:
        try:
            outcome, rec = transcribe_one(rel, index, a.force)
        except Exception as e:  # one bad song must not lose the others' results
            print(f"{rel}: {e}", file=sys.stderr)
            failed = True
            continue
        failed |= outcome == "failed"
        key = outcome.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
        if len(files) <= 5 or not outcome.startswith("skipped"):
            print(f"{outcome}: {rel}" + (f" ({describe(rec)})" if rec and outcome != "same audio" else ""))
    print("transcripts: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing to do"))
    if failed:
        sys.exit(1)
