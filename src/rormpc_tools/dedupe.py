"""musicdb dedupe: one library file per identical audio stream.

  musicdb dedupe [--json]     # preview: groups of copies, which one stays, what moves to it (changes nothing)
  musicdb dedupe --apply      # do it

Copies are found by an MD5 of the audio stream as stored (ffmpeg -c copy), so tags, covers and ReplayGain do not
make two copies differ, and a re-encode never counts as a copy. Same name with different audio is not handled
here: those are versions to decide by hand.

Applying, per group: the survivor is the copy with a like, then a hand-made list, then the most tags. It gets
the tags it lacks from the others (e.g. a cover or MusicBrainz album id), their like if it has none, and their
lyrics if it has none. Each other copy is recorded in aliases.jsonl (old path -> survivor; musicdb reads plays,
skips, keep decisions and lists through it), replaced by the survivor in every MPD playlist, and moved to a
quarantine folder outside the library (not the Trash), from where it can be put back by hand. Play counts are
not merged: `musicdb sync` recounts them from the history, which now reaches the survivor.

Before the first change, each top-level folder that loses a copy gets an MPD playlist "Folder <name>" with its
files in name order, so membership and the NNN order of the folders survive.
"""
import argparse, collections, concurrent.futures, datetime as dt, json, os, subprocess, sys

from . import musicdb, settings

QUARANTINE = settings.XDG_DATA / "rormpc-tools" / "quarantine"
CACHE = settings.XDG_CACHE / "rormpc-tools" / "audio-hash.json"
GENERATED = ("Hits ", "LB ", "Tag ", "Skipped", "Not finished")  # playlists the tools rewrite themselves


def audio_hash(path):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-c", "copy", "-f", "md5", "-"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = p.stdout.strip()
    return out.removeprefix("MD5=") if p.returncode == 0 and out.startswith("MD5=") else None


def hashes(files):
    """{rel: audio md5}, cached by size and mtime (a tag write changes the mtime but not the hash: recomputed)."""
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    out, todo = {}, []
    for rel in files:
        st = (musicdb.MUSIC / rel).stat()
        c = cache.get(rel)
        if c and c["size"] == st.st_size and c["mtime"] == st.st_mtime:
            out[rel] = c["md5"]
        else:
            todo.append((rel, st))
    with concurrent.futures.ThreadPoolExecutor(os.cpu_count() or 4) as ex:
        for (rel, st), h in zip(todo, ex.map(lambda t: audio_hash(musicdb.MUSIC / t[0]), todo)):
            if h:
                out[rel] = h
                cache[rel] = {"md5": h, "size": st.st_size, "mtime": st.st_mtime}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(CACHE)
    return out


def tag_map(path):
    import mutagen
    f = mutagen.File(path)
    return f, (f.tags if f is not None and f.tags is not None else {})


def plan(m=None):
    """[{hash, keep, drop: [...], like, tags: {drop: [keys]}, lyrics}] for every group of identical audio."""
    from . import lyrics, tags
    m = m or musicdb.mpd()
    files = sorted(s["file"] for s in m.listallinfo() if s.get("file") and s["file"].lower().endswith((".mp3", ".flac")))
    groups = collections.defaultdict(list)
    for rel, h in hashes(files).items():
        groups[h].append(rel)
    listed = {e["song"]["file"] for songs in tags.lists().values() for e in songs.values()}
    index = lyrics.load()
    out = []
    for h, fs in sorted(groups.items(), key=lambda kv: sorted(kv[1])):
        if len(fs) < 2:
            continue
        info = {}
        for f in fs:
            try:
                st = m.sticker_list("song", f)
            except Exception:
                st = {}
            info[f] = {"like": st.get("like"), "listed": f in listed, "ntags": len(tag_map(musicdb.MUSIC / f)[1]),
                       "lyrics": index.get(f, {}).get("state") in ("synced", "plain")}
        keep = min(fs, key=lambda f: (info[f]["like"] is None, not info[f]["listed"], -info[f]["ntags"], f))
        drop = sorted(f for f in fs if f != keep)
        likes = {info[f]["like"] for f in fs if info[f]["like"] is not None}
        keys = set(tag_map(musicdb.MUSIC / keep)[1].keys())
        out.append({"hash": h, "keep": keep, "drop": drop,
                    "like": info[keep]["like"] if info[keep]["like"] is not None else next(iter(likes), None),
                    "like_conflict": len(likes) > 1,
                    "tags": {f: sorted(set(tag_map(musicdb.MUSIC / f)[1].keys()) - keys) for f in drop},
                    "lyrics": None if info[keep]["lyrics"] else next((f for f in drop if info[f]["lyrics"]), None)})
    return out


def merge_tags(keep, drop):
    """Copy the frames/fields the survivor lacks from a dropped copy; never overwrite one it has."""
    kf, kt = tag_map(musicdb.MUSIC / keep)
    _, dt_ = tag_map(musicdb.MUSIC / drop)
    missing = [k for k in dt_.keys() if k not in kt]
    if not missing:
        return []
    if kf.tags is None:
        kf.add_tags()
    for k in missing:
        if hasattr(kf.tags, "add") and hasattr(dt_[k], "HashKey"):  # ID3 frame
            kf.tags.add(dt_[k])
        else:  # Vorbis comment
            kf.tags[k] = dt_[k]
    if hasattr(kf, "pictures") and not kf.pictures:  # FLAC pictures are not tags
        src = tag_map(musicdb.MUSIC / drop)[0]
        for pic in getattr(src, "pictures", []):
            kf.add_picture(pic)
    kf.save()
    return missing


def folder_playlists(groups, m):
    """'Folder <name>' playlists for the top-level folders that lose a copy, created once."""
    tops = {f.split("/", 1)[0] for g in groups for f in g["drop"] if "/" in f}
    files = sorted(s["file"] for s in m.listallinfo() if s.get("file"))
    made = []
    for top in sorted(tops):
        pl = musicdb.PLAYLISTS / f"Folder {top}.m3u"
        if not pl.exists():
            musicdb.write_playlist(f"Folder {top}", [f for f in files if f.startswith(top + "/")])
            made.append(pl.stem)
    return made


def rewrite_playlists(al):
    """Replace merged copies by their survivor in every playlist the tools do not regenerate."""
    n = 0
    for pl in sorted(musicdb.PLAYLISTS.glob("*.m3u")):
        lines = pl.read_text(errors="surrogateescape").splitlines()
        new = [al.get(l, l) for l in lines]
        if new != lines:
            tmp = pl.with_name("." + pl.name + ".tmp")
            tmp.write_text("".join(l + "\n" for l in new), errors="surrogateescape")
            tmp.replace(pl)  # atomic: MPD never reads a half-written playlist
            n += 1
    return n


def mpd_update():
    subprocess.run(["mpc", "-q", "update", "--wait"], check=True)


def apply(groups, m=None):
    from . import lyrics, tags
    m = m or musicdb.mpd()
    if not groups:
        print("no copies"); return
    made = folder_playlists(groups, m)
    day = dt.date.today().isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")
    index = lyrics.load()
    for g in groups:
        keep = g["keep"]
        for d in g["drop"]:
            g.setdefault("merged_tags", {})[d] = merge_tags(keep, d)
        if g["like"] is not None:
            m.sticker_set("song", keep, "like", g["like"])
        if g["lyrics"]:
            for src, dst in zip(lyrics.paths(g["lyrics"]), lyrics.paths(keep)):
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(dst)
            index[keep] = index[g["lyrics"]]
        # the alias first: if the move below fails, the next run sees both files again and redoes the group
        tags.append(musicdb.DATA / "aliases.jsonl",
                            [{"old": d, "new": keep, "why": "same audio", "md5": g["hash"], "at": now} for d in g["drop"]])
        for d in g["drop"]:
            dst = QUARANTINE / day / d
            dst.parent.mkdir(parents=True, exist_ok=True)
            (musicdb.MUSIC / d).rename(dst)
            index.pop(d, None)
    lyrics.save(index)
    # skips keep their old path (the scrobbler's log is re-imported by (ts, file): a rewritten row would come back
    # as a second skip); skipped() and not_finished() read them through the aliases
    n = rewrite_playlists(musicdb.aliases())
    mpd_update()
    musicdb.sync(None)
    print(f"{len(groups)} groups, {sum(len(g['drop']) for g in groups)} copies moved to {QUARANTINE / day}, "
          f"{n} playlists rewritten" + (f", new playlists: {', '.join(made)}" if made else ""))
    return groups


def show(groups):
    for g in groups:
        print(f"keep  {g['keep']}" + (f"   like={g['like']}" if g["like"] is not None else "")
              + ("   LIKE CONFLICT" if g["like_conflict"] else ""))
        for d in g["drop"]:
            extra = (f"   +tags {', '.join(g['tags'][d])}" if g["tags"][d] else "") + ("   +lyrics" if g["lyrics"] == d else "")
            print(f"  drop {d}{extra}")
    print(f"\n{len(groups)} groups, {sum(len(g['drop']) for g in groups)} copies to drop "
          f"(musicdb dedupe --apply; they go to {QUARANTINE}/<date>/)")


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb dedupe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    groups = plan()
    if a.apply:
        conflicts = [g["keep"] for g in groups if g["like_conflict"]]
        if conflicts:
            sys.exit("copies with different likes, decide first (rmpc key r on each): " + ", ".join(conflicts))
        apply(groups)
    elif a.json:
        print(json.dumps(groups, ensure_ascii=False, indent=1))
    else:
        show(groups)
