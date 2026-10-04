"""Genre explorer: every genre of my library songs with how many carry it, and the genres pinned as checkboxes in
rormpc's Hits pane.

  hits genres [--json]           # genres of the library, most songs first
  hits genres pin GENRE ...      # show it as a checkbox in the Hits filter column
  hits genres unpin GENRE ...
  hits genres pins [--json]

A song's genres: its recording's MusicBrainz genres with at least 2 votes, else its artist's (also at least 2
votes when any have that many), plus my corrections (`musicdb genre`). Unlike the Hits display there is no top-3
cut, so a "funk, disco, soul" artist counts for disco too. Counts overlap (a song has several genres); "recording"
vs "artist" says where the genre came from, and artist genres make pop look bigger than it is. Aliases (hip-hop,
rap -> hip hop; rhythm and blues -> r&b) are merged here and in the Hits genre filter; edit them in the pins file.

Pins and aliases: ~/.config/rormpc-tools/hits-genres.json (version 1). rormpc reads it; only this command writes it.
"""
import argparse, collections, json, sys

from . import mbtag, settings

PINS = settings.XDG_CONFIG / "rormpc-tools" / "hits-genres.json"
DEFAULT_PINS = ["rock", "pop", "hip hop", "r&b", "soul", "dance", "electronic", "disco", "funk", "country", "metal",
                "folk", "latin", "jazz", "blues", "punk", "reggae", "classical"]
DEFAULT_ALIASES = {"hip-hop": "hip hop", "rap": "hip hop", "rhythm and blues": "r&b"}


def load_pins():
    if PINS.exists():
        d = json.loads(PINS.read_text())
        return {"version": 1, "pins": d.get("pins", DEFAULT_PINS), "aliases": d.get("aliases", DEFAULT_ALIASES)}
    return {"version": 1, "pins": list(DEFAULT_PINS), "aliases": dict(DEFAULT_ALIASES)}


def save_pins(d):
    PINS.parent.mkdir(parents=True, exist_ok=True)
    tmp = PINS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.replace(PINS)


def voted(tags):
    """Tags with >= 2 votes when there are any, else all of them (a sparse entry still says something)."""
    tags = [t for t in tags if t.get("count", 0) > 0]
    strong = [t for t in tags if t["count"] >= 2]
    return [t.get("name") or t.get("tag") for t in (strong or tags)]


def index():
    """[(file, plays, [genres], provenance)] for every library song."""
    from .hits import cached, lb_metadata
    from .musicdb import counted, db, library, mpd, one
    from .tags import effective_genres, manual_genres
    m, lib = mpd(), library()
    plays = counted(db(), lib)[0]
    songs = [s for s in m.listallinfo() if s.get("file")]
    meta = lb_metadata([one(s["musicbrainz_trackid"]) for s in songs if s.get("musicbrainz_trackid")])
    manual, aliases = manual_genres(), load_pins()["aliases"]
    out = []
    for s in songs:
        rec_mbid = one(s.get("musicbrainz_trackid", "")) or None
        md = meta.get(rec_mbid) or {}
        rec = voted([t for t in md.get("tag", {}).get("recording", []) if t.get("genre_mbid")])
        if rec:
            gs, how = rec, "recording"
        else:
            artist_mbid = (one(s.get("musicbrainz_artistid", "")) or "").split("/")[0].strip()
            r = cached(f"a-{artist_mbid}", lambda: mbtag.http(
                f"https://musicbrainz.org/ws/2/artist/{artist_mbid}?inc=genres+tags&fmt=json") or {}) if artist_mbid else {}
            gs, how = voted(r.get("genres", [])), "artist"
        gs = effective_genres([aliases.get(g, g) for g in gs], s["file"], rec_mbid, manual)
        out.append((s["file"], plays.get(s["file"], 0), list(dict.fromkeys(gs)), how if gs else "unknown"))
    return out


def cmd_list(a):
    rows = index()
    counts = collections.defaultdict(lambda: {"songs": 0, "recording": 0, "artist": 0, "plays": 0})
    for _f, plays, gs, how in rows:
        for g in gs:
            c = counts[g]
            c["songs"] += 1; c["plays"] += plays
            c["recording" if how == "recording" else "artist"] += 1
    pins = set(load_pins()["pins"])
    out = sorted(({"name": g} | c | {"pinned": g in pins} for g, c in counts.items()), key=lambda x: (-x["songs"], x["name"]))
    unknown = sum(1 for r in rows if r[3] == "unknown")
    if a.json:
        print(json.dumps({"total": len(rows), "unknown": unknown, "genres": out}, ensure_ascii=False)); return
    print(f"{len(rows)} songs, {unknown} without a genre; songs (from recording / artist), plays")
    for x in out:
        print(f"{x['songs']:4d} ({x['recording']:3d}/{x['artist']:3d}) {x['plays']:5d}  {'📌 ' if x['pinned'] else ''}{x['name']}")


def cmd_pin(a, pin):
    d = load_pins()
    for g in (g.strip().lower() for g in a.genres):
        if pin and g not in d["pins"]:
            d["pins"].append(g)
        if not pin and g in d["pins"]:
            d["pins"].remove(g)
    save_pins(d)
    print(f"pinned: {', '.join(d['pins'])}")


def main(argv):
    if argv and argv[0] in ("pin", "unpin", "pins"):
        ap = argparse.ArgumentParser(prog=f"hits genres {argv[0]}")
        ap.add_argument("cmd")
        if argv[0] == "pins":
            ap.add_argument("--json", action="store_true")
            a = ap.parse_args(argv)
            d = load_pins()
            print(json.dumps(d, ensure_ascii=False) if a.json else "\n".join(d["pins"]))
            return
        ap.add_argument("genres", nargs="+")
        return cmd_pin(ap.parse_args(argv), argv[0] == "pin")
    ap = argparse.ArgumentParser(prog="hits genres", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    cmd_list(ap.parse_args(argv))


if __name__ == "__main__":
    main(sys.argv[1:])
