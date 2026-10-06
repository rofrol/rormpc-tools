"""musicdb doctor: look for silent data errors, the kind that look plausible and are wrong. Read-only.

  musicdb doctor [--json] [-v] [--strict]

Checks:
  lb-duplicates     one ListenBrainz listen stored twice (counted twice); `musicdb import-lb` merges them
  shared-ids        a YouTube id or recording MBID in several library files: its plays go to one copy only
                    (`musicdb versions shared-ok ID` when that is right, else fix the wrong tag)
  ambiguous-names   plays matched by name to several files and not decided, so credited to none
  versions-open     `musicdb versions`: undecided tracks, unlabelled files sharing a name, decisions whose
                    file is gone or whose group changed since (a download, a deletion)
  local-orphans     scrobbler listens whose file is gone and that no YouTube id or MBID finds again
  stale-paths       skips, keep decisions, hand-made lists, likes and playlists naming files MPD does not have
  accounting        every event is credited to a file, unmatched, or a known duplicate: nothing vanishes
--strict exits 1 when any check finds something (for tests and hooks).
"""
import argparse, collections, json, sys

from . import musicdb, tags, versions


def check():
    c, lib, m = musicdb.db(), musicdb.library(), musicdb.mpd()
    songs = [s for s in m.listallinfo() if s.get("file")]
    files = {s["file"] for s in songs}
    out = {}

    out["lb-duplicates"] = [{"ts": ts, "artist": a, "title": t, "copies": n} for ts, a, t, n in c.execute(
        "SELECT ts, artist, title, count(*) FROM events WHERE source = 'lb' GROUP BY ts, artist, title HAVING count(*) > 1")]

    ids = collections.defaultdict(set)
    for s in songs:
        if y := musicdb.YTID_IN_NAME.search(s["file"]):
            ids[f"yt:{y.group(1)}"].add(s["file"])
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
    out["versions-open"] = [{"name": g["name"], "open": g["pending"]} for g in versions.groups()]

    stale, al = [], musicdb.aliases()
    for (f,) in c.execute("SELECT DISTINCT file FROM skips"):
        if musicdb.canon(f, al) not in files:
            stale.append({"where": "skips", "file": f})
    for r in musicdb.jsonl(musicdb.DATA / "not-finished-keep.jsonl"):
        if r.get("file") and musicdb.canon(r["file"], al) not in files:
            stale.append({"where": "not-finished-keep.jsonl", "file": r["file"]})
    for name, songs_in in tags.lists().items():
        for e in songs_in.values():
            if musicdb.canon(e["song"].get("file"), al) not in files:
                stale.append({"where": f"list {name}", "file": e["song"].get("file")})
    for r in musicdb.jsonl(musicdb.DATA / "likes.jsonl"):
        if r.get("file") not in files:
            stale.append({"where": "likes.jsonl", "file": r.get("file")})
    if musicdb.PLAYLISTS.exists():
        for pl in sorted(musicdb.PLAYLISTS.glob("*.m3u")):
            for line in pl.read_text(errors="replace").splitlines():
                if line and not line.startswith("#") and "://" not in line and line not in files:
                    stale.append({"where": f"playlist {pl.stem}", "file": line})
    out["stale-paths"] = stale

    out["accounting"] = {"events": total, "credited": credited, "unmatched": unmatched, "lb-copy-of-local": dup,
                         "ok": total == credited + unmatched + dup}
    return out


def problems(out):
    return {k: v for k, v in out.items() if (not v["ok"] if k == "accounting" else v)}


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", action="store_true", help="list every finding, not the first 5 per check")
    ap.add_argument("--strict", action="store_true", help="exit 1 when anything is found")
    a = ap.parse_args(argv)
    out = check()
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
