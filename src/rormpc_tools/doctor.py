"""musicdb doctor: look for silent data errors, the kind that look plausible and are wrong. Read-only.

  musicdb doctor [--json] [-v] [--strict]

Checks:
  lb-duplicates     one ListenBrainz listen stored twice (counted twice); `musicdb import-lb` merges them
  shared-ids        a YouTube id or recording MBID in several library files: its plays go to one copy only
                    (`musicdb versions shared-ok ID` when that is right, else fix the wrong tag)
  ambiguous-names   plays matched by name to several files and not decided, so credited to none
  identity          files without a registry id (musicdb identity sync), ids missing from the file's tags or
                    disagreeing with them, a name's YouTube id disagreeing with the registry, live rows whose file is gone
  versions-open     `musicdb versions`: undecided tracks, unlabelled files sharing a name, decisions whose
                    file is gone or whose group changed since (a download, a deletion)
  local-orphans     scrobbler listens whose file is gone and that no YouTube id or MBID finds again
  stale-paths       skips, keep decisions, hand-made lists, likes and playlists naming files MPD does not have
  accounting        every event is credited to a file, unmatched, or a known duplicate: nothing vanishes
--strict exits 1 when any check finds something (for tests and hooks). --fix removes playlist lines of songs
deleted with `musicdb delete`, the only finding that is safe to repair without a decision.
"""
import argparse, collections, datetime as dt, json, sys

from . import identity, musicdb, settings, tags, versions


def check():
    c, lib, m = musicdb.db(), musicdb.library(), musicdb.mpd()
    songs = [s for s in m.listallinfo() if s.get("file")]
    files = {s["file"] for s in songs}
    out = {}

    out["lb-duplicates"] = [{"ts": ts, "artist": a, "title": t, "copies": n} for ts, a, t, n in c.execute(
        "SELECT ts, artist, title, count(*) FROM events WHERE source = 'lb' GROUP BY ts, artist, title HAVING count(*) > 1")]

    ids = collections.defaultdict(set)
    for s in songs:
        if y := identity.ytid(s["file"]):
            ids[f"yt:{y}"].add(s["file"])
        if mb := musicdb.one(s.get("musicbrainz_trackid", "")):
            ids[f"mb:{mb}"].add(s["file"])
    ok = versions.fold()["shared"]
    out["shared-ids"] = [{"id": k, "files": sorted(v)} for k, v in sorted(ids.items()) if len(v) > 1 and k not in ok]

    local = {ts for (ts,) in c.execute("SELECT ts FROM events WHERE source = 'local'")}
    ambiguous, orphans, credited, unmatched, dup = collections.Counter(), [], 0, 0, 0
    lib_files = musicdb.library_files(lib)
    musicdb.prepare(lib, lib_files)
    total = 0
    for src, ts, ytid, mbid, uri, artist, title, extra in c.execute(
            "SELECT source, ts, ytid, mbid, spotify_uri, artist, title, extra FROM events"):
        total += 1
        if src == "lb" and ts in local:
            dup += 1
            continue
        f = musicdb.event_file(lib, lib_files, src, ytid, mbid, artist, title, extra, uri)
        if f:
            credited += 1
            continue
        unmatched += 1
        if src == "local":
            orphans.append({"ts": ts, "file": json.loads(extra or "{}").get("file")})
        elif (artist and title and len(lib[2].get(musicdb.name_key(artist, title), ())) > 1
              and musicdb.RESOLVE[0](src, uri, mbid, artist, title) is None):
            ambiguous[(artist, title)] += 1
    out["ambiguous-names"] = [{"artist": a, "title": t, "plays": n,
                               "files": sorted(lib[2][musicdb.name_key(a, t)])} for (a, t), n in ambiguous.most_common()]
    out["local-orphans"] = orphans
    out["identity"] = identity_problems(sorted(files))
    out["versions-open"] = [{"name": g["name"], "open": g["pending"]} for g in versions.groups()]

    stale, al, gone = [], musicdb.aliases(), deleted()
    # history of a song deleted through `musicdb delete` (skips, keep decisions, likes) is expected to name it
    for (f,) in c.execute("SELECT DISTINCT file FROM skips"):
        if musicdb.canon(f, al) not in files and f not in gone:
            stale.append({"where": "skips", "file": f})
    for r in musicdb.jsonl(musicdb.DATA / "not-finished-keep.jsonl"):
        if r.get("file") and musicdb.canon(r["file"], al) not in files and r["file"] not in gone:
            stale.append({"where": "not-finished-keep.jsonl", "file": r["file"]})
    for name, songs_in in tags.lists().items():
        for e in songs_in.values():
            if musicdb.canon(e["song"].get("file"), al) not in files:
                stale.append({"where": f"list {name}", "file": e["song"].get("file")})
    for r in musicdb.jsonl(musicdb.DATA / "likes.jsonl"):
        if r.get("file") not in files and r.get("file") not in gone:
            stale.append({"where": "likes.jsonl", "file": r.get("file")})
    if musicdb.PLAYLISTS.exists():
        for pl in sorted(musicdb.PLAYLISTS.glob("*.m3u")):
            for line in pl.read_text(errors="replace").splitlines():
                if line and not line.startswith("#") and "://" not in line and line not in files:
                    stale.append({"where": f"playlist {pl.stem}", "file": line}
                                 | ({"fix": "deleted: musicdb doctor --fix removes it"} if line in gone else {}))
    out["stale-paths"] = stale

    out["accounting"] = {"events": total, "credited": credited, "unmatched": unmatched, "lb-copy-of-local": dup,
                         "ok": total == credited + unmatched + dup}
    return out


def identity_problems(files):
    reg, problems = identity.load(fresh=True), []
    tags = identity.cached_tags(files)
    for f in files:
        r = identity.resolve(f)
        if not r or r.get("path") != f:
            problems.append({"file": f, "why": "not registered"})
            continue
        sid, tag_yt = tags.get(f, (None, None))
        name_yt = identity.ytid_from_name(f)
        if sid != r["id"]:
            problems.append({"file": f, "why": "id missing from the tags" if not sid else f"tags carry id {sid}"})
        for what, yt in (("tag", tag_yt), ("name", name_yt)):
            if yt and r.get("ytid") and yt != r["ytid"]:
                problems.append({"file": f, "why": f"{what} says YouTube id {yt}, registry {r['ytid']}"})
    live = set(files)
    problems += [{"file": r["path"], "why": "registry says live, MPD has no such file"}
                 for r in reg["rows"].values() if r.get("state") == "live" and r.get("path") not in live]
    return problems


def deleted():
    """Files deleted through `musicdb delete` (the journal, finished and pending)."""
    return {r["file"] for p in (musicdb.DONE, musicdb.PENDING) for r in musicdb.jsonl(p) if r.get("file")}


def fix():
    """Remove playlist lines naming files deleted through `musicdb delete`; nothing else is changed."""
    gone, n = deleted(), 0
    for pl in sorted(musicdb.PLAYLISTS.glob("*.m3u")) if musicdb.PLAYLISTS.exists() else []:
        lines = pl.read_text(errors="surrogateescape").splitlines()
        keep = [l for l in lines if l not in gone]
        if keep != lines:
            tmp = pl.with_name("." + pl.name + ".tmp")
            tmp.write_text("".join(l + "\n" for l in keep), errors="surrogateescape")
            tmp.replace(pl)
            print(f"{pl.stem}: removed {len(lines) - len(keep)} deleted songs")
            n += len(lines) - len(keep)
    return n


SUMMARY = settings.XDG_CACHE / "rormpc-tools" / "doctor.json"


def write_summary(out=None):
    """Counts per check into ~/.cache/rormpc-tools/doctor.json (atomic), for rormpc's status bar and for
    `musicdb update`, which runs it hourly after imports, downloads and deletions. Returns a one-line summary."""
    out = out or check()
    bad = problems(out)
    counts = {k: (len(v) if isinstance(v, list) else int(not v["ok"])) for k, v in bad.items()}
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    tmp = SUMMARY.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "at": dt.datetime.now().isoformat(timespec="seconds"),
                               "clean": not bad, "counts": counts}))
    tmp.replace(SUMMARY)
    return "doctor: clean" if not bad else "doctor: " + ", ".join(f"{k} {n}" for k, n in counts.items())


def problems(out):
    return {k: v for k, v in out.items() if (not v["ok"] if k == "accounting" else v)}


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", action="store_true", help="list every finding, not the first 5 per check")
    ap.add_argument("--strict", action="store_true", help="exit 1 when anything is found")
    ap.add_argument("--fix", action="store_true", help="remove playlist lines of songs deleted with musicdb delete")
    a = ap.parse_args(argv)
    if a.fix:
        fix()
    out = check()
    write_summary(out)
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        acc = out["accounting"]
        print(f"events {acc['events']}: credited {acc['credited']}, unmatched {acc['unmatched']}, "
              f"LB copies of local listens {acc['lb-copy-of-local']}" + ("" if acc["ok"] else "  MISMATCH"))
        for k, v in out.items():
            if k == "accounting":
                continue
            print(f"{k}: {len(v) or 'ok'}")
            for item in v if a.v else v[:5]:
                print("  " + json.dumps(item, ensure_ascii=False))
            if v and not a.v and len(v) > 5:
                print(f"  … {len(v) - 5} more (-v)")
    if a.strict and problems(out):
        sys.exit(1)
