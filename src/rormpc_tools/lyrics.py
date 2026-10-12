"""Lyrics from LRCLIB for library songs, kept as files where rmpc's Lyrics pane looks for them.

  musicdb lyrics fetch --current        # the playing song
  musicdb lyrics fetch FILE ...         # paths relative to the music dir
  musicdb lyrics fetch --all            # every song not checked yet (--recheck: also songs that had none)
  musicdb lyrics candidates FILE        # what LRCLIB has for a song, as JSON (rormpc's "Choose lyrics…")
  musicdb lyrics use FILE ID            # take this LRCLIB entry for the song
  musicdb lyrics status [--json]        # counts, or every song's state
  musicdb lyrics check [FILE ...] [--dry-run]   # recompute the timing flags of synced lyrics (default: all)
  musicdb lyrics translate FILE|--current   # its Polish translation from tekstowo.pl (one song, on request)
  musicdb lyrics lang FILE CODE|auto    # override the detected language of a song's lyrics (pl: no translation)
  musicdb lyrics transcribe FILE ...|--current|--playlist NAME   # Whisper transcript of songs without lyrics

Files: <lyrics_dir>/<song path>.lrc for synced lyrics (rmpc: lyrics_dir in its config, same directory),
<song path>.txt for plain lyrics without timestamps. <lyrics_dir>/index.json records per song: synced, plain,
instrumental, none (LRCLIB has nothing within 2 s of the file's length) or untagged, with the LRCLIB id and when it
was checked, so a miss is not asked again until --recheck. LRCLIB matches on duration (about 2 s), so YouTube rips
with intros or outros often miss; `candidates` + `use` pick another entry by hand.
Timing flags (lyrics_check.py): a synced entry whose lines run past the file's end, go back in time or end long
before the file does is flagged "lyrics end early, check timing" in its record, "suspect": [{"reason", "label",
numbers}], never rejected: the lyrics are still written and shown. fetch and use set it, check recomputes it.
Translations: <song path>.pl.json beside the lyrics, outside index.json (see translation.py).
Transcripts: <song path>.transcript.json beside the lyrics, outside index.json, never .lrc/.txt (see transcribe.py).
"""
import argparse, datetime as dt, json, subprocess, sys, urllib.parse

from . import lyrics_check, mbtag, settings

LYRICS = settings.LYRICS_DIR
INDEX = LYRICS / "index.json"
API = "https://lrclib.net/api"
SLACK = 2  # seconds: LRCLIB's own matching tolerance


def load():
    return json.loads(INDEX.read_text()) if INDEX.exists() else {}


def save(index):
    LYRICS.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True))
    tmp.replace(INDEX)


def song_tags(m, rel):
    t = (m.find("file", rel) or [{}])[0]
    one = lambda k: (t.get(k)[0] if isinstance(t.get(k), list) else t.get(k)) or ""
    return one("artist"), one("title"), one("album"), float(one("duration") or one("time") or 0)


def search(artist, title):
    q = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    return mbtag.http(f"{API}/search?{q}", host_interval=0.5, attempts=3, timeout=20) or []


def best(cands, duration):
    """Closest entry within SLACK seconds, synced lyrics first, then instrumental, then plain."""
    near = [c for c in cands if not duration or abs((c.get("duration") or 0) - duration) <= SLACK]
    rank = lambda c: (not c.get("syncedLyrics"), not c.get("instrumental"), not c.get("plainLyrics"),
                      abs((c.get("duration") or 0) - duration))
    return min(near, key=rank) if near else None


def paths(rel):
    base = LYRICS / rel
    return base.with_suffix(".lrc"), base.with_suffix(".txt")


def last_lines(cands, entry, duration):
    """Last-line starts of the other synced candidates of the same cut (within SLACK of the file's length)."""
    return [x for c in cands if c.get("syncedLyrics") and c.get("id") != entry.get("id")
            and (not duration or abs((c.get("duration") or 0) - duration) <= SLACK)
            and (x := lyrics_check.last_line(c["syncedLyrics"])) is not None]


def write(rel, entry, artist, title, how, duration=0, cands=()):
    """Store one LRCLIB entry for a song; returns the index record. Synced lyrics get their timing flags, with the
    other candidates (when at hand) as supporting evidence."""
    lrc, txt = paths(rel)
    lrc.parent.mkdir(parents=True, exist_ok=True)
    for p in (lrc, txt):
        p.unlink(missing_ok=True)
    rec = {"checked_at": dt.datetime.now().isoformat(timespec="seconds"), "artist": artist, "title": title,
           "lrclib_id": entry.get("id") if entry else None, "how": how}
    if not entry:
        return rec | {"state": "none"}
    if entry.get("instrumental"):
        return rec | {"state": "instrumental"}
    if entry.get("syncedLyrics"):
        head = f"[ar:{artist}]\n[ti:{title}]\n[re:lrclib.net #{entry['id']}]\n"
        lrc.write_text(head + entry["syncedLyrics"].rstrip() + "\n")
        rec["state"] = "synced"
        if suspect := lyrics_check.reasons(entry["syncedLyrics"], duration, last_lines(cands, entry, duration)):
            rec["suspect"] = suspect
        return rec
    if entry.get("plainLyrics"):
        txt.write_text(entry["plainLyrics"].rstrip() + "\n")
        return rec | {"state": "plain"}
    return rec | {"state": "none"}


def fetch_one(m, rel, index):
    artist, title, _album, duration = song_tags(m, rel)
    if not (artist and title):
        index[rel] = {"state": "untagged", "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
        return index[rel]
    cands = search(artist, title)
    index[rel] = write(rel, best(cands, duration), artist, title, "auto", duration, cands)
    return index[rel]


def current_file():
    p = subprocess.run(["mpc", "-f", "%file%", "current"], capture_output=True, text=True)
    f = p.stdout.strip()
    if not f:
        sys.exit("nothing is playing")
    return f


def cmd_fetch(a):
    from .musicdb import mpd
    m, index = mpd(), load()
    if a.current:
        files = [current_file()]
    elif a.all:
        retry = {"none", "untagged"} if a.recheck else set()
        files = [s["file"] for s in m.listallinfo() if s.get("file")
                 and (s["file"] not in index or index[s["file"]]["state"] in retry)]
    else:
        files = a.files
    counts = {}
    for i, rel in enumerate(files, 1):
        try:
            rec = fetch_one(m, rel, index)
        except Exception as e:  # one bad song must not lose the others' results
            print(f"{rel}: {e}", file=sys.stderr)
            continue
        counts[rec["state"]] = counts.get(rec["state"], 0) + 1
        if len(files) <= 5:
            print(f"{rec['state']}: {rel}")
        if i % 25 == 0:
            save(index)  # long runs keep what they found
            print(f"{i}/{len(files)}", flush=True)
    save(index)
    print("lyrics: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "nothing to fetch"))


def cmd_candidates(a):
    from .musicdb import mpd
    artist, title, _album, duration = song_tags(mpd(), a.file)
    cands = search(a.artist or artist, a.title or title)
    out = [{"id": c["id"], "artist": c.get("artistName"), "title": c.get("trackName"), "album": c.get("albumName"),
            "duration": c.get("duration"), "delta": round((c.get("duration") or 0) - duration, 1),
            "synced": bool(c.get("syncedLyrics")), "plain": bool(c.get("plainLyrics")),
            "instrumental": bool(c.get("instrumental")),
            "suspect": lyrics_check.reasons(c["syncedLyrics"], duration, last_lines(cands, c, duration))
            if c.get("syncedLyrics") else []} for c in cands]
    out.sort(key=lambda c: (abs(c["delta"]), not c["synced"]))
    print(json.dumps({"file": a.file, "duration": duration, "current": load().get(a.file), "candidates": out},
                     ensure_ascii=False))


def cmd_use(a):
    from .musicdb import mpd
    entry = mbtag.http(f"{API}/get/{a.id}", host_interval=0.5, attempts=3, timeout=20)
    if not entry:
        sys.exit(f"LRCLIB has no entry {a.id}")
    artist, title, _album, duration = song_tags(mpd(), a.file)
    index = load()
    index[a.file] = write(a.file, entry, artist, title, "chosen", duration)
    save(index)
    print(f"lyrics: {index[a.file]['state']} from LRCLIB #{a.id}")
    if reasons := lyrics_check.flagged(index[a.file].get("suspect")):
        print(f"{lyrics_check.LABEL} ({', '.join(reasons)})")


def cmd_status(a):
    index = load()
    if a.json:
        print(json.dumps(index, ensure_ascii=False)); return
    counts = {}
    for rec in index.values():
        counts[rec["state"]] = counts.get(rec["state"], 0) + 1
    suspect = sum(bool(lyrics_check.flagged(rec.get("suspect"))) for rec in index.values())
    print(f"{LYRICS}: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
          + (f" ({suspect} synced flagged: {lyrics_check.LABEL})" if suspect else ""))


def cmd_check(a):
    """Recompute the timing flags of existing synced entries from their .lrc and the file's length in MPD (no
    candidates: no supporting evidence)."""
    from .musicdb import mpd
    m, index = mpd(), load()
    files = a.files or [rel for rel, rec in index.items() if rec.get("state") == "synced"]
    counts, checked, flagged = {}, 0, 0
    for rel in files:
        rec, lrc = index.get(rel), paths(rel)[0]
        if not rec or rec.get("state") != "synced" or not lrc.exists():
            print(f"{rel}: no synced lyrics", file=sys.stderr)
            continue
        checked += 1
        suspect = lyrics_check.reasons(lrc.read_text(), song_tags(m, rel)[3])
        if suspect:
            rec["suspect"] = suspect
            flagged += bool(lyrics_check.flagged(suspect))
            print(f"{', '.join(lyrics_check.flagged(suspect))}: {rel}")
        else:
            rec.pop("suspect", None)
        for reason in lyrics_check.flagged(suspect):
            counts[reason] = counts.get(reason, 0) + 1
    if not a.dry_run:
        save(index)
    print(f"checked {checked} synced, {flagged} flagged" + "".join(f", {n} {s}" for s, n in sorted(counts.items()))
          + (" (dry run, index.json unchanged)" if a.dry_run else ""))


def cmd_translate(a):
    from . import translation
    from .musicdb import mpd
    if bool(a.current) == bool(a.file):
        sys.exit("give FILE or --current")
    rel = current_file() if a.current else a.file
    artist, title, *_ = song_tags(mpd(), rel)
    try:
        rec = translation.translate(rel, artist, title, (load().get(rel) or {}).get("state"))
    except RuntimeError as e:
        sys.exit(f"translation: {e}")
    print(translation.describe(rec))


def cmd_lang(a):
    from . import translation
    rec = translation.set_lang(a.file, a.code)
    print(f"lyrics language: {rec['original_lang']}" + (" (set by hand)" if rec["original_lang_manual"] else ""))


def cmd_transcribe(a):
    from . import transcribe
    transcribe.cmd(a)


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb lyrics", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("fetch"); p.add_argument("files", nargs="*"); p.add_argument("--current", action="store_true")
    p.add_argument("--all", action="store_true"); p.add_argument("--recheck", action="store_true"); p.set_defaults(fn=cmd_fetch)
    p = sp.add_parser("candidates"); p.add_argument("file"); p.add_argument("--artist"); p.add_argument("--title")
    p.set_defaults(fn=cmd_candidates)
    p = sp.add_parser("use"); p.add_argument("file"); p.add_argument("id", type=int); p.set_defaults(fn=cmd_use)
    p = sp.add_parser("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = sp.add_parser("check"); p.add_argument("files", nargs="*"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_check)
    p = sp.add_parser("translate"); p.add_argument("file", nargs="?"); p.add_argument("--current", action="store_true")
    p.set_defaults(fn=cmd_translate)
    p = sp.add_parser("lang"); p.add_argument("file"); p.add_argument("code"); p.set_defaults(fn=cmd_lang)
    p = sp.add_parser("transcribe"); p.add_argument("files", nargs="*"); p.add_argument("--current", action="store_true")
    p.add_argument("--playlist"); p.add_argument("--force", action="store_true")
    p.add_argument("--install", action="store_true"); p.set_defaults(fn=cmd_transcribe)
    a = ap.parse_args(argv)
    a.fn(a)
