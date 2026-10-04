"""Top hits of a decade (Billboard Year-End Hot 100, 1959-), genre filter, MPD playlist of what you have.

  hits 1980s                                  # top 100 of the decade, all genres
  hits 1980s -n 10 -g "rock -country"         # include/exclude genres: a song matches "rock" if any of its
                                              # MusicBrainz genres contains the word (hard rock, pop rock, ...)
  hits 1990s -g "hip hop,r&b" --rank listens  # rank by ListenBrainz listen counts instead of chart points
  hits 1980s -g rock --playlist               # write MPD playlist "Hits 1980s rock top100" (songs in the library)
  hits 1980s -g rock --download               # yt-mp3-mb the missing ones into <music>/Hits/1980s (first hit, unverified)
  hits fetch --help                           # verified import queue for missing songs (rormpc: Fetch missing…)
  hits all -n 10 -g "+rock -thrash metal" --playlist   # top 10 of every decade, one playlist ordered by decade
  hits all -n 10 --owned --playlist           # the 10 biggest hits you have from each decade
  hits --years 1985-1992 --top 11-20 -g "+rock +pop -country"   # ranks 11-20% of that cohort
  hits 1980s --top 1-10 --json ~/.cache/rormpc/hits/current.json  # result file for rormpc's Hits pane
  hits prefetch 1959-2025                     # warm the caches (charts + MusicBrainz, ~1 request/s)
  hits --source likes [--sort rediscover]     # your liked songs instead of a chart
  hits --source recs                          # recommendations: artists similar to your most played (LB Radio)
  hits hide --artist A --title T [--mbid M]   # hide a song from every Hits result (log in the data repo)
  hits unhide --artist A --title T; hits hidden [--json]   # undo / review

Ranking "chart" = the song's best year-end position in the chosen years (101 - position); points summed over
years only break ties. Genres come from the recording's
MusicBrainz genres/tags, falling back to the artist's. Library matching and play counts reuse musicdb.
Caches: ~/.cache/hits/.
"""
import argparse, datetime as dt, json, math, os, pathlib, re, subprocess, sys, urllib.parse, urllib.request

from . import mbtag, musicdb, settings


CACHE = pathlib.Path(os.environ.get("XDG_CACHE_HOME", pathlib.Path.home() / ".cache")) / "hits"
PLAYLISTS = settings.MPD_PLAYLISTS
FIRST_YEAR = 1959  # the Year-End Hot 100 starts with 1959


# ---------------------------------------------------------------- charts

def wiki_text(s):
    s = re.sub(r"<ref[^>]*/>|<ref.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"\{\{(?:sort|sortname)\|[^|}]*\|([^}]*)\}\}", r"\1", s, flags=re.I)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"<[^>]+>|'''?|&nbsp;", " ", s)
    return re.sub(r"\s+", " ", s).strip().strip('"').strip()


def cells(row):
    out = []
    for line in row.split("\n"):
        line = line.strip()
        if not line or line[0] not in "|!" or line.startswith(("|+", "|}", "{|")):
            continue
        for c in re.split(r"\|\||!!", line[1:]):
            # drop cell attributes: 'scope="row" style="..." | value'
            if "|" in c and re.match(r'\s*(?:scope|style|rowspan|colspan|align|class|data-sort-value)\s*=', c):
                c = c.split("|", 1)[1]
            out.append(c)
    return out


def chart(year):
    """[(position, title, artist)] of the Billboard Year-End Hot 100 for a year (cached)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"chart-{year}.json"
    if f.exists():
        return json.loads(f.read_text())
    u = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "parse", "page": f"Billboard Year-End Hot 100 singles of {year}", "prop": "wikitext",
        "format": "json", "formatversion": 2, "redirects": 1})
    w = (mbtag.http(u, host_interval=0.2) or {}).get("parse", {}).get("wikitext", "")
    table = w[w.find("{|"): w.find("|}", w.find("{|"))] if "{|" in w else ""
    rows, pos = [], 0
    for r in re.split(r"\n\|-[^\n]*", table):
        c = [wiki_text(x) for x in cells(r)]
        if len(c) >= 3 and re.fullmatch(r"\d+", c[0]):
            pos = int(c[0])
            rows.append((pos, c[1], c[2]))
        elif len(c) == 2 and pos and not c[0].isdigit():  # tie: position cell spans several rows
            rows.append((pos, c[0], c[1]))
    if len(rows) >= 50:
        f.write_text(json.dumps(rows, ensure_ascii=False))
    else:
        print(f"chart {year}: parsed only {len(rows)} rows", file=sys.stderr)
    return rows


# ---------------------------------------------------------------- MusicBrainz

def cached(name, fn):
    f = CACHE / "mb" / (re.sub(r"[^\w.-]", "_", name)[:180] + ".json")
    if f.exists():
        return json.loads(f.read_text())
    f.parent.mkdir(parents=True, exist_ok=True)
    r = fn()
    f.write_text(json.dumps(r, ensure_ascii=False))
    return r


def main_artist(artist):
    return re.split(r"\s+(?:featuring|feat\.?|ft\.?|with|and|&|x|vs\.?)\s+|,\s*", artist, flags=re.I)[0].strip()


def mb_song(title, artist, year):
    """Best MB recording for a chart entry: {mbid, artist_mbid, first, tags} or {}."""
    def search():
        q = f'recording:"{title}" AND artist:"{main_artist(artist)}"'
        return mbtag.http("https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode({"query": q, "limit": 25, "fmt": "json"})) or {}
    names = [main_artist(artist)] + [main_artist(x) for x in re.findall(r"\(([^)]*)\)", artist)]
    ok, early = None, None
    for rec in cached(f"s-{artist}-{title}", search).get("recordings", []):
        credit = main_artist("".join(a["name"] + a.get("joinphrase", "") for a in rec.get("artist-credit", [])))
        if mbtag.sim(rec["title"], title) < 0.85 or max(mbtag.sim(credit, n) for n in names) < 0.7:
            continue
        if mbtag.versions(rec["title"]) - mbtag.versions(title) or rec.get("video"):
            continue
        first = rec.get("first-release-date") or "9999"
        key = (-rec.get("score", 0) // 10, first)  # coarse score first, then the earliest release
        if int(first[:4]) <= year + 1 and (ok is None or key < ok[0]):
            ok = (key, rec)
        if early is None or first < early[0][1]:
            early = (key, rec)
    # the original is often missing among 25 hits full of reissues: then take the earliest matching one
    best = ok or early
    if not best:
        return {}
    rec = best[1]
    ac = rec.get("artist-credit") or [{}]
    return {"mbid": rec["id"], "artist_mbid": (ac[0].get("artist") or {}).get("id"), "first": rec.get("first-release-date"),
            "mb_title": rec["title"], "tags": top_tags(rec.get("tags", []))}


def top_tags(tags, n=3):
    """The n most voted tags; single-vote tags are noise ("rock" on a Madonna song) unless nothing else exists."""
    tags = sorted((t for t in tags if t.get("count", 0) > 0), key=lambda t: -t["count"])
    if tags and tags[0]["count"] >= 2:
        tags = [t for t in tags if t["count"] >= 2]
    return [t["name"] for t in tags[:n]]


def artist_genres(mbid):
    if not mbid:
        return []
    r = cached(f"a-{mbid}", lambda: mbtag.http(f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=genres+tags&fmt=json") or {})
    return top_tags(r.get("genres", [])) or top_tags(r.get("tags", []))


def genres(song):
    """Recording tags only when they are backed by several votes, else the artist's main genres."""
    return song.get("tags") or artist_genres(song.get("artist_mbid"))


def lb_popularity(mbids):
    """ListenBrainz listen counts, only a tie-breaker: when LB is slow or down, warn and rank without it."""
    out = {}
    mbids = [m for m in mbids if m]
    for i in range(0, len(mbids), 100):
        # optional tie-breaker: fail fast (2 tries x 8 s) instead of holding up the whole ranking
        r = mbtag.http("https://api.listenbrainz.org/1/popularity/recording", host_interval=0.5,
                       data=json.dumps({"recording_mbids": mbids[i:i + 100]}).encode(),
                       headers={"Content-Type": "application/json"}, attempts=2, timeout=8)
        if r is None:
            print("warning: ListenBrainz popularity unavailable, ties broken by chart data only", file=sys.stderr)
            return {}
        for x in r:
            out[x["recording_mbid"]] = x.get("total_listen_count") or 0
    return out


# ---------------------------------------------------------------- genre filter

def genre_filter(spec):
    """'+rock -thrash metal, pop' -> predicate over a list of genre names.
    Included genres are ORed, excluded ones win; word match, so 'rock' hits 'hard rock'. '+' is optional."""
    inc, exc = [], []
    for tok in re.findall(r"[-+]?[^,\s][^,]*?(?=\s+[-+]|,|$)", spec or ""):
        tok = tok.strip()
        (exc if tok.startswith("-") else inc).append(tok.lstrip("-+").strip().lower())
    word = lambda g, t: re.search(rf"(?<![\w&]){re.escape(t)}(?![\w&])", g.lower())
    def ok(gs):
        if any(word(g, t) for g in gs for t in exc):
            return False
        return not inc or any(word(g, t) for g in gs for t in inc)
    return ok


# ---------------------------------------------------------------- main

DECADES = [f"{d}s" for d in range(1950, 2030, 10)]


def decade_years(s):
    m = re.fullmatch(r"(\d{4})s?", s) or re.fullmatch(r"(\d{2})s", s)
    if not m:
        sys.exit(f"decade like 1980s, not {s!r}")
    y = int(m.group(1))
    y = y + (1900 if y >= 50 else 2000) if y < 100 else y
    # a year-end chart exists only once the year is over
    return list(range(max(y - y % 10, FIRST_YEAR), min(y - y % 10 + 10, dt.date.today().year)))


def entries(years):
    """Songs of the given years merged across charts: {key: {title, artist, points, best, years}}."""
    songs = {}
    for y in years:
        for pos, title, artist in chart(y):
            k = musicdb.name_key(artist, title)
            s = songs.setdefault(k, {"title": title, "artist": artist, "points": 0, "peak": 0, "best": 101, "years": [], "year": y})
            s["points"] += 101 - pos
            # each year-end chart has 100 places, so 101 - position is already comparable across years
            s["peak"] = max(s["peak"], 101 - pos)
            s["best"] = min(s["best"], pos)
            s["years"].append(y)
    return songs


def range_years(part):
    """'1985-1992' or '1987' -> years with a finished year-end chart."""
    lo, _, hi = part.partition("-")
    return range(max(int(lo), FIRST_YEAR), min(int(hi or lo), dt.date.today().year - 1) + 1)


def coverage(decade):
    ys = decade_years(decade)
    return f"{ys[0]}" if len(ys) == 1 else f"{ys[0]}-{ys[-1]}"


def parse_top(spec):
    """'1-10,11-20' -> [(1, 10), (11, 20)] percent ranges of the rank."""
    out = []
    for part in filter(None, (x.strip() for x in (spec or "").split(","))):
        lo, _, hi = part.partition("-")
        out.append((int(lo), int(hi or lo)))
    return out


def in_top(i, n, ranges):
    """Is rank i (1-based) of n inside one of the percent ranges? Top 10% = ranks 1..ceil(0.1 n)."""
    return any(math.ceil((lo - 1) / 100 * n) < i <= math.ceil(hi / 100 * n) for lo, hi in ranges)


def ranked(years, a, lib):
    """Songs of the years that pass the genre filter, best first, each with rank and pct within that cohort.
    Cut: --top percent ranges of the whole cohort (unowned songs count), else the top a.n; --owned keeps only
    songs in the library, applied after the cut so owning a song never shifts the others' ranges."""
    songs = list(entries(years).values())
    ok = genre_filter(a.genre)
    rows = []
    for s in songs:
        s.update(mb_song(s["title"], s["artist"], s["year"]))
        if a.genre and not ok(genres(s)):
            continue
        s["file"], _ = musicdb.match(lib, None, s.get("mbid"), s["artist"], s["title"], any_copy=True)
        rows.append(s)
    pop = lb_popularity([s.get("mbid") for s in rows])
    for s in rows:
        s["listens"] = pop.get(s.get("mbid"), 0)
    # every year's #1 gets 100 points, so ties are common: break them by ListenBrainz listens
    # chart rank = the song's best single year; summing years would favour songs that charted in two years
    # over bigger one-year hits. The sum only breaks ties, then ListenBrainz listens.
    key = ((lambda s: (-s["listens"], s["best"], s["title"])) if a.rank == "listens"
           else (lambda s: (-s["peak"], -s["points"], -s["listens"], s["title"])))
    rows = sorted(rows, key=key)
    for i, s in enumerate(rows, 1):
        s["rank"], s["pct"], s["cohort"] = i, round(100 * i / len(rows), 1), len(rows)
    # hidden songs stay in the cohort (ranks and Top % of the others never move) and are removed after the cut
    hidden = hidden_set()
    for s in rows:
        s["hidden"] = hide_key(s["artist"], s["title"]) in hidden
    top = parse_top(a.top)
    if top:
        rows = [s for s in rows if in_top(s["rank"], len(rows), top)]
    else:
        rows = rows[: a.n] if not a.owned else rows
    rows = [s for s in rows if (s["file"] or not a.owned) and (a.show_hidden or not s["hidden"])]
    return rows if top or not a.owned else rows[: a.n]


def likes_rows(years, a, lib, plays, last):
    """Source "likes": library songs with rmpc's like sticker = 2, not a chart. Ranked by own plays (--sort plays)
    or by "rediscover" (liked, played a lot, not lately); decade/genre filters still apply, Top % is within the set."""
    from mpd import MPDClient
    c = MPDClient(); c.connect(os.environ.get("MPD_HOST", "localhost"), int(os.environ.get("MPD_PORT", 6600)))
    liked = [x["file"] for x in c.sticker_find("song", "", "like") if x.get("sticker", "").endswith("=2")]
    ok, wanted, rows = genre_filter(a.genre), set(years or []), []
    now = dt.datetime.now()
    for f in liked:
        tags = (c.find("file", f) or [{}])[0]
        one = lambda k: (tags.get(k)[0] if isinstance(tags.get(k), list) else tags.get(k)) or ""
        year = int(one("date")[:4]) if one("date")[:4].isdigit() else None
        if wanted and year not in wanted:
            continue
        artist_mbid = one("musicbrainz_artistid")
        s = {"artist": one("artist") or f, "title": one("title") or pathlib.Path(f).stem, "file": f, "year": year or 0,
             "years": [year] if year else [], "mbid": one("musicbrainz_trackid") or None, "artist_mbid": artist_mbid or None,
             "tags": [], "points": plays.get(f, 0), "peak": plays.get(f, 0), "listens": 0, "hidden": False}
        if a.genre and not ok(genres(s)):
            continue
        idle_days = (now - dt.datetime.fromisoformat(last[f])).days if last.get(f) else 3650
        s["score"] = math.log1p(s["points"]) * min(idle_days, 365) if a.sort == "rediscover" else s["points"]
        rows.append(s)
    rows.sort(key=lambda s: (-s["score"], s["artist"], s["title"]))
    for i, s in enumerate(rows, 1):
        s["rank"], s["pct"], s["cohort"] = i, round(100 * i / len(rows), 1), len(rows)
    top = parse_top(a.top)
    return [s for s in rows if in_top(s["rank"], len(rows), top)] if top else rows[: a.n]


RECS_SEEDS = 8  # most played artists used as seeds
RECS_SIMILAR = 8  # similar artists per seed (LB Radio)


def recs_seeds(lib, plays):
    """Artists I play most (likes count extra), as (artist MBID, name, weight): plays per artist MBID tag."""
    from mpd import MPDClient
    c = MPDClient(); c.connect(os.environ.get("MPD_HOST", "localhost"), int(os.environ.get("MPD_PORT", 6600)))
    liked = {x["file"] for x in c.sticker_find("song", "", "like") if x.get("sticker", "").endswith("=2")}
    weight, names = {}, {}
    for t in c.listallinfo():
        f, mbid = t.get("file"), t.get("musicbrainz_artistid")
        if not f or not mbid:
            continue
        mbid = (mbid[0] if isinstance(mbid, list) else mbid).split("/")[0].strip()
        weight[mbid] = weight.get(mbid, 0) + plays.get(f, 0) + (5 if f in liked else 0)
        names.setdefault(mbid, musicdb.one(t.get("artist", "")))
    top = sorted((w, m) for m, w in weight.items() if w > 0)[::-1][:RECS_SEEDS]
    return [(m, names[m], w) for w, m in top]


def lb_radio(artist_mbid):
    """LB Radio's similar-artist recordings for one seed, cached per day (the radio is shuffled per call):
    {similar artist mbid: [{recording_mbid, similar_artist_name, total_listen_count}]} or {} on failure."""
    f = CACHE / "recs" / f"radio-{artist_mbid}-{dt.date.today()}.json"
    if f.exists():
        return json.loads(f.read_text())
    q = urllib.parse.urlencode({"mode": "easy", "max_similar_artists": RECS_SIMILAR, "max_recordings_per_artist": 3,
                                "pop_begin": 0, "pop_end": 100})
    r = mbtag.http(f"https://api.listenbrainz.org/1/lb-radio/artist/{artist_mbid}?{q}", host_interval=0.5, attempts=2, timeout=15)
    if not isinstance(r, dict):
        print(f"warning: LB Radio failed for {artist_mbid}", file=sys.stderr)
        return {}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(r))
    return r


def lb_metadata(mbids):
    """Names, credits and tags of recordings from ListenBrainz, 50 per call, cached for good (a recording's name
    and credit rarely change): {mbid: metadata}. A slow or failing call skips those rows with a warning."""
    f = CACHE / "recs" / "metadata.json"
    cache = json.loads(f.read_text()) if f.exists() else {}
    todo = [m for m in mbids if m not in cache]
    for i in range(0, len(todo), 50):
        q = urllib.parse.urlencode({"recording_mbids": ",".join(todo[i:i + 50]), "inc": "artist tag"})
        # 2 tries x 15 s: the metadata endpoint sometimes hangs, and rormpc waits for this run
        r = mbtag.http(f"https://api.listenbrainz.org/1/metadata/recording/?{q}", host_interval=0.5, attempts=2, timeout=15)
        if r is None:
            print(f"warning: ListenBrainz metadata unavailable, {len(todo) - i} recommendations skipped", file=sys.stderr)
            break
        cache |= r
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False))
    tmp.replace(f)
    return {m: cache[m] for m in mbids if m in cache}


def recs_rows(a, lib, plays):
    """Source "recs": recordings of artists similar to the ones I play most (ListenBrainz Radio), not owned and not
    hidden. Each row says which seeds led to it; more seeds pointing at a recording rank it higher, then the seeds
    take turns, each with its most listened recordings first. No year (LB has none for these); genre filter on recording tags, else artist tags."""
    seeds = recs_seeds(lib, plays)
    found = {}
    for seed_mbid, seed_name, w in seeds:
        recs = [r for similar, rs in lb_radio(seed_mbid).items() if similar != seed_mbid for r in rs]  # not the seed's own
        recs.sort(key=lambda r: -(r.get("total_listen_count") or 0))
        for turn, r in enumerate(recs):
            x = found.setdefault(r["recording_mbid"], {"seeds": [], "weight": 0, "turn": turn,
                                                       "listens": r.get("total_listen_count") or 0})
            if seed_name not in x["seeds"]:
                x["seeds"].append(seed_name)
                x["weight"] += w
                x["turn"] = min(x["turn"], turn)
    meta = lb_metadata(list(found))
    ok, hidden, rows = genre_filter(a.genre), hidden_set(), []
    for mbid, x in found.items():
        m = meta.get(mbid)
        if not m:
            continue
        artist, title = m["artist"]["name"], m["recording"]["name"]
        if musicdb.match(lib, None, mbid, artist, title, any_copy=True)[0]:
            continue
        key = hide_key(artist, title)
        if key in hidden and not a.show_hidden:
            continue
        rec_tags = [t["tag"] for t in m.get("tag", {}).get("recording", []) if t.get("count", 0) >= 2]
        art_tags = sorted(m.get("tag", {}).get("artist", []), key=lambda t: -t.get("count", 0))
        tags = rec_tags or [t["tag"] for t in art_tags if t.get("genre_mbid")][:5]
        if a.genre and not ok(tags):
            continue
        rows.append({"artist": artist, "title": title, "file": None, "year": 0, "years": [], "mbid": mbid,
                     "tags": tags, "points": x["weight"], "peak": len(x["seeds"]), "listens": x["listens"], "turn": x["turn"],
                     "hidden": key in hidden, "reason": "similar to " + ", ".join(x["seeds"][:3])})
    # seeds take turns (each one's most listened first), so the most played artist doesn't fill the whole list
    rows.sort(key=lambda s: (-s["peak"], s["turn"], -s["points"], s["artist"]))
    for i, s in enumerate(rows, 1):
        s["rank"], s["pct"], s["cohort"] = i, round(100 * i / len(rows), 1), len(rows)
    top = parse_top(a.top)
    return [s for s in rows if in_top(s["rank"], len(rows), top)] if top else rows[: a.n]


def show(a):
    lib = musicdb.library()
    plays, *_ = musicdb.counted(musicdb.db(), lib)
    if a.years:
        years = sorted({y for part in a.years.split(",") if part.strip()
                        for y in range_years(part.strip())})
        groups = [(a.years, years)]
    elif a.source in ("likes", "recs") and not a.decade:
        groups = [("all years", [])]
    else:
        groups = [(d, decade_years(d)) for d in (DECADES if a.decade == "all" else [a.decade or sys.exit("give a decade or --years")])]
    period = a.years or a.decade or "all years"
    label = (f"Hits {period}" + (f" {a.genre}" if a.genre else "") + (f" top {a.top}%" if a.top else f" top{a.n}")
             + (" per decade" if a.decade == "all" and not a.years else "") + (" owned" if a.owned else "")
             + (" by listens" if a.rank == "listens" else ""))
    rows = []
    if a.source == "likes":
        plays_last = musicdb.counted(musicdb.db(), lib)
        plays, last = plays_last[0], plays_last[1]
        label = label.replace("Hits ", "Likes ", 1) + (" · by plays" if a.sort == "plays" else " · rediscover")
    if a.source == "recs":
        label = label.replace("Hits ", "Recommendations ", 1) + " · LB Radio, similar to your most played artists"
    print(f"# {label}   ✓ = in library, plays = your play count")
    for d, years in groups:
        part = (likes_rows(years, a, lib, plays, last) if a.source == "likes" else recs_rows(a, lib, plays)
                if a.source == "recs" else ranked(years, a, lib))
        if len(groups) > 1:
            print(f"\n## {d} (Billboard year-end {coverage(d)}): {sum(1 for s in part if s['file'])}/{len(part)} in library")
        print_rows(part, plays, a)
        rows += part
    have = [s for s in rows if s["file"]]
    print(f"\n{len(have)}/{len(rows)} in library")
    if a.json:
        write_json(a, label, rows, plays)
    if a.playlist:
        PLAYLISTS.mkdir(parents=True, exist_ok=True)
        p = PLAYLISTS / (re.sub(r'[/\\:*?"<>|]', "", label) + ".m3u")
        p.write_text("".join(s["file"] + "\n" for s in have))  # ordered by decade, then rank
        print(f"playlist: {p.stem} ({len(have)} songs)")
    if a.download:
        missing = [s for s in rows if not s["file"]]
        for s in missing:
            q = f"ytsearch1:{main_artist(s['artist'])} - {s['title']} official audio"
            print(f"\n==> {s['artist']} - {s['title']}")
            subprocess.run([sys.executable, "-m", "rormpc_tools.yt_mp3_mb", "--yes", "-d", f"Hits/{s['year'] // 10 * 10}s", q, "--", "--no-playlist"])
        if missing and a.playlist:
            print("re-run with --playlist after the MPD update to include the new files")


HIDDEN = settings.DATA_DIR / "hits-hidden.jsonl"


def hide_key(artist, title):
    """Chart-song identity for hiding: main artist + full title, normalised but keeping version notes
    ("(remix)", "(live)") so hiding one version does not hide the others. Year and chart position are not
    part of it, so a hide applies to every period."""
    return f"{mbtag.norm(main_artist(artist))}|{mbtag.norm(title)}"


def hidden_set():
    """Fold the hide/unhide event log into {key: event}."""
    state = {}
    if HIDDEN.exists():
        for line in HIDDEN.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                if e["action"] == "hide":
                    state[e["key"]] = e
                else:
                    state.pop(e["key"], None)
    return state


def hide_cmd(argv):
    """hits hide|unhide --artist A --title T [--mbid M];  hits hidden [--json]"""
    ap = argparse.ArgumentParser(prog=f"hits {argv[0]}")
    ap.add_argument("cmd")
    if argv[0] == "hidden":
        ap.add_argument("--json", action="store_true")
        a = ap.parse_args(argv)
        rows = list(hidden_set().values())
        if a.json:
            print(json.dumps(rows, ensure_ascii=False))
        for r in ([] if a.json else rows):
            print(f"{r['artist']} - {r['title']}  (hidden {r['ts'][:10]})")
        return
    ap.add_argument("--artist", required=True); ap.add_argument("--title", required=True); ap.add_argument("--mbid")
    a = ap.parse_args(argv)
    e = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "action": argv[0], "key": hide_key(a.artist, a.title),
         "artist": a.artist, "title": a.title, "mbid": a.mbid}
    HIDDEN.parent.mkdir(parents=True, exist_ok=True)
    with HIDDEN.open("a") as fh:  # an append-only log: the history of decisions stays reviewable in git
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"{argv[0]}: {a.artist} - {a.title}")


def write_json(a, label, rows, plays):
    """Versioned result file for rormpc's Hits pane, written atomically (the pane may read it any time)."""
    out = {"version": 1, "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "label": label,
           "args": {"period": a.years or a.decade, "top": a.top, "genre": a.genre, "owned": a.owned, "rank": a.rank,
                    "show_hidden": a.show_hidden, "source": a.source, "sort": a.sort},
           "rank_note": ("more of your most played artists point to it; then they take turns" if a.source == "recs"
                         else "rank by your plays among liked songs" if a.source == "likes" and a.sort == "plays"
                         else "liked, often played, not lately" if a.source == "likes"
                         else "best year-end chart position within the chosen years and genres"),
           "rows": [{"rank": s["rank"], "pct": s["pct"], "cohort": s["cohort"], "artist": s["artist"], "title": s["title"],
                     "year": min(s["years"], default=0), "years": s["years"], "points": s["points"], "peak": s["peak"],
                     "listens": s.get("listens", 0),
                     "genres": genres(s)[:5], "mbid": s.get("mbid"), "file": s["file"], "hidden": s.get("hidden", False),
                     "plays": plays.get(s["file"], 0) if s["file"] else 0, "reason": s.get("reason")} for s in rows]}
    path = pathlib.Path(a.json).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    tmp.replace(path)
    print(f"json: {path}")


def print_rows(rows, plays, a):
    for i, s in enumerate(rows, 1):
        have = "✓" if s["file"] else " "
        k = plays.get(s["file"], 0) if s["file"] else 0
        score = f"{s['listens']:>9,}" if a.rank == "listens" else f"{s['peak']:>4}"
        g = ", ".join(genres(s)[:3])
        print(f"{s.get('rank', i):4d}.{'h' if s.get('hidden') else ' '}{have} {k or '':>4} {score}  {s['artist']} - {s['title']}  ({min(s['years'], default='?')}; {g})"
              + (f"  [{s['reason']}]" if s.get("reason") else ""))


def prefetch(a):
    lo, _, hi = a.years.partition("-")
    years = range(max(int(lo), FIRST_YEAR), int(hi or lo) + 1)
    for y in years:
        es = entries([y]).values()
        for s in es:
            genres(s | mb_song(s["title"], s["artist"], y))
        print(y, len(es), flush=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        from . import fetch
        return fetch.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("hide", "unhide", "hidden"):
        return hide_cmd(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "prefetch":
        ap = argparse.ArgumentParser(prog="hits prefetch")
        ap.add_argument("cmd"); ap.add_argument("years", nargs="?", default=f"{FIRST_YEAR}-2025")
        return prefetch(ap.parse_args())
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("decade", nargs="?", help='e.g. 1980s, 80s, 2010s, or "all" for top N of every decade')
    ap.add_argument("--years", help="year ranges instead of a decade, e.g. 1985-1992 or 1970-1979,1990-1999 (one pooled ranking)")
    ap.add_argument("--top", help='percent ranges of the ranking, e.g. "1-10" or "11-20,21-50" (instead of -n)')
    ap.add_argument("--json", metavar="PATH", help="also write the result as JSON (for rormpc's Hits pane)")
    ap.add_argument("--show-hidden", action="store_true", help="include songs hidden with `hits hide` (marked)")
    ap.add_argument("--source", choices=["billboard", "likes", "recs"], default="billboard",
                    help="billboard: US year-end charts; likes: your liked songs (rmpc like sticker); "
                         "recs: songs of artists similar to your most played ones (ListenBrainz Radio)")
    ap.add_argument("--sort", choices=["plays", "rediscover"], default="plays", help="order for --source likes")
    ap.add_argument("-n", type=int, default=100, help="how many (10/100/1000)")
    ap.add_argument("-g", "--genre", default="", help='e.g. "rock -country" or "hip hop, r&b"')
    ap.add_argument("--rank", choices=["chart", "listens"], default="chart")
    ap.add_argument("--owned", action="store_true", help="top N among the songs you have, not the overall top N")
    ap.add_argument("--playlist", action="store_true", help="write an MPD playlist of the songs you have")
    ap.add_argument("--download", action="store_true", help="download missing songs with yt-mp3-mb")
    show(ap.parse_args())


if __name__ == "__main__":
    main()
