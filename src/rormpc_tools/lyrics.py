"""Lyrics from LRCLIB for library songs, kept as files where rmpc's Lyrics pane looks for them.

  musicdb lyrics fetch --current        # the playing song
  musicdb lyrics fetch FILE ...         # paths relative to the music dir
  musicdb lyrics fetch --all            # every song not checked yet (--recheck: also songs that had none)
  musicdb lyrics candidates FILE        # what LRCLIB has for a song, as JSON (rormpc's "Choose lyrics…")
  musicdb lyrics use FILE ID            # take this LRCLIB entry for the song
  musicdb lyrics status [--json]        # counts, or every song's state

Files: <lyrics_dir>/<song path>.lrc for synced lyrics (rmpc: lyrics_dir in its config, same directory),
<song path>.txt for plain lyrics without timestamps. <lyrics_dir>/index.json records per song: synced, plain,
instrumental, none (LRCLIB has nothing within 2 s of the file's length) or untagged, with the LRCLIB id and when it
was checked, so a miss is not asked again until --recheck. LRCLIB matches on duration (about 2 s), so YouTube rips
with intros or outros often miss; `candidates` + `use` pick another entry by hand.
"""
import argparse, datetime as dt, json, subprocess, sys, urllib.parse

from . import mbtag, settings

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


def write(rel, entry, artist, title, how):
    """Store one LRCLIB entry for a song; returns the index record."""
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
        return rec | {"state": "synced"}
    if entry.get("plainLyrics"):
        txt.write_text(entry["plainLyrics"].rstrip() + "\n")
        return rec | {"state": "plain"}
    return rec | {"state": "none"}


def fetch_one(m, rel, index):
    artist, title, _album, duration = song_tags(m, rel)
    if not (artist and title):
        index[rel] = {"state": "untagged", "checked_at": dt.datetime.now().isoformat(timespec="seconds")}
        return index[rel]
    index[rel] = write(rel, best(search(artist, title), duration), artist, title, "auto")
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
            "instrumental": bool(c.get("instrumental"))} for c in cands]
    out.sort(key=lambda c: (abs(c["delta"]), not c["synced"]))
    print(json.dumps({"file": a.file, "duration": duration, "current": load().get(a.file), "candidates": out},
                     ensure_ascii=False))


def cmd_use(a):
    from .musicdb import mpd
    entry = mbtag.http(f"{API}/get/{a.id}", host_interval=0.5, attempts=3, timeout=20)
    if not entry:
        sys.exit(f"LRCLIB has no entry {a.id}")
    artist, title, *_ = song_tags(mpd(), a.file)
    index = load()
    index[a.file] = write(a.file, entry, artist, title, "chosen")
    save(index)
    print(f"lyrics: {index[a.file]['state']} from LRCLIB #{a.id}")


def cmd_status(a):
    index = load()
    if a.json:
        print(json.dumps(index, ensure_ascii=False)); return
    counts = {}
    for rec in index.values():
        counts[rec["state"]] = counts.get(rec["state"], 0) + 1
    print(f"{LYRICS}: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb lyrics", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("fetch"); p.add_argument("files", nargs="*"); p.add_argument("--current", action="store_true")
    p.add_argument("--all", action="store_true"); p.add_argument("--recheck", action="store_true"); p.set_defaults(fn=cmd_fetch)
    p = sp.add_parser("candidates"); p.add_argument("file"); p.add_argument("--artist"); p.add_argument("--title")
    p.set_defaults(fn=cmd_candidates)
    p = sp.add_parser("use"); p.add_argument("file"); p.add_argument("id", type=int); p.set_defaults(fn=cmd_use)
    p = sp.add_parser("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    a = ap.parse_args(argv)
    a.fn(a)
