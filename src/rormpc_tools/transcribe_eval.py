"""An evaluation run for the transcript checks: how today's thresholds in transcribe.py judge labelled songs, and
which values would judge them right (docs/transcribe-lyrics-plan.md, "Build order" step 1).

  musicdb lyrics transcribe-eval LIST [--out DIR] [--json]   # run the backend once per song, keep its JSON in DIR
  musicdb lyrics transcribe-eval LIST --from DIR [--json]    # judge again from DIR, without the backend

LIST is a text file kept outside the repository, one song per line: `<path relative to the music dir><TAB><label>`;
blank lines and lines starting with # are skipped. Labels:
  lyrics        the song has LRCLIB synced lyrics in the lyrics dir (<song>.lrc): the reference text and times
  instrumental  labelled by hand: no singing
Nothing is written to the lyrics dir: no sidecar, .lrc, .txt or index.json. The backend's raw JSON for each song goes
to DIR (default a new temporary directory, printed on stderr), one file per song keyed by its path; a rerun with the
same DIR reuses a song's result while the backend's inputs (model, VAD and no_vocals values) are the same.

Per song: the measures each check compares (share of words outside speech and the SPEECH_PAD that would pass it,
the top line's repeat count and factor over the next one, words per second of speech, VAD speech seconds and their
share of the song), the state and reasons transcribe.judge gives with today's constants; for `lyrics` songs the WER
against the reference (both lowercased, punctuation removed: transcribe.norm) and the sync (for each reference line
the distance to the nearest transcript line start: the median and the share within SYNC_WITHIN); for `instrumental`
songs whether they ended no_vocals.

Separating ranges: for each threshold, the values that keep its check from firing on every `lyrics` song and make it
catch every `instrumental` song no other check (at today's values) catches; or which songs make that impossible.
The VAD values and LANG_WINDOWS change the backend's output itself: they are calibrated by rerunning with other
constants, not from saved results.
"""
import hashlib, json, math, os, pathlib, statistics, sys, tempfile

from . import lyrics, lyrics_check, settings, transcribe

LABELS = ("lyrics", "instrumental")
SYNC_WITHIN = 1.0  # seconds: the plan's measure, "lines within 1 s"
# (threshold, transcribe's constant, how its check fires on the song's input x, the catch it makes); "gt": x > T,
# "ge": x >= T, "lt": x < T
CHECKS = [("no_vocals_min_seconds", "NO_VOCALS_MIN_SECONDS", "lt", "no_vocals_seconds"),
          ("no_vocals_min_ratio", "NO_VOCALS_MIN_RATIO", "lt", "no_vocals_ratio"),
          ("speech_pad", "SPEECH_PAD", "gt", "words_outside_speech"),
          ("outside_speech_max", "OUTSIDE_SPEECH_MAX", "gt", "words_outside_speech"),
          ("repeat_min", "REPEAT_MIN", "ge", "repeated_line"),
          ("repeat_factor", "REPEAT_FACTOR", "ge", "repeated_line"),
          ("density_min", "DENSITY_MIN", "lt", "low_word_density"),
          ("density_max", "DENSITY_MAX", "gt", "high_word_density")]
BACKEND_ONLY = ("vad_threshold", "vad_min_speech_ms", "vad_min_silence_ms", "lang_windows")


# ---------------------------------------------------------------- the list and the raw results

def read_list(path):
    songs = []
    for n, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        rel, sep, label = line.partition("\t")
        if not sep or not rel.strip() or label.strip() not in LABELS:
            sys.exit(f"{path}:{n}: want <path relative to the music dir><TAB>lyrics|instrumental")
        songs.append((rel, label.strip()))
    return songs


def backend_params():
    """The values the backend's output depends on."""
    t = transcribe
    return {"model": t.MODEL, "vad_threshold": t.VAD_THRESHOLD, "vad_min_speech_ms": t.VAD_MIN_SPEECH_MS,
            "vad_min_silence_ms": t.VAD_MIN_SILENCE_MS, "no_vocals_min_seconds": t.NO_VOCALS_MIN_SECONDS,
            "no_vocals_min_ratio": t.NO_VOCALS_MIN_RATIO, "lang_windows": t.LANG_WINDOWS}


def raw_path(d, rel):
    return pathlib.Path(d) / (hashlib.sha256(rel.encode()).hexdigest()[:16] + ".json")


def load_raw(d, rel):
    try:
        rec = json.loads(raw_path(d, rel).read_text())
    except (OSError, ValueError):
        return None
    return rec if rec.get("file") == rel else None


def save_raw(d, rec):
    p = raw_path(d, rec["file"])
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        os.unlink(tmp)
        raise


def obtain(rel, out_dir, from_dir):
    """(raw record, None), or (None, why not): from DIR only with --from, else a saved result for the same backend
    inputs or a new backend run."""
    if from_dir:
        rec = load_raw(from_dir, rel)
        return (rec, None) if rec else (None, f"no saved result in {from_dir}")
    rec = load_raw(out_dir, rel)
    if rec and rec.get("params") == backend_params():
        return rec, None
    path = settings.MUSIC_DIR / rel
    if not path.exists():
        return None, "no such file"
    identity = transcribe.audio_identity(path)
    override = transcribe.language_override(rel)
    try:
        out = transcribe.run_backend(path, identity, override)
    except RuntimeError as e:
        return None, str(e)
    rec = {"file": rel, "identity": identity, "language_override": override, "params": backend_params(), "out": out}
    save_raw(out_dir, rec)
    return rec, None


# ---------------------------------------------------------------- the measures

def reference(rel):
    """[(start, normalized words)] of the song's synced lyrics in time order, or None without an .lrc."""
    try:
        text = lyrics.paths(rel)[0].read_text()
    except OSError:
        return None
    lines = sorted((at, transcribe.norm(words)) for at, words, _ in lyrics_check.parse(text))
    return [(at, w) for at, w in lines if w] or None


def wer(ref, hyp):
    """Word error rate of the word list hyp against ref: substitutions, deletions and insertions over len(ref)."""
    if not ref:
        return None
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def sync(ref_starts, line_starts):
    """For each reference line the distance to the nearest transcript line start: median, share within SYNC_WITHIN."""
    if not ref_starts or not line_starts:
        return None
    d = [min(abs(r - s) for s in line_starts) for r in ref_starts]
    return {"median": statistics.median(d), "within": sum(x <= SYNC_WITHIN for x in d) / len(d), "lines": len(d)}


def distance(word, regions):
    """How far a word's middle lies outside the VAD regions (0 inside): it is in speech while this <= SPEECH_PAD."""
    mid = (word["start"] + word["end"]) / 2
    return min((max(s - mid, mid - e, 0.0) for s, e in regions), default=math.inf)


def pad_needed(words, regions):
    """The smallest SPEECH_PAD at which words_outside_speech would not fire, with today's OUTSIDE_SPEECH_MAX."""
    n = len(words)
    allowed = max(c for c in range(n + 1) if c / n <= transcribe.OUTSIDE_SPEECH_MAX)
    if allowed >= n:
        return 0.0
    return sorted((distance(w, regions) for w in words), reverse=True)[allowed]


def evaluate(rel, label, raw):
    """One song's report from its raw backend result."""
    t, out = transcribe, raw["out"]
    rec = t.judge(out, raw.get("identity") or {}, raw.get("language_override"))
    regions = out.get("vad", {}).get("regions", [])
    speech, duration = sum(e - s for s, e in regions), out.get("duration") or 0
    segments = out.get("segments", [])
    words = t.flat_words(segments)
    lines = t.make_lines(t.regroup(segments, words), words)
    ratio = speech / duration if duration else None
    m = {"speech_seconds": speech, "speech_ratio": ratio, "duration": duration, "words": len(words),
         "lines": len(lines), "outside_share": None, "pad_needed": None, "repeat_top": None, "repeat_next": None,
         "repeat_factor": None, "density": None}
    x = dict.fromkeys(c[0] for c in CHECKS)
    x["no_vocals_min_seconds"], x["no_vocals_min_ratio"] = speech, ratio
    catches = set()
    if speech < t.NO_VOCALS_MIN_SECONDS:
        catches.add("no_vocals_seconds")
    if ratio is not None and ratio < t.NO_VOCALS_MIN_RATIO:
        catches.add("no_vocals_ratio")
    if out.get("skipped") == "no_vocals" and not catches:
        catches.add("no_vocals_backend")  # skipped by the backend with other values than today's
    if words:
        w = t.measures(lines, words, regions)
        factor = w["repeat_top"] / max(w["repeat_next"], 1)
        m |= {"outside_share": w["outside_share"], "pad_needed": pad_needed(words, regions),
              "repeat_top": w["repeat_top"], "repeat_next": w["repeat_next"], "repeat_factor": factor,
              "density": w["density"]}
        x |= {"speech_pad": m["pad_needed"], "outside_speech_max": w["outside_share"], "density_min": w["density"],
              "repeat_min": w["repeat_top"] if w["repeat_top"] >= t.REPEAT_FACTOR * max(w["repeat_next"], 1)
              else None,
              "repeat_factor": factor if w["repeat_top"] >= t.REPEAT_MIN else None,
              "density_max": w["density"] if w["density"] >= t.DENSITY_MIN else None}
        catches |= set(t.withheld_reasons(lines, words, regions))
    if not lines:
        catches.add("no_words")
    song = {"file": rel, "label": label, "state": rec["state"], "reasons": rec["reasons"], "measures": m,
            "inputs": x, "catches": sorted(catches)}
    if label == "lyrics":
        ref = reference(rel)
        song["reference_lines"] = len(ref) if ref else 0
        song["wer"] = wer(" ".join(w for _, w in ref).split(), t.norm(" ".join(w["word"] for w in words)).split()) \
            if ref else None
        song["sync"] = sync([at for at, _ in ref], [line["start"] for line in lines]) if ref else None
        song["ok"] = rec["state"] == "shown"
    else:
        song["no_vocals"] = rec["state"] == "no_vocals"
        song["ok"] = rec["state"] in ("no_vocals", "withheld")
    return song


# ---------------------------------------------------------------- separating ranges

def separating(songs, threshold, kind, catch):
    """The values of one threshold that keep its check off every lyrics song and make it catch every instrumental
    that no other check catches: {"range": [lo, lo_inclusive, hi, hi_inclusive] or None, "lyrics": [files that block
    it], "instrumentals": [files that block it], "relied_on": how many instrumentals need this check}."""
    lyr = [s for s in songs if s["label"] == "lyrics" and s["inputs"][threshold] is not None]
    need = [s for s in songs if s["label"] == "instrumental" and not set(s["catches"]) - {catch}]
    uncatchable = [s["file"] for s in need if s["inputs"][threshold] is None]
    need = [s for s in need if s["inputs"][threshold] is not None]
    x = lambda s: s["inputs"][threshold]
    xl, xi = [x(s) for s in lyr], [x(s) for s in need]
    if kind in ("gt", "ge"):  # fires at T below the input: lyrics bound it from below, instrumentals from above
        lo, hi, lo_in, hi_in = max(xl, default=-math.inf), min(xi, default=math.inf), kind == "gt", kind == "ge"
        bad_l, bad_i = [s for s in lyr if x(s) >= hi], [s for s in need if x(s) <= lo]
    else:  # fires at T above the input
        lo, hi, lo_in, hi_in = max(xi, default=-math.inf), min(xl, default=math.inf), False, True
        bad_l, bad_i = [s for s in lyr if x(s) <= lo], [s for s in need if x(s) >= hi]
    empty = lo > hi or (lo == hi and not (lo_in and hi_in))
    return {"threshold": threshold, "range": None if empty or uncatchable else [lo, lo_in, hi, hi_in],
            "lyrics": [s["file"] for s in bad_l] if empty else [],
            "instrumentals": ([s["file"] for s in bad_i] if empty else []) + uncatchable,
            "relied_on": len(need) + len(uncatchable)}


def inside(rng, value):
    lo, lo_in, hi, hi_in = rng
    return (lo < value or (lo_in and lo == value)) and (value < hi or (hi_in and value == hi))


def ranges(songs):
    out = []
    for threshold, const, kind, catch in CHECKS:
        r = separating(songs, threshold, kind, catch)
        today = getattr(transcribe, const)
        out.append(r | {"today": today, "today_inside": bool(r["range"]) and inside(r["range"], today)})
    return out


# ---------------------------------------------------------------- the report

def fmt(x, spec=".3g"):
    if x is None:
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf" if x > 0 else "-inf"
    return format(x, spec)


def interval(rng):
    lo, lo_in, hi, hi_in = rng
    left, right = "[" if lo_in and math.isfinite(lo) else "(", "]" if hi_in and math.isfinite(hi) else ")"
    return f"{left}{fmt(lo)}, {fmt(hi)}{right}"


def plain(x):
    """JSON without infinities."""
    if isinstance(x, float) and math.isinf(x):
        return "inf" if x > 0 else "-inf"
    if isinstance(x, dict):
        return {k: plain(v) for k, v in x.items()}
    if isinstance(x, list):
        return [plain(v) for v in x]
    return x


def summary(songs):
    lyr = [s for s in songs if s["label"] == "lyrics" and "error" not in s]
    ins = [s for s in songs if s["label"] == "instrumental" and "error" not in s]
    wers = [s["wer"] for s in lyr if s["wer"] is not None]
    syncs = [s["sync"]["median"] for s in lyr if s["sync"]]
    return {"lyrics": len(lyr), "lyrics_shown": sum(s["ok"] for s in lyr),
            "instrumentals": len(ins), "instrumentals_caught": sum(s["ok"] for s in ins),
            "instrumentals_no_vocals": sum(s["no_vocals"] for s in ins),
            "mean_wer": statistics.mean(wers) if wers else None,
            "median_sync": statistics.median(syncs) if syncs else None,
            "failed": sum("error" in s for s in songs)}


def print_text(songs, rngs, raw_dir):
    print(f"{'label':<12} {'state':<9} {'ok':<3} {'outside':>7} {'pad':>5} {'repeat':>9} {'density':>7} "
          f"{'speech':>7} {'ratio':>5} {'WER':>5} {'sync':>5} {'<=1s':>5}  file")
    for s in songs:
        if "error" in s:
            print(f"{s['label']:<12} {'failed':<9} {'no':<3} {'':>7} {'':>5} {'':>9} {'':>7} {'':>7} {'':>5} "
                  f"{'':>5} {'':>5} {'':>5}  {s['file']}")
            continue
        m, sy = s["measures"], s.get("sync")
        rep = f"{m['repeat_top']}/{m['repeat_next']} {fmt(m['repeat_factor'], '.1f')}x" if m["repeat_top"] else "-"
        print(f"{s['label']:<12} {s['state']:<9} {'yes' if s['ok'] else 'no':<3} "
              f"{fmt(m['outside_share'], '.2f'):>7} {fmt(m['pad_needed'], '.2f'):>5} {rep:>9} "
              f"{fmt(m['density'], '.2f'):>7} {fmt(m['speech_seconds'], '.1f'):>7} {fmt(m['speech_ratio'], '.2f'):>5} "
              f"{fmt(s.get('wer'), '.2f'):>5} {fmt(sy and sy['median'], '.2f'):>5} "
              f"{fmt(sy and sy['within'] * 100, '.0f') + ('%' if sy else ''):>5}  {s['file']}")
    for s in songs:
        if "error" in s:
            print(f"{s['file']}: failed: {s['error']}")
        elif not s["ok"]:
            print(f"{s['file']}: {s['label']}, {s['state']}" + (f": {', '.join(s['reasons'])}" if s["reasons"] else ""))
    t = summary(songs)
    print(f"lyrics: {t['lyrics_shown']}/{t['lyrics']} shown, mean WER {fmt(t['mean_wer'], '.2f')}, median sync "
          f"{fmt(t['median_sync'], '.2f')} s; instrumentals: {t['instrumentals_caught']}/{t['instrumentals']} "
          f"no_vocals or withheld ({t['instrumentals_no_vocals']} no_vocals)"
          + (f"; {t['failed']} failed" if t["failed"] else ""))
    print("separating ranges (each threshold alone, the others at today's values):")
    for r in rngs:
        head = f"  {r['threshold']:<22} today {fmt(r['today'], 'g'):<6} "
        if r["range"]:
            print(head + interval(r["range"]) + (" (today inside)" if r["today_inside"] else " (today outside)")
                  + ("" if r["relied_on"] else ", no instrumental relies on it"))
        else:
            blocks = [f"lyrics {', '.join(r['lyrics'])}"] * bool(r["lyrics"]) + \
                     [f"instrumentals {', '.join(r['instrumentals'])}"] * bool(r["instrumentals"])
            print(head + "no value separates them: " + "; ".join(blocks))
    print(f"  {', '.join(BACKEND_ONLY)}: change the backend's output; rerun with other constants")
    print(f"raw backend results: {raw_dir}")


def cmd(a):
    songs_in = read_list(a.list)
    if a.from_dir:
        raw_dir = pathlib.Path(a.from_dir)
        if not raw_dir.is_dir():
            sys.exit(f"musicdb lyrics transcribe-eval: no directory {raw_dir}")
    else:
        transcribe.check_backend()
        raw_dir = pathlib.Path(a.out) if a.out else pathlib.Path(tempfile.mkdtemp(prefix="transcribe-eval-"))
        raw_dir.mkdir(parents=True, exist_ok=True)
    print(f"raw backend results: {raw_dir}", file=sys.stderr)
    songs = []
    for rel, label in songs_in:
        try:
            raw, why = obtain(rel, None if a.from_dir else raw_dir, raw_dir if a.from_dir else None)
            songs.append(evaluate(rel, label, raw) if raw else {"file": rel, "label": label, "error": why,
                                                                  "ok": False})
        except Exception as e:  # one bad song must not lose the others' results
            songs.append({"file": rel, "label": label, "error": f"{type(e).__name__}: {e}", "ok": False})
    judged = [s for s in songs if "error" not in s]
    rngs = ranges(judged)
    if a.json:
        print(json.dumps(plain({"raw_dir": str(raw_dir), "thresholds": transcribe.thresholds(),
                                "summary": summary(songs), "songs": songs, "ranges": rngs}), ensure_ascii=False))
    else:
        print_text(songs, rngs, raw_dir)
    if len(judged) < len(songs):
        sys.exit(1)
