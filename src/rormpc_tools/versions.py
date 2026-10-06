"""musicdb versions: decide which library file a played track is, when several files share its name.

  musicdb versions [--json] [--all]          # groups that need a decision (--all: every group)
  musicdb versions set SOURCE TRACK FILE     # plays of this track are this file
  musicdb versions none SOURCE TRACK         # plays of this track are a version I don't own (download list)
  musicdb versions clear SOURCE TRACK        # forget the decision (undecided again)
  musicdb versions label FILE VERSION        # original | live | remix | edit | cover | other | clear
  musicdb versions same KEEP OTHER...        # one recording: keep KEEP, merge the others into it (quarantine)
  musicdb versions shared-ok ID              # an MBID / YouTube id on several files is right (e.g. album + video)

A play matched only by artist + title to a name shared by several files is credited to no file. Here it is
decided once per source track: a Spotify track URI, a ListenBrainz recording MBID, or, for plays that carry no
id, the name itself ("name:<artist>|<title>"). Later plays of the same track follow the decision. Decisions are
an append-only log, versions.jsonl in data_dir, keyed by track and by the file's identity (YouTube id / MBID,
through aliases.jsonl), never by the normalised name alone. A decision records the group's files when it was
made: when the group changes (a download, a deletion) it comes back for review (`musicdb doctor`).
"""
import argparse, collections, datetime as dt, json, re, sys

from . import musicdb

VERSIONS = ("original", "live", "remix", "edit", "cover", "other")
MARKERS = {"live": r"\blive\b|\bunplugged\b|\bacoustic\b", "remix": r"\bremix\b|\brmx\b|\bbootleg\b",
           "edit": r"\bedit\b|\bextended\b", "cover": r"\bcover\b"}


def log_path():
    return musicdb.DATA / "versions.jsonl"


def fold():
    """{"tracks": {(source, track): event}, "labels": {song key: event}, "shared": {id: event}}."""
    st = {"tracks": {}, "labels": {}, "shared": {}}
    for e in musicdb.jsonl(log_path()):
        a = e["action"]
        if a in ("set", "none"):
            st["tracks"][(e["source"], e["track"])] = e
        elif a == "clear":
            st["tracks"].pop((e["source"], e["track"]), None)
        elif a == "label":
            if e["version"] == "clear":
                st["labels"].pop(e["key"], None)
            else:
                st["labels"][e["key"]] = e
        elif a == "shared-ok":
            st["shared"][e["id"]] = e
    return st


def track_of(src, uri, mbid, artist, title):
    """The id a decision is remembered by, for an event's source and fields."""
    if src == "spotify" and uri:
        return uri
    if mbid:
        return f"mb:{mbid}"
    return "name:" + musicdb.name_key(artist, title)


def key_file(lib, files, key, file, al):
    """The library file a decision names: its path (through aliases), else its YouTube id / MBID."""
    f = al.get(file, file)
    if f in files:
        return f
    kind, _, val = (key or "").partition(":")
    return {"yt": lib[0], "mb": lib[1]}.get(kind, {}).get(val)


NOT_OWNED = "\0not-owned"


def resolver(lib, files):
    """function(src, uri, mbid, artist, title) -> file | NOT_OWNED | None (no decision or a stale one)."""
    st, al = fold(), musicdb.aliases()

    def resolve(src, uri, mbid, artist, title):
        e = st["tracks"].get((src, track_of(src, uri, mbid, artist, title)))
        if not e:
            return None
        if e["action"] == "none":
            return NOT_OWNED
        return key_file(lib, files, e.get("key"), e.get("file"), al)
    return resolve


def song_info(m):
    return {s["file"]: s for s in m.listallinfo() if s.get("file")}


def markers(text):
    t = (text or "").lower().replace("_", " ")  # file names use _ for spaces
    return [k for k, rx in MARKERS.items() if re.search(rx, t)]


def spotify_albums():
    """{track URI: album} from the Spotify export(s) in data_dir/raw, which keep the album the history lacks."""
    out = {}
    raw = musicdb.DATA / "raw"
    if not raw.exists():
        return out
    for _, data in musicdb.open_export(raw, r"Streaming_History_Audio\S*\.json$"):
        for e in json.loads(data):
            if e.get("spotify_track_uri") and e.get("master_metadata_album_album_name"):
                out[e["spotify_track_uri"]] = e["master_metadata_album_album_name"]
    return out


def groups(include_all=False):
    """Name groups with several library files: their files, the tracks played under that name that only the
    name matches, each track's decision and a suggestion with its reason. Pending items say what is open."""
    m, c = musicdb.mpd(), musicdb.db()
    lib = musicdb.library()
    files = musicdb.library_files(lib)
    info, st, al = song_info(m), fold(), musicdb.aliases()
    musicdb.ALIASES_CACHE.clear(); musicdb.ALIASES_CACHE.update(al)
    multi = {k: sorted(v) for k, v in lib[2].items() if len(v) > 1}
    plays, *_ = musicdb.counted(c, lib)
    albums = spotify_albums()
    tracks = collections.defaultdict(dict)
    for src, ts, ytid, mbid, uri, artist, title, ms, extra in c.execute(
            "SELECT source, ts, ytid, mbid, spotify_uri, artist, title, ms_played, extra FROM events"):
        if not (artist and title):
            continue
        k = musicdb.name_key(artist, title)
        if k not in multi:
            continue
        if src == "local" or (ytid and ytid in lib[0]) or (mbid and mbid in lib[1]):
            continue  # an exact match: no decision needed
        tid = track_of(src, uri, mbid, artist, title)
        t = tracks[k].setdefault((src, tid), {"source": src, "track": tid, "artist": artist, "title": title,
                                              "album": albums.get(uri), "plays": 0, "first": ts, "last": ts,
                                              "longest_s": 0})
        t["plays"] += 1
        t["first"], t["last"] = min(t["first"], ts), max(t["last"], ts)
        if ms not in ("", None):
            t["longest_s"] = max(t["longest_s"], round(int(ms) / 1000))
    out = []
    for k, fs in sorted(multi.items()):
        rows = []
        for f in fs:
            s = info.get(f, {})
            ref = {"key": musicdb_key(f, s), "file": f}
            lab = st["labels"].get(ref["key"])
            rows.append({**ref, "artist": musicdb.one(s.get("artist", "")), "title": musicdb.one(s.get("title", "")),
                         "duration_s": round(float(musicdb.one(s.get("duration", 0)) or 0)),
                         "mbid": musicdb.one(s.get("musicbrainz_trackid", "")) or None,
                         "version": lab["version"] if lab else None,
                         "markers": markers(f + " " + musicdb.one(s.get("title", ""))), "plays": plays.get(f, 0)})
        ts_ = []
        for t in sorted(tracks[k].values(), key=lambda t: -t["plays"]):
            e = st["tracks"].get((t["source"], t["track"]))
            dec = None
            if e:
                target = None if e["action"] == "none" else key_file(lib, files, e.get("key"), e.get("file"), al)
                stale = e["action"] == "set" and not target
                changed = sorted(e.get("group", [])) != sorted(canon_all(fs, al)) and bool(e.get("group"))
                dec = {"action": e["action"], "file": target, "at": e["ts"], "stale": stale, "group_changed": changed}
            ts_.append({**t, "decision": dec, "suggest": suggest(t, rows)})
        pending = [f"track {t['source']} {t['track']}" for t in ts_
                   if not t["decision"] or t["decision"]["stale"] or t["decision"]["group_changed"]]
        pending += [f"label {r['file']}" for r in rows if not r["version"]]
        if pending or include_all:
            out.append({"name": k, "files": rows, "tracks": ts_, "pending": pending})
    return out


def shared():
    """YouTube ids / MBIDs on several library files not yet reviewed (doctor's shared-ids) with the files'
    lengths: identical audio belongs to `musicdb dedupe`, one recording to `same`, a reviewed case to shared-ok,
    a wrong tag to a tag fix."""
    from . import doctor
    info = song_info(musicdb.mpd())
    return [{"id": d["id"], "files": [{"file": f, "duration_s": round(float(musicdb.one(info.get(f, {}).get("duration", 0)) or 0))}
                                       for f in d["files"]]} for d in doctor.check()["shared-ids"]]


def canon_all(fs, al):
    return [al.get(f, f) for f in fs]


def musicdb_key(f, s):
    from . import identity
    return identity.key(f, musicdb.one(s.get("musicbrainz_trackid", "")) or None)


LEN_SLACK_S = 8  # a file a few seconds longer or shorter than the longest play (silence, fades) still fits


def fits_length(t, r):
    """Can the plays be of this file? No play is longer than the track; and with several plays, if even the
    longest stopped before 60 % of the file, they were most likely of a shorter version."""
    if t["longest_s"] < 60:
        return True  # no length information
    if t["longest_s"] > r["duration_s"] + LEN_SLACK_S:
        return False
    return not (t["plays"] >= 3 and t["longest_s"] < 0.6 * r["duration_s"])


def suggest(t, rows):
    """A candidate with its reason, only when the markers and the play lengths point at the same single file;
    never applied by itself. Conflicting evidence (Tiësto: no "live" in the title, but 26 plays never longer
    than 3:27, while the unmarked file is 7:23) gives no suggestion."""
    want = markers(f"{t['title']} {t.get('album') or ''}")
    if want:
        hits = [r for r in rows if set(want) & set(r["markers"] + ([r["version"]] if r["version"] else []))]
    else:
        hits = [r for r in rows if not r["markers"] and r["version"] in (None, "original")]
    fit = [r for r in rows if fits_length(t, r)]
    both = [r for r in hits if r in fit]
    if len(both) != 1 or (t["longest_s"] >= 60 and len(fit) > 1 and len(hits) > 1):
        return None
    r = both[0]
    reason = f"title says {', '.join(want)}" if want else "no live/remix/edit marker in the title"
    if t["longest_s"] >= 60:
        reason += f"; longest play {t['longest_s']} s, file {r['duration_s']} s"
        others = [x for x in rows if x is not r and not fits_length(t, x)]
        if others:
            reason += "; " + ", ".join(f"{x['duration_s']} s does not fit" for x in others)
    return {"file": r["file"], "reason": reason}


def log(rows):
    from . import tags
    tags.append(log_path(), rows)


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def decide(a):
    lib = musicdb.library()
    files = musicdb.library_files(lib)
    group = None
    for g in groups(include_all=True):
        if any(t["source"] == a.source and t["track"] == a.track for t in g["tracks"]):
            group = [r["file"] for r in g["files"]]
    e = {"ts": now(), "action": a.cmd, "source": a.source, "track": a.track, "group": group}
    if a.cmd == "set":
        if a.file not in files:
            sys.exit(f"not a library file: {a.file}")
        if group is not None and a.file not in group:
            sys.exit(f"{a.file} is not one of this track's candidates: {', '.join(group)}")
        e |= {"file": a.file, "key": musicdb_key(a.file, (musicdb.mpd().find("file", a.file) or [{}])[0])}
    log([e])
    print(f"{a.cmd} {a.source} {a.track}" + (f" -> {a.file}" if a.cmd == "set" else ""))


def label(a):
    if a.version not in VERSIONS + ("clear",):
        sys.exit(f"version: one of {', '.join(VERSIONS)} or clear")
    s = (musicdb.mpd().find("file", a.file) or [None])[0]
    if not s:
        sys.exit(f"not a library file: {a.file}")
    log([{"ts": now(), "action": "label", "file": a.file, "key": musicdb_key(a.file, s), "version": a.version}])
    print(f"{a.file}: {a.version}")


def same(a):
    """The files are one recording: keep KEEP, merge the others into it (like, missing tags, lyrics), record
    aliases and move the others to the quarantine, like `musicdb dedupe` does for identical audio."""
    from . import dedupe
    m = musicdb.mpd()
    lib_files = {s["file"] for s in m.listallinfo() if s.get("file")}
    missing = [f for f in [a.keep, *a.others] if f not in lib_files]
    if missing or not a.others:
        sys.exit(f"not library files: {', '.join(missing)}" if missing else "name the files to merge into KEEP")
    g = dedupe.group_for([a.keep, *a.others], m, keep=a.keep, why="same recording")
    if g["like_conflict"]:
        sys.exit("the files have different likes: decide first (rmpc key r)")
    dedupe.apply([g], m)


def shared_ok(a):
    log([{"ts": now(), "action": "shared-ok", "id": a.id}])
    print(f"{a.id}: several files are fine")


def show(gs):
    for g in gs:
        print(f"\n{g['name']}  ({len(g['pending'])} open)")
        for r in g["files"]:
            print(f"   {r['duration_s']:4d}s  {r['version'] or '?':8}  {r['plays']:3d} plays  {r['file']}")
        for t in g["tracks"]:
            d = t["decision"]
            state = ("-> " + (d["file"] or "?stale") if d and d["action"] == "set" else "not owned" if d else "OPEN")
            if d and (d["stale"] or d["group_changed"]):
                state += "  (review: " + ("file gone" if d["stale"] else "group changed") + ")"
            sug = f"   suggest {t['suggest']['file']} ({t['suggest']['reason']})" if t["suggest"] and not d else ""
            print(f"   [{t['source']}] {t['track']}  {t['plays']} plays  {t['album'] or ''}  {state}{sug}")
    print(f"\n{len(gs)} groups, {sum(len(g['pending']) for g in gs)} open items")


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb versions", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd")
    for verb in ("set", "none", "clear"):
        p = sp.add_parser(verb); p.add_argument("source"); p.add_argument("track")
        if verb == "set":
            p.add_argument("file")
        p.set_defaults(fn=decide)
    p = sp.add_parser("label"); p.add_argument("file"); p.add_argument("version"); p.set_defaults(fn=label)
    p = sp.add_parser("same"); p.add_argument("keep"); p.add_argument("others", nargs="+"); p.set_defaults(fn=same)
    p = sp.add_parser("shared-ok"); p.add_argument("id"); p.set_defaults(fn=shared_ok)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="also groups with nothing open")
    a = ap.parse_args(argv)
    if a.cmd:
        return a.fn(a)
    gs = groups(a.all)
    if a.json:
        print(json.dumps({"version": 1, "music_dir": str(musicdb.MUSIC), "groups": gs, "shared": shared()},
                         ensure_ascii=False, indent=1))
    else:
        show(gs)
