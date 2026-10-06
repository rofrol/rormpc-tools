"""Hand-made song lists (tags such as "God", "melancholic", "tearjerkers") and a manual genre layer.

  musicdb tag add NAME [--current | FILE ...] [--note TEXT]   # put songs on a list
  musicdb tag remove NAME [--current | FILE ...]
  musicdb tag list [NAME] [--json]          # every list with its size, or the songs of one list
  musicdb tag of [--current | FILE] [--json]   # the lists and manual genres of a song
  musicdb genre add|exclude|reset GENRE [--current | FILE ...]   # effective = (MusicBrainz + added) - excluded

Both are append-only logs in the data dir (collections.jsonl, genres.jsonl): one line per change, so the history
of decisions stays reviewable in git. A song is keyed by its YouTube id when the file name has one, else its
recording MBID, else its path, so a list survives renames and retagging; two copies of the same video are one
song, two different rips (or covers) are two. The music files are never changed. Each list is also written as an
MPD playlist "Tag NAME" (playing it is up to you; nothing is queued). Names are compared without case, so
"Melancholic" and "melancholic" are one list (spelled as first used).
"""
import argparse, collections, datetime as dt, fcntl, json, re, subprocess, sys

from . import identity, settings

LISTS = settings.DATA_DIR / "collections.jsonl"
GENRES = settings.DATA_DIR / "genres.jsonl"


def norm_name(name, known=()):
    """Trimmed name; a list that already exists under another case keeps its first spelling ("God")."""
    name = re.sub(r"\s+", " ", name.strip())
    return next((k for k in known if k.lower() == name.lower()), name)


def song_ref(m, rel):
    """Identity and description of a library file."""
    t = (m.find("file", rel) or [{}])[0]
    one = lambda k: (t.get(k)[0] if isinstance(t.get(k), list) else t.get(k)) or ""
    mbid = one("musicbrainz_trackid")
    return {"key": identity.key(rel, mbid), "file": rel, "ytid": identity.ytid(rel), "mbid": mbid or None,
            "artist": one("artist"), "title": one("title")}


def append(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # rormpc and a terminal may both write
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def events(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def lists():
    """Fold the log: {name: {key: last add event}}."""
    state = collections.defaultdict(dict)
    for e in events(LISTS):
        if e["action"] == "add":
            state[e["list"]][e["song"]["key"]] = e
        else:
            state[e["list"]].pop(e["song"]["key"], None)
    return {k: v for k, v in state.items() if v}


def manual_genres():
    """{song key: {"add": set, "exclude": set}} from genres.jsonl."""
    state = collections.defaultdict(lambda: {"add": set(), "exclude": set()})
    for e in events(GENRES):
        s, g = state[e["song"]["key"]], e["genre"]
        s["add"].discard(g); s["exclude"].discard(g)
        if e["action"] in ("add", "exclude"):
            s[e["action"]].add(g)
    return state


def key_of_file(rel, mbid=None):
    """Song key from what `hits` knows about a library row (no MPD round trip)."""
    return identity.key(rel or "", mbid)


def effective_genres(genres, rel, mbid=None, manual=None):
    """MusicBrainz genres with my additions and exclusions applied (used by hits and the genre explorer)."""
    s = (manual if manual is not None else manual_genres()).get(key_of_file(rel, mbid))
    if not s:
        return list(genres)
    return [g for g in genres if g not in s["exclude"]] + sorted(s["add"] - set(genres))


def files_arg(a):
    if a.current:
        f = subprocess.run(["mpc", "-f", "%file%", "current"], capture_output=True, text=True).stdout.strip()
        if not f:
            sys.exit("nothing is playing")
        return [f]
    if not a.files:
        sys.exit("give --current or files")
    return a.files


def write_playlists(state):
    """One MPD playlist per list, in the order songs were added; files that are gone are skipped."""
    from .musicdb import mpd
    m = mpd()
    known = {s["file"] for s in m.listallinfo() if s.get("file")}
    settings.MPD_PLAYLISTS.mkdir(parents=True, exist_ok=True)
    for old in settings.MPD_PLAYLISTS.glob("Tag *.m3u"):
        if old.stem[4:] not in state:
            old.unlink()
    from .musicdb import aliases
    al = aliases()
    for name, songs in state.items():
        files = [al.get(e["song"]["file"], e["song"]["file"]) for e in sorted(songs.values(), key=lambda e: e["ts"])]
        files = list(dict.fromkeys(f for f in files if f in known))  # two merged copies: listed once
        (settings.MPD_PLAYLISTS / f"Tag {name}.m3u").write_text("".join(f + "\n" for f in files))


def cmd_tag(a):
    from .musicdb import mpd
    if a.action in ("add", "remove"):
        m, name = mpd(), norm_name(a.name, lists())
        refs = [song_ref(m, f) for f in files_arg(a)]
        now = dt.datetime.now().isoformat(timespec="seconds")
        append(LISTS, [{"ts": now, "list": name, "action": a.action, "song": r} | ({"note": a.note} if a.note else {})
                       for r in refs])
        write_playlists(lists())
        for r in refs:
            print(f"{'tagged' if a.action == 'add' else 'untagged'} {name}: {r['artist']} - {r['title']} ({r['file']})")
        return
    state = lists()
    if a.action == "list":
        if a.name:
            songs = sorted(state.get(norm_name(a.name, state), {}).values(), key=lambda e: e["ts"])
            if a.json:
                print(json.dumps([e["song"] | {"ts": e["ts"], "note": e.get("note")} for e in songs], ensure_ascii=False)); return
            for e in songs:
                print(f"{e['ts'][:10]}  {e['song']['artist']} - {e['song']['title']}  {e.get('note', '')}")
            return
        if a.json:
            print(json.dumps({n: len(s) for n, s in sorted(state.items())})); return
        for n, s in sorted(state.items()):
            print(f"{len(s):4d}  {n}")
        return
    # of
    m = mpd()
    ref = song_ref(m, files_arg(a)[0])
    mine = sorted(n for n, s in state.items() if ref["key"] in s)
    g = manual_genres().get(ref["key"], {"add": set(), "exclude": set()})
    out = {"song": ref, "lists": mine, "all_lists": sorted(state), "genres_added": sorted(g["add"]),
           "genres_excluded": sorted(g["exclude"])}
    if a.json:
        print(json.dumps(out, ensure_ascii=False)); return
    changes = [f"+{g}" for g in out["genres_added"]] + [f"-{g}" for g in out["genres_excluded"]]
    print(f"{ref['artist']} - {ref['title']}\n  lists: {', '.join(mine) or 'none'}\n  genre changes: {' '.join(changes) or 'none'}")


def cmd_genre(a):
    from .musicdb import mpd
    m, genre = mpd(), norm_name(a.genre).lower()  # MusicBrainz genres are lower case
    refs = [song_ref(m, f) for f in files_arg(a)]
    now = dt.datetime.now().isoformat(timespec="seconds")
    append(GENRES, [{"ts": now, "genre": genre, "action": a.action, "song": r} for r in refs])
    verb = {"add": "added", "exclude": "excluded", "reset": "back to MusicBrainz for"}[a.action]
    for r in refs:
        print(f"genre {genre} {verb}: {r['artist']} - {r['title']}")


def song_args(p):
    p.add_argument("files", nargs="*"); p.add_argument("--current", action="store_true")


def main_tag(argv):
    ap = argparse.ArgumentParser(prog="musicdb tag", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="action", required=True)
    for action in ("add", "remove"):
        p = sp.add_parser(action); p.add_argument("name"); song_args(p); p.add_argument("--note")
    p = sp.add_parser("list"); p.add_argument("name", nargs="?"); p.add_argument("--json", action="store_true")
    p = sp.add_parser("of"); song_args(p); p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    a.note = getattr(a, "note", None)
    cmd_tag(a)


def main_genre(argv):
    ap = argparse.ArgumentParser(prog="musicdb genre", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["add", "exclude", "reset"]); ap.add_argument("genre"); song_args(ap)
    cmd_genre(ap.parse_args(argv))
