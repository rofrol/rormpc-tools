"""musicdb identity: one id per library file, independent of its name and folder.

  musicdb identity sync [--dry-run]   # register new files, follow renamed ones, mark gone ones (hourly in update)
  musicdb identity show FILE          # the file's registry row

The registry is songs.jsonl in data_dir: one row per library file ever seen,
{"id", "path" (current, null when gone), "paths" (every path it had), "state" live|gone|merged, "into" (merged),
"ytid", "mbid", "md5"}. The id is a UUID of the FILE (an asset), not of a song or recording: two files of one
recording are two ids; MBID and YouTube id are attributes, never keys (several files may share them).

The id and the YouTube id are also written into the file's tags (MP3: TXXX "rormpc Song ID" / "YouTube ID";
FLAC: RORMPC_SONG_ID / YOUTUBE_ID). MPD does not show custom tags, so the tools read the registry, and the tags
are what lets `sync` recognise a file that was renamed or moved by anything (Finder, a migration) as the same id.

The YouTube id of a file name ("…--<id>--YYYYMMDD.mp3", or "… [<id>].ext") is parsed here only: every tool
asks ytid(path), which answers from the registry (current and old paths) and falls back to the name.
"""
import argparse, contextlib, fcntl, json, re, sys, uuid

from . import musicdb, settings

YTID_RE = r"[A-Za-z0-9_-]{11}"
NAME_PATTERNS = (re.compile(rf"--({YTID_RE})--\d{{8}}\.mp3$", re.I), re.compile(rf"\[({YTID_RE})\]\.\w+$"))
TAG_ID, TAG_YT = "rormpc Song ID", "YouTube ID"  # TXXX descriptions; FLAC: RORMPC_SONG_ID, YOUTUBE_ID
TAG_CACHE = settings.XDG_CACHE / "rormpc-tools" / "identity-tags.json"


def registry_path():
    return musicdb.DATA / "songs.jsonl"


def ytid_from_name(path):
    name = (path or "").rsplit("/", 1)[-1]
    for rx in NAME_PATTERNS:
        if m := rx.search(name):
            return m.group(1)
    return None


# ------------------------------------------------------------------ registry

_cache = {}


def load(fresh=False):
    """{"rows": {id: row}, "by_path": {any path ever: id}} (cached per process; sync refreshes it)."""
    if fresh or "rows" not in _cache:
        rows = {r["id"]: r for r in musicdb.jsonl(registry_path())}
        by_path = {}
        for r in rows.values():
            for p in r.get("paths", []):
                by_path.setdefault(p, r["id"])
            if r.get("path"):
                by_path[r["path"]] = r["id"]  # the current owner of a path wins over history
        _cache.update(rows=rows, by_path=by_path)
    return _cache


def save(rows):
    musicdb.write_jsonl(registry_path(), [rows[k] for k in sorted(rows)])
    _cache.clear()


@contextlib.contextmanager
def locked():
    lock = musicdb.DB.parent / "identity.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def resolve(path):
    """The registry row of a path (current or old; merged rows lead to their survivor), else None."""
    reg = load()
    rid = reg["by_path"].get(path)
    seen = set()
    while rid and reg["rows"][rid].get("state") == "merged" and rid not in seen:
        seen.add(rid)
        rid = reg["rows"][rid].get("into")
    return reg["rows"].get(rid) if rid else None


def ytid(path):
    """The YouTube id of a file (any path it ever had), from the registry, else parsed from the name."""
    r = resolve(path)
    return (r or {}).get("ytid") or ytid_from_name(path)


def key(path, mbid=None):
    """Song key used by hand-made lists and manual genres: yt:<id>, else mb:<mbid>, else file:<path>."""
    y = ytid(path)
    return f"yt:{y}" if y else f"mb:{mbid}" if mbid else f"file:{path}"


# ------------------------------------------------------------------ tags

def read_tags(rel):
    """(song id, YouTube id) stored in the file's tags."""
    import mutagen
    f = mutagen.File(musicdb.MUSIC / rel)
    t = f.tags if f is not None else None
    if t is None:
        return None, None
    if hasattr(t, "getall"):  # ID3
        g = lambda d: (str(t.get(f"TXXX:{d}").text[0]) if t.get(f"TXXX:{d}") else None)
        return g(TAG_ID), g(TAG_YT)
    g = lambda k: (t.get(k) or [None])[0]
    return g("rormpc_song_id"), g("youtube_id")


def cached_tags(rels):
    """read_tags for many files, cached by size and mtime (reading 800 files' tags every hour is wasted work)."""
    try:
        cache = json.loads(TAG_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    out = {}
    for rel in rels:
        try:
            st = (musicdb.MUSIC / rel).stat()
        except OSError:
            continue
        c = cache.get(rel)
        if not c or c["size"] != st.st_size or c["mtime"] != st.st_mtime:
            try:
                sid, yt = read_tags(rel)
            except Exception:  # unreadable tags: treated as untagged, reported by doctor
                sid, yt = None, None
            c = cache[rel] = {"size": st.st_size, "mtime": st.st_mtime, "id": sid, "ytid": yt}
        out[rel] = (c["id"], c["ytid"])
    TAG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TAG_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(TAG_CACHE)
    return out


def write_tags(rel, sid, yt):
    """Add the song id (and YouTube id) to the file's tags; other tags are left as they are."""
    import mutagen
    from mutagen.id3 import TXXX
    f = mutagen.File(musicdb.MUSIC / rel)
    if f is None:
        raise ValueError(f"not an audio file mutagen can tag: {rel}")
    if f.tags is None:
        f.add_tags()
    if hasattr(f.tags, "getall"):
        f.tags.delall(f"TXXX:{TAG_ID}")
        f.tags.add(TXXX(encoding=3, desc=TAG_ID, text=[sid]))
        if yt:
            f.tags.delall(f"TXXX:{TAG_YT}")
            f.tags.add(TXXX(encoding=3, desc=TAG_YT, text=[yt]))
    else:
        f.tags["rormpc_song_id"] = [sid]
        if yt:
            f.tags["youtube_id"] = [yt]
    f.save()


# ------------------------------------------------------------------ sync

def sync(m=None, dry_run=False, write=True):
    """Bring the registry in line with MPD's library. Returns {"new", "renamed", "gone", "tagged", "conflicts"}.
    A file with a known id in its tags at a new path is that id renamed (or moved); a known id on two live files
    is a copy and is reported, never resolved by guessing; a file without an id gets a new one (written to its
    tags unless write=False); a live row whose file is gone becomes "gone", or "merged" into the survivor
    when aliases.jsonl says so (dedupe, same recording)."""
    from . import dedupe
    m = m or musicdb.mpd()
    songs = {s["file"]: s for s in m.listallinfo() if s.get("file")}
    with locked():
        reg = load(fresh=True)
        rows = {k: dict(v) for k, v in reg["rows"].items()}
        tags = cached_tags(sorted(songs))
        report = {"new": [], "renamed": [], "gone": [], "tagged": [], "conflicts": []}
        seen = {}
        hashes = None
        for rel, s in sorted(songs.items()):
            sid, tag_yt = tags.get(rel, (None, None))
            name_yt = ytid_from_name(rel)
            mbid = musicdb.one(s.get("musicbrainz_trackid", "")) or None
            if sid and sid in seen:
                report["conflicts"].append({"id": sid, "files": [seen[sid], rel], "why": "two files carry one id (a copy)"})
                continue
            if not sid and (rid := reg["by_path"].get(rel)) and rows[rid].get("path") == rel:
                sid = rid  # registered, tags not written yet
            if sid and sid in rows:
                r = rows[sid]
                if r.get("path") != rel:
                    report["renamed"].append({"id": sid, "from": r.get("path"), "to": rel})
                    if r.get("path") and not dry_run:
                        move_lyrics(r["path"], rel)
                    r["path"], r["state"] = rel, "live"
                    r.pop("into", None)
                if rel not in r["paths"]:
                    r["paths"].append(rel)
            else:
                if hashes is None:
                    hashes = dedupe.hashes(sorted(songs)) if not dry_run else {}
                sid = sid or str(uuid.uuid4())
                rows[sid] = {"id": sid, "path": rel, "paths": [rel], "state": "live"}
                report["new"].append(rel)
                r = rows[sid]
                if hashes:
                    r["md5"] = hashes.get(rel)
            seen[sid] = rel
            yt = tag_yt or name_yt or r.get("ytid")
            if name_yt and tag_yt and name_yt != tag_yt:
                report["conflicts"].append({"id": sid, "files": [rel], "why": f"name says {name_yt}, tag says {tag_yt}"})
            if yt:
                r["ytid"] = yt
            if mbid:
                r["mbid"] = mbid
            if write and not dry_run and (tags.get(rel, (None,))[0] != sid or (r.get("ytid") and tag_yt != r.get("ytid"))):
                try:
                    write_tags(rel, sid, r.get("ytid"))
                    report["tagged"].append(rel)
                except Exception as e:
                    report["conflicts"].append({"id": sid, "files": [rel], "why": f"tags not written: {e}"})
        al = musicdb.aliases()
        by_path_now = {r["path"]: k for k, r in rows.items() if r.get("path")}
        for k, r in rows.items():
            if r.get("state") == "live" and r.get("path") not in songs and k not in seen:
                into = by_path_now.get(al.get(r["path"]))
                r["state"], r["path"] = ("merged", None) if into else ("gone", None)
                if into:
                    r["into"] = into
                report["gone"].append(r["paths"][-1])
        if not dry_run:
            save(rows)
    return report


def move_lyrics(old, new):
    """rormpc finds lyrics by the song's path (lyrics_dir/<path>.lrc|.txt): they follow a renamed file."""
    from . import lyrics
    moved = False
    for src, dst in zip(lyrics.paths(old), lyrics.paths(new)):
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            moved = True
    index = lyrics.load()
    if old in index and new not in index:
        index[new] = index.pop(old)
        lyrics.save(index)
    elif moved:
        lyrics.save(index)


def summary(rep):
    return ("identity: " + ", ".join(f"{len(v)} {k}" for k, v in rep.items() if v)) if any(rep.values()) else "identity: ok"


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb identity", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("sync"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-tags", action="store_true", help="register only, write no tags")
    p = sp.add_parser("show"); p.add_argument("file")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        print(json.dumps(resolve(a.file), ensure_ascii=False, indent=1))
        return
    rep = sync(dry_run=a.dry_run, write=not a.no_tags)
    print(summary(rep))
    for c in rep["conflicts"]:
        print("  conflict:", json.dumps(c, ensure_ascii=False))
    if rep["tagged"] and not a.dry_run:
        subprocess_update()


def subprocess_update():
    """Tags were written: MPD rescans the changed files (their mtime moved)."""
    import subprocess
    subprocess.run(["mpc", "-q", "update", "--wait"], check=False)
    sys.stdout.flush()
