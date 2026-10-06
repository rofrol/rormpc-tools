"""How my most played songs rose and fell: a standalone HTML page (no network, open it in a browser).

  musicdb chart                       # quarters, writes ~/.cache/rormpc-tools/chart.html and prints the path
  musicdb chart --bucket month -o FILE [--open]

- Top 10 (most plays overall) as a bump chart: rank among all songs played in that quarter; the tooltip has the
  plays, since rank alone hides 5 vs 50. A quarter where a song wasn't played is a gap, not a zero.
- Top 100 as small multiples: each song's share of that quarter's plays.
- Race: my top 10 of each listening year as animated bars (play, pause, scrub, speed), like a bar chart race.
  The weighted shuffle's own picks (mpd-player's auto.jsonl) are left out: they show what the algorithm chose.
  Years with fewer than THIN plays are marked as thin.
- Coverage strip: plays per quarter by source. Sources changed over the years (Spotify export, then MPD and
  ListenBrainz), and that looks like a change in taste; quarters with fewer than 30 plays are greyed out and left
  out of the shares.

Songs are merged by artist + title (musicdb's name key: main artist, title without "(remix)"/"[Remastered]").
A ListenBrainz listen with the timestamp of the scrobbler's local one is counted once. Spotify counts a play from
30 s, ListenBrainz and the local scrobbler log from 90 % of the song: the page says so.
"""
import argparse, collections, json, pathlib, subprocess, sys

from . import settings

THIN = 30  # plays in a bucket below which its ranks and shares are not trusted
TOP_BUMP, TOP_GRID, MAX_RANK = 10, 100, 30
OUT = settings.XDG_CACHE / "rormpc-tools" / "chart.html"


def bucket_of(ts, kind):
    y, m = int(ts[:4]), int(ts[5:7])
    return f"{y}-{m:02d}" if kind == "month" else f"{y} Q{(m - 1) // 3 + 1}"


def all_buckets(first, last, kind):
    """Every bucket from first to last, so empty ones show as gaps."""
    out, (y, m) = [], (int(first[:4]), int(first[5:7]) if kind == "month" else (int(first[-1]) - 1) * 3 + 1)
    end = (int(last[:4]), int(last[5:7]) if kind == "month" else (int(last[-1]) - 1) * 3 + 1)
    while (y, m) <= end:
        out.append(f"{y}-{m:02d}" if kind == "month" else f"{y} Q{(m - 1) // 3 + 1}")
        m += 1 if kind == "month" else 3
        if m > 12:
            y, m = y + 1, m - 12
    return out


def library_names():
    """Resolver for plays that name only a file or video id (MPD log, the scrobbler's local log): (artist, title)
    from the MPD tags of the library file musicdb matches them to, else None."""
    from .musicdb import event_file, library, library_files, mpd, one, prepare
    lib = library()
    files = library_files(lib)
    prepare(lib, files)
    tags = {s["file"]: (one(s.get("artist", "")), one(s.get("title", ""))) for s in mpd().listallinfo() if s.get("file")}
    def resolve(e):
        f = event_file(lib, files, e["source"], e.get("ytid"), e.get("mbid"), e.get("artist"), e.get("title"), e.get("extra"), e.get("spotify_uri"))
        return tags.get(f) if f and all(tags.get(f, ("", ""))) else None
    return resolve


def compute(events, kind, resolve=lambda e: None):
    from .musicdb import name_key
    local = {e["ts"] for e in events if e["source"] == "local"}
    plays = collections.defaultdict(collections.Counter)  # bucket -> song -> plays
    sources = collections.defaultdict(collections.Counter)  # bucket -> source -> plays
    names = collections.defaultdict(collections.Counter)
    for e in events:
        if e["source"] == "lb" and e["ts"] in local:
            continue
        artist, title = e.get("artist"), e.get("title")
        if e["source"] in ("mpd", "local") or not (artist and title):
            artist, title = resolve(e) or (None, None)  # these name a file, not a song
        if not (artist and title):
            continue
        key = name_key(artist, title)
        b = bucket_of(e["ts"], kind)
        plays[b][key] += 1
        sources[b][e["source"]] += 1
        names[key][f"{artist} - {title}"] += 1
    buckets = all_buckets(min(plays), max(plays), kind)
    total = collections.Counter()
    for b in plays.values():
        total.update(b)
    top = [k for k, _ in total.most_common(TOP_GRID)]
    ranks = {}
    for b, counter in plays.items():
        ordered = sorted(counter.items(), key=lambda kv: -kv[1])
        r, prev = 0, None
        for i, (k, n) in enumerate(ordered, 1):  # ties share a rank
            if n != prev:
                r, prev = i, n
            ranks[(b, k)] = r
    songs = []
    for i, k in enumerate(top):
        series = []
        for b in buckets:
            n = plays.get(b, {}).get(k, 0)
            size = sum(plays.get(b, {}).values())
            series.append({"plays": n, "rank": ranks.get((b, k)) if n else None,
                           "share": round(n / size, 4) if n and size >= THIN else None})
        played = [j for j, p in enumerate(series) if p["plays"]]
        songs.append({"name": names[k].most_common(1)[0][0], "total": total[k], "series": series,
                      "peak": min((p["rank"] for p in series if p["rank"]), default=None),
                      "first": buckets[played[0]], "last": buckets[played[-1]]})
    src_names = sorted({s for c in sources.values() for s in c}, key=lambda s: -sum(c[s] for c in sources.values()))
    coverage = [{"bucket": b, "total": sum(plays.get(b, {}).values()),
                 "sources": {s: sources.get(b, {}).get(s, 0) for s in src_names}} for b in buckets]
    return {"kind": kind, "buckets": buckets, "songs": songs, "coverage": coverage, "sources": src_names,
            "thin": THIN, "top_bump": TOP_BUMP, "max_rank": MAX_RANK,
            "events": sum(c["total"] for c in coverage)}


RACE_TOP = 10


def auto_index():
    """{name key: [start times]} of the weighted shuffle's own picks, keyed like the plays (artist + title)."""
    from .musicdb import auto_starts, mpd, name_key, one
    starts = auto_starts()
    if not starts:
        return {}
    tags = {s["file"]: (one(s.get("artist", "")), one(s.get("title", ""))) for s in mpd().listallinfo() if s.get("file")}
    out = collections.defaultdict(list)
    for f, ts in starts.items():
        if all(tags.get(f, ("", ""))):
            out[name_key(*tags[f])].extend(ts)
    return out


def compute_race(events, resolve=lambda e: None, auto=None):
    """My top RACE_TOP per listening year, without the shuffle's own picks: [{year, total, thin, top: [{name,
    plays}]}], plus how many plays were left out as the shuffle's."""
    from .musicdb import name_key, ts_epoch
    auto = auto or {}
    local = {e["ts"] for e in events if e["source"] == "local"}
    plays, names, left_out = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter), 0
    seen = set()  # every year with a play, the shuffle's included: its years stay on the axis, empty
    for e in events:
        if e["source"] == "lb" and e["ts"] in local:
            continue
        artist, title = e.get("artist"), e.get("title")
        if e["source"] in ("mpd", "local") or not (artist and title):
            artist, title = resolve(e) or (None, None)
        if not (artist and title):
            continue
        key = name_key(artist, title)
        seen.add(int(e["ts"][:4]))
        if key in auto:
            t = ts_epoch(e["ts"])
            if any(s - 120 <= t <= s + 1800 for s in auto[key]):
                left_out += 1
                continue
        plays[e["ts"][:4]][key] += 1
        names[key][f"{artist} - {title}"] += 1
    years = [str(y) for y in range(min(seen), max(seen) + 1)] if seen else []
    frames = []
    for y in years:
        c = plays.get(y, collections.Counter())
        total = sum(c.values())
        frames.append({"year": y, "total": total, "thin": total < THIN,
                       "top": [{"name": names[k].most_common(1)[0][0], "plays": n} for k, n in c.most_common(RACE_TOP)]})
    return {"frames": frames, "left_out": left_out, "top": RACE_TOP}


def main(argv):
    ap = argparse.ArgumentParser(prog="musicdb chart", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", choices=["quarter", "month"], default="quarter")
    ap.add_argument("-o", "--output", default=str(OUT))
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    a = ap.parse_args(argv)
    path = settings.DATA_DIR / "events.jsonl"
    events = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    try:
        resolve = library_names()
    except Exception as err:  # MPD down: Spotify/ListenBrainz plays still have names
        print(f"warning: no MPD library ({err}); plays logged by file are left out", file=sys.stderr)
        resolve = lambda e: None
    data = compute(events, a.bucket, resolve)
    try:
        auto = auto_index()
    except Exception as err:  # MPD down: the shuffle's picks can't be named, so none are left out
        print(f"warning: the shuffle's picks are not left out ({err})", file=sys.stderr)
        auto = {}
    data["race"] = compute_race(events, resolve, auto)
    html = (pathlib.Path(__file__).with_name("chart.html").read_text()
            .replace("/*DATA*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    out = pathlib.Path(a.output).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(html)
    tmp.replace(out)
    print(out)
    if a.open:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(out)])
