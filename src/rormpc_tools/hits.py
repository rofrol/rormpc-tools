"""Top hits of a decade (Billboard Year-End Hot 100, 1959-), genre filter, MPD playlist of what you have.

  hits 1980s                                  # top 100 of the decade, all genres
  hits 1980s -n 10 -g "rock -country"         # include/exclude genres: a song matches "rock" if any of its
                                              # MusicBrainz genres contains the word (hard rock, pop rock, ...)
  hits 1990s -g "hip hop,r&b" --rank listens  # rank by ListenBrainz listen counts instead of chart points
  hits 1980s -g rock --playlist               # write MPD playlist "Hits 1980s rock top100" (songs in the library)
  hits 1980s -g rock --download               # yt-mp3-mb the missing ones into <music>/Hits/1980s (first hit, unverified)
  hits genres [pin|unpin GENRE]               # every genre of the library with counts; Hits checkboxes
  hits fetch --help                           # verified import queue for missing songs (rormpc: Fetch missing…)
  hits all -n 10 -g "+rock -thrash metal" --playlist   # top 10 of every decade, one playlist ordered by decade
  hits all -n 10 --owned --playlist           # the 10 biggest hits you have from each decade
  hits --years 1985-1992 --top 11-20 -g "+rock +pop -country"   # ranks 11-20% of that cohort
  hits 1980s --top 1-10 --artist "+Queen +Toto"   # their songs in the 1980s top 10% (ranks within the decade)
  hits 1980s --top 1-10 --json ~/.cache/rormpc/hits/current.json  # result file for rormpc's Hits pane
  hits prefetch 1959-2025                     # warm the caches (charts + MusicBrainz, ~1 request/s)
  hits --set +likes --rank rediscover        # your liked songs, often played but not lately
  hits 1980s --set +billboard --set +likes --set -playlists --top 1-10   # (Billboard ∪ Likes) − Playlists
  hits --years 1990-1999 --rank plays --years-of release --top 1-10     # my most played songs released then
  hits --set +recommended                     # recommendations: artists similar to your most played (LB Radio)
  hits --set +tag:God --set "-playlist:Road trip" --set "+list:80s party"   # named sets (hits_sets.py)
  hits sets [--json]                          # every tag list, MPD playlist, Live playlist and smart list as a set
  hits --source likes|library|mine|playlists|recs   # the old shorthands, mapped onto --set/--rank/--years-of
  hits hide --artist A --title T [--mbid M]   # hide a song from every Hits result (log in the data repo)
  hits except pin|exclude|remove --scope library|set:KIND[:NAME] --file PATH   # an exception to the rules (--help)
  hits exceptions [--json]                    # every pin and exclusion, the hides included
  hits lists [create|update|rename|duplicate|delete|export] (--help)   # smart lists: saved rules with a name
  hits --list "80s party"                     # run a smart list's rules (its list-scoped exceptions apply)
  hits --rules rules.json                     # run rules from a file (a smart list's "rules" object)
  hits unhide --artist A --title T; hits hidden [--json]   # undo / review

Selection = (union of + sets, or the whole library when no set is +) − (union of − sets) ∩ period ∩ genres ∩
artists ∩ Top % ∩ owned (hits_rules.py). Top % is cut in the rank's own population, so a song's rank never depends
on the sets or filters. `--set -KIND` may be written as is (it is read as `--set=-KIND`).
Rank "billboard" = the song's best year-end position in the chosen years (101 - position); points summed over
years only break ties. Genres come from the recording's
MusicBrainz genres/tags, falling back to the artist's. Library matching and play counts reuse musicdb.
Caches: ~/.cache/hits/.
"""
import argparse, collections, datetime as dt, hashlib, json, os, pathlib, re, subprocess, sys, tempfile, time, urllib.parse, urllib.request

from . import deleted, external, hits_exceptions, hits_rules, hits_sets, mbtag, musicdb, settings


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
    cache_db()  # imports the seed first: it brings the chart pages too
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

_cache_db = None


def cache_db():
    """One SQLite file for the MusicBrainz lookups and ListenBrainz popularity (it replaced ~1100 small JSON files
    that every run opened: 1.5 s of a 3 s Apply)."""
    global _cache_db
    if _cache_db is None:
        import sqlite3
        CACHE.mkdir(parents=True, exist_ok=True)
        _cache_db = sqlite3.connect(CACHE / "cache.sqlite3", isolation_level=None)
        _cache_db.execute("PRAGMA journal_mode=WAL")
        _cache_db.execute("CREATE TABLE IF NOT EXISTS mb (name TEXT PRIMARY KEY, json TEXT)")
        _cache_db.execute("CREATE TABLE IF NOT EXISTS lb_pop (mbid TEXT PRIMARY KEY, listens INTEGER, fetched REAL)")
        _cache_db.execute("CREATE TABLE IF NOT EXISTS health (service TEXT PRIMARY KEY, down_until REAL)")
        _cache_db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        import_seed(_cache_db)
    return _cache_db


SEED = pathlib.Path(__file__).with_name("data") / "hits-seed.jsonl.gz"


def import_seed(db):
    """Fill an empty or older cache from the seed shipped with the package (the chosen MusicBrainz match of every
    chart entry, artist genres, ListenBrainz popularity), so a new install ranks every year at once instead of
    spending ~17 min of MusicBrainz requests per decade. Once per seed file; it only adds what the cache lacks,
    never overwrites (a local re-match or a newer fetch stays)."""
    if not SEED.exists():
        return
    import gzip, hashlib
    rev = hashlib.sha256(SEED.read_bytes()).hexdigest()[:16]
    if (db.execute("SELECT value FROM meta WHERE key = 'seed'").fetchone() or [None])[0] == rev:
        return
    with gzip.open(SEED, "rt") as fh, db:
        for line in fh:
            r = json.loads(line)
            if r["t"] == "mb":
                db.execute("INSERT OR IGNORE INTO mb VALUES (?, ?)", (r["k"], json.dumps(r["v"], ensure_ascii=False)))
            elif r["t"] == "lb":
                db.execute("INSERT OR IGNORE INTO lb_pop VALUES (?, ?, ?)", (r["k"], r["v"], r["at"]))
            elif r["t"] == "chart":
                f = CACHE / f"chart-{r['k']}.json"
                if not f.exists():
                    f.write_text(json.dumps(r["v"], ensure_ascii=False))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('seed', ?)", (rev,))


def export_seed(out=SEED):
    """`hits seed`: write the seed from this cache: the matches of the current MATCH_VERSION, artist genres and
    ListenBrainz popularity; not the raw search results (they stay local, for re-matching)."""
    import gzip
    db = cache_db()
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with gzip.open(out.with_suffix(".tmp"), "wt", compresslevel=9) as fh:
        for k, v in db.execute("SELECT name, json FROM mb WHERE name LIKE ? OR name LIKE 'a-%' ORDER BY name",
                               (f"m{MATCH_VERSION}-%",)):
            fh.write(json.dumps({"t": "mb", "k": k, "v": json.loads(v)}, ensure_ascii=False) + "\n")
            n += 1
        for f in sorted(CACHE.glob("chart-*.json")):
            fh.write(json.dumps({"t": "chart", "k": f.stem[6:], "v": json.loads(f.read_text())}, ensure_ascii=False) + "\n")
            n += 1
        for k, v, at in db.execute("SELECT mbid, listens, fetched FROM lb_pop ORDER BY mbid"):
            fh.write(json.dumps({"t": "lb", "k": k, "v": v, "at": at}) + "\n")
            n += 1
    out.with_suffix(".tmp").replace(out)
    print(f"{out}: {n} records, {out.stat().st_size / 1e6:.2f} MB")


def cached(name, fn):
    """A MusicBrainz lookup, computed once. Raw search results ("s-...", ~200 KB each) are kept compressed: they
    are only read again to re-match after a matching rule changes. Entries from the old per-file cache move into
    SQLite when first read."""
    import zlib
    db = cache_db()
    row = db.execute("SELECT json FROM mb WHERE name = ?", (name,)).fetchone()
    if row:
        v = row[0]
        return json.loads(zlib.decompress(v) if isinstance(v, bytes) else v)
    f = CACHE / "mb" / (re.sub(r"[^\w.-]", "_", name)[:180] + ".json")
    r = json.loads(f.read_text()) if f.exists() else fn()
    text = json.dumps(r, ensure_ascii=False)
    db.execute("INSERT OR REPLACE INTO mb VALUES (?, ?)",
               (name, zlib.compress(text.encode(), 6) if name.startswith("s-") else text))
    return r


def compact_cache():
    """Compress the raw search results already stored as text and give the space back (`hits compact`)."""
    import zlib
    db = cache_db()
    rows = db.execute("SELECT name, json FROM mb WHERE name LIKE 's-%' AND typeof(json) = 'text'").fetchall()
    with db:
        for k, v in rows:
            db.execute("UPDATE mb SET json = ? WHERE name = ?", (zlib.compress(v.encode(), 6), k))
    db.execute("VACUUM")
    print(f"compressed {len(rows)} search results; {(CACHE / 'cache.sqlite3').stat().st_size / 1e6:.1f} MB")


main_artist = mbtag.main_artist


MATCH_VERSION = 1  # bump when _mb_song's choice changes: cached choices of the old rule are not reused


def mb_song(title, artist, year):
    """Best MB recording for a chart entry: {mbid, artist_mbid, first, tags} or {}. The choice is cached (parsing
    the search result and fuzzy-matching it again took most of a warm run)."""
    return cached(f"m{MATCH_VERSION}-{artist}-{title}-{year}", lambda: _mb_song(title, artist, year))


def _mb_song(title, artist, year):
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
    from .genres import with_tag_genres
    r = cached(f"a-{mbid}", lambda: mbtag.http(f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=genres+tags&fmt=json") or {})
    return top_tags(with_tag_genres(r.get("genres", []), r.get("tags", []), artist=True)) or top_tags(r.get("tags", []))


def genres(song):
    """Recording tags only when they are backed by several votes, else the artist's main genres; for library
    songs with my additions/exclusions (`musicdb genre`) applied."""
    from .tags import effective_genres
    base = song.get("tags") or artist_genres(song.get("artist_mbid"))
    return effective_genres(base, song["file"], song.get("mbid"), MANUAL_GENRES()) if song.get("file") else base


_manual = None


def MANUAL_GENRES():
    """musicdb genre's log, read once per run."""
    global _manual
    if _manual is None:
        from .tags import manual_genres
        _manual = manual_genres()
    return _manual


LB_POP_TTL_D = 30  # popularity moves slowly; it only breaks ties in the chart ranking
# recordings per request: 25 answer in ~0.3 s, 100 took 4.6 s (2026-10-06), longer than the 2.5 s timeout
LB_POP_BATCH = 25
LB_DOWN_MIN = 20  # after a failed request, ListenBrainz is not asked again for this long (no waiting on every Apply)


def lb_popularity(mbids, cached_only=False):
    """ListenBrainz listen counts, only a tie-breaker. Cached per MBID for LB_POP_TTL_D days; only missing or
    stale ones are fetched, with one attempt and a short timeout; when LB fails it is left alone for LB_DOWN_MIN
    minutes and the ranking uses what the cache has (a timeout never becomes "0 listens"). cached_only: what the
    cache has, nothing fetched."""
    db, now = cache_db(), time.time()
    mbids = sorted({m for m in mbids if m})
    out, missing = {}, []
    for i in range(0, len(mbids), 500):
        part = mbids[i:i + 500]
        rows = db.execute(f"SELECT mbid, listens, fetched FROM lb_pop WHERE mbid IN ({','.join('?' * len(part))})",
                          part).fetchall()
        got = {m: (n, t) for m, n, t in rows}
        for m in part:
            if m in got:
                out[m] = got[m][0]
            if m not in got or now - got[m][1] > LB_POP_TTL_D * 86400:
                missing.append(m)
    down = db.execute("SELECT down_until FROM health WHERE service = 'lb_pop'").fetchone()
    if cached_only or (missing and down and down[0] > now):
        return out
    for i in range(0, len(missing), LB_POP_BATCH):
        r = mbtag.http("https://api.listenbrainz.org/1/popularity/recording", host_interval=0.5,
                       data=json.dumps({"recording_mbids": missing[i:i + LB_POP_BATCH]}).encode(),
                       headers={"Content-Type": "application/json"}, attempts=1, timeout=2.5)
        if r is None:
            db.execute("INSERT OR REPLACE INTO health VALUES ('lb_pop', ?)", (now + LB_DOWN_MIN * 60,))
            print(f"warning: ListenBrainz popularity unavailable; using the cache, not asking again for "
                  f"{LB_DOWN_MIN} min", file=sys.stderr)
            break
        for x in r:
            n = x.get("total_listen_count") or 0
            out[x["recording_mbid"]] = n
            db.execute("INSERT OR REPLACE INTO lb_pop VALUES (?, ?, ?)", (x["recording_mbid"], n, now))
    return out


# ---------------------------------------------------------------- genre filter

def genre_filter(spec):
    """'+rock -thrash metal, pop' -> predicate over a list of genre names.
    Included genres are ORed, excluded ones win; word match, so 'rock' hits 'hard rock'. '+' is optional."""
    from .genres import load_pins
    aliases = load_pins()["aliases"]
    def spellings(t):  # "rap" also finds "hip hop" and "hip-hop" (aliases in hits-genres.json)
        canon = aliases.get(t, t)
        return {canon} | {k for k, v in aliases.items() if v == canon}
    inc, exc = [], []
    for tok in re.findall(r"[-+]?[^,\s][^,]*?(?=\s+[-+]|,|$)", spec or ""):
        tok = tok.strip()
        (exc if tok.startswith("-") else inc).extend(spellings(tok.lstrip("-+").strip().lower()))
    word = lambda g, t: re.search(rf"(?<![\w&]){re.escape(t)}(?![\w&])", g.lower())
    def ok(gs):
        if any(word(g, t) for g in gs for t in exc):
            return False
        return not inc or any(word(g, t) for g in gs for t in inc)
    return ok


# ---------------------------------------------------------------- artist filter

# separators inside an artist credit: "A feat. B", "A & B", "A and B", "A x B", "A vs. B", "A/B"; not a comma
# ("Earth, Wind & Fire"; the whole credit always matches too)
CREDIT_SPLIT = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with|x|and|vs\.?)\s+|\s*[&/]\s*|\s*\((?:feat\.?|ft\.?)\s*",
                          re.I)


def fold(name):
    """Lowercase, diacritics removed ("Tiësto" = "tiesto", "Łódź" = "lodz"), spaces collapsed."""
    import unicodedata
    name = name.lower().replace("ł", "l").replace("ø", "o").replace("ß", "ss")
    name = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return " ".join(name.replace(")", " ").split())


def credit_members(credit):
    """Each artist named in a credit, as written: "Rihanna feat. Calvin Harris" -> ["Rihanna", "Calvin Harris"]."""
    return [m.strip(" )") for m in CREDIT_SPLIT.split(credit or "") if m.strip(" )")]


def artist_filter(spec):
    """'+Queen -Madonna, Toto' -> predicate over an artist credit. Included artists are ORed, excluded ones win;
    a name matches the whole credit or any artist in it, exactly after folding case and diacritics ("Tiesto"
    matches "Tiësto"; "Queen" does not match "Queen Latifah")."""
    inc, exc = set(), set()
    # rormpc separates names with ";" (a name may hold a comma: "Earth, Wind & Fire"); by hand "," works too
    toks = spec.split(";") if ";" in (spec or "") else re.findall(r"[-+]?[^,\s][^,]*?(?=\s+[-+]|,|$)", spec or "")
    for tok in filter(None, (t.strip() for t in toks)):
        tok = tok.strip()
        (exc if tok.startswith("-") else inc).add(fold(tok.lstrip("-+").strip()))
    def ok(credit):
        names = {fold(credit)} | {fold(m) for m in credit_members(credit)}
        if names & exc:
            return False
        return not inc or bool(names & inc)
    return ok


def count_artists(rows):
    """{folded name: [display name, songs]} over the artists named in the rows' credits."""
    out = {}
    for s in rows:
        names = credit_members(s["artist"])
        # a duo ("Simon & Garfunkel") is an artist of its own too; a "feat." credit is not
        if len(names) > 1 and not re.search(r"\b(?:feat|featuring|ft)\b|\bx\b|\bvs\b", s["artist"], re.I):
            names.append(s["artist"])
        for m in names:
            out.setdefault(fold(m), [m, 0])[1] += 1
    return out


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


def year_bounds(part, first, last):
    """'1985-1992', '1987', or an open end ('-1991', '2000-') -> (lo, hi); an open end is first or last."""
    lo, dash, hi = part.strip().partition("-")
    lo = int(lo) if lo.strip() else first
    return lo, int(hi) if hi.strip() else last if dash else lo


def range_years(part):
    """'1985-1992', '1987', '-1991' or '2000-' -> years with a finished year-end chart."""
    lo, hi = year_bounds(part, FIRST_YEAR, dt.date.today().year - 1)
    return range(max(lo, FIRST_YEAR), min(hi, dt.date.today().year - 1) + 1)


def coverage(decade):
    ys = decade_years(decade)
    return f"{ys[0]}" if len(ys) == 1 else f"{ys[0]}-{ys[-1]}"


parse_top, in_top = hits_rules.parse_top, hits_rules.in_top


def library_songs():
    """Every library song's tags from MPD in one call: {file: song dict as the sources below use it}."""
    from mpd import MPDClient
    c = MPDClient(); c.connect(os.environ.get("MPD_HOST", "localhost"), int(os.environ.get("MPD_PORT", 6600)))
    liked = {x["file"] for x in c.sticker_find("song", "", "like") if x.get("sticker", "").endswith("=2")}
    out = {}
    for t in c.listallinfo():
        f = t.get("file")
        if not f:
            continue
        one = lambda k: (t.get(k)[0] if isinstance(t.get(k), list) else t.get(k)) or ""
        date = one("originaldate") or one("date")  # the song's original release (TDOR), else this file's date
        year = int(date[:4]) if date[:4].isdigit() else None
        out[f] = {"artist": one("artist") or f, "title": one("title") or pathlib.Path(f).stem, "file": f,
                  "year": year or 0, "years": [year] if year else [], "mbid": one("musicbrainz_trackid") or None,
                  "artist_mbid": one("musicbrainz_artistid") or None, "tags": [], "listens": 0, "hidden": False,
                  "liked": f in liked}
    return out


# playlists the tools write themselves (hits --playlist under each source's label, musicdb lb-playlists, sync's
# Skipped and Not finished, dedupe's whole-folder dumps, the smart lists' "Smart …" exports, which would feed a
# list on itself): not a choice of songs. "Tag …" playlists (my tags) and
# liveplaylist's .m3u (every song accepted by me) stay.
GENERATED_PLAYLISTS = ("Hits ", "My charts ", "Library ", "Likes ", "Recommendations ", "My playlists ", "LB ",
                       "Folder ", "Skipped", "Not finished", "Smart ")


def my_playlists():
    """Songs of my stored MPD playlists, the generated ones left out: ({file: [playlist names]}, [names left out]).
    Entries are mapped through musicdb.canon (a merged or moved file); a song on several playlists is one entry."""
    c, al = musicdb.mpd(), musicdb.aliases()
    files, skipped = {}, []
    for p in sorted(c.listplaylists(), key=lambda p: p["playlist"].lower()):
        name = p["playlist"]
        if name.startswith(GENERATED_PLAYLISTS):
            skipped.append(name)
            continue
        for f in c.listplaylist(name):
            names = files.setdefault(musicdb.canon(f, al), [])
            if name not in names:
                names.append(name)
    return files, skipped


def playlists_reason(names):
    return "on " + ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")


MINE_THIN = 30  # plays in the chosen listening years below which "my charts" says the data is thin


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


def recs_candidates(a, lib, plays):
    """Set "recommended": recordings of artists similar to the ones I play most (ListenBrainz Radio), not owned.
    Each says which seeds led to it; more seeds pointing at a recording come first, then the seeds take turns,
    each with its most listened recordings first (their `order`; they have no rank of their own). No year (LB has
    none for these); genres from the recording's tags, else the artist's."""
    from .genres import with_tag_genres
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
    hidden, rows = hidden_set(), []
    for mbid, x in found.items():
        m = meta.get(mbid)
        if not m:
            continue
        artist, title = m["artist"]["name"], m["recording"]["name"]
        if musicdb.match(lib, None, mbid, artist, title, any_copy=True)[0]:
            continue
        rec_tags = [t["tag"] for t in m.get("tag", {}).get("recording", []) if t.get("count", 0) >= 2]
        art_tags = m.get("tag", {}).get("artist", [])
        art_genres = sorted(with_tag_genres([t for t in art_tags if t.get("genre_mbid")], art_tags, artist=True), key=lambda t: -t.get("count", 0))
        tags = rec_tags or [t.get("name") or t["tag"] for t in art_genres][:5]
        rows.append({"key": f"rec:{mbid}", "artist": artist, "title": title, "file": None, "release": 0, "years": [],
                     "chart_years": [], "mbid": mbid, "tags": tags, "points": x["weight"], "peak": len(x["seeds"]),
                     "listens": x["listens"], "turn": x["turn"], "plays": 0,
                     "hidden": hide_key(artist, title) in hidden, "reason": "similar to " + ", ".join(x["seeds"][:3])})
    # seeds take turns (each one's most listened first), so the most played artist doesn't fill the whole list
    rows.sort(key=lambda s: (-s["peak"], s["turn"], -s["points"], s["artist"]))
    for i, s in enumerate(rows):
        s["order"] = (0, i)
    return rows


def song_order(c):
    """Order of unranked rows: recommendations keep theirs (0, i), every other song by artist and title."""
    return (1, fold(c["artist"]), fold(c["title"]))


def chart_years_for(rules, years):
    """The chart years to read: the period's when Years of is the chart year, else every finished chart (a song
    released in the period may chart in any year)."""
    if rules.years_of == "chart" and years:
        return years
    return list(range(FIRST_YEAR, dt.date.today().year))


def chart_candidates(cands, members, years, a, lib, plays):
    """Add the Billboard rows of the chart years: merged into the library song they match (the chart's artist and
    title shown, as in the chart), else a missing row "chart:<name key>". Hidden chart songs are marked."""
    r, hidden = a.rules, hidden_set()
    chart = []
    for s in entries(chart_years_for(r, years)).values():
        s.update(mb_song(s["title"], s["artist"], s["year"]))
        f, _ = musicdb.match(lib, None, s.get("mbid"), s["artist"], s["title"], any_copy=True)
        c = cands.get(f) if f else None
        if c is None:
            key = f or "chart:" + musicdb.name_key(s["artist"], s["title"])
            first = (s.get("first") or "")[:4]
            c = cands.setdefault(key, {"key": key, "file": f, "release": int(first) if first.isdigit() else 0,
                                       "chart_years": [], "plays": plays.get(f, 0) if f else 0, "liked": False,
                                       "artist_mbid": None, "mbid": None, "tags": []})
        # the chart's recording first: its listens break rank ties, and owning a copy must not move a rank
        c.update(artist=s["artist"], title=s["title"], mbid=s.get("mbid") or c.get("mbid"),
                 artist_mbid=c.get("artist_mbid") or s.get("artist_mbid"), tags=s.get("tags") or c.get("tags") or [],
                 chart_years=sorted(set(c["chart_years"]) | set(s["years"])), peak=max(c.get("peak", 0), s["peak"]),
                 points=c.get("points", 0) + s["points"], best=min(c.get("best", 101), s["best"]))
        c["order"] = song_order(c)
        c["hidden"] = hide_key(c["artist"], c["title"]) in hidden
        if "billboard" in members:
            members["billboard"].add(c["key"])
        chart.append(c)
    # ListenBrainz listens only break ties among ranked chart songs: asked for the rank's population only, the
    # other chart rows (Billboard as a set or a year axis) use what the cache has
    wanted = set(years or ())
    ranked = r.rank == "billboard"
    need = [c for c in chart if ranked and hits_rules.in_period(c, r.years_of, wanted)]
    pop = lb_popularity([c.get("mbid") for c in need])
    pop |= lb_popularity([c.get("mbid") for c in chart], cached_only=True)
    for c in chart:
        c["listens"] = pop.get(c.get("mbid"), 0)


SPLIT_VERSION = 1  # bump when the split's rule changes: a cached split of the old rule is not reused


def split_cache():
    return CACHE / "my-plays-split.json"


def split_key(lib):
    """What the play-history split is computed from: the play DB (and its WAL), the shuffle's auto log, the path
    logs event_file maps plays through (aliases, identity registry, version decisions) and the library's files.
    A change to any of them gives another key."""
    paths = [musicdb.DB, pathlib.Path(f"{musicdb.DB}-wal"), musicdb.auto_log(), musicdb.DATA / "aliases.jsonl",
             musicdb.DATA / "songs.jsonl", musicdb.DATA / "versions.jsonl"]
    stats = []
    for p in paths:
        try:
            st = p.stat()
            stats.append([str(p), st.st_mtime_ns, st.st_size])
        except FileNotFoundError:
            stats.append([str(p), None, None])
    files = sorted(musicdb.library_files(lib)) if lib else []
    return hashlib.sha256(json.dumps([SPLIT_VERSION, stats, files]).encode()).hexdigest()


def shuffle_split(a, lib):
    """Each library file's play timestamps split by who chose the song, gathered once per run: ({file: [ts] of my
    own plays}, {file: number of plays the weighted shuffle picked itself}). The shuffle's picks show what the
    algorithm chose, not me, so my plays leave them out with every Years of. Kept across runs in the cache under
    `split_key`: a run with the same play history reuses it instead of matching every play again."""
    if getattr(a, "own_plays", None) is None:
        key, path = split_key(lib), split_cache()
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        if saved.get("key") == key:
            a.own_plays, a.shuffle_picks = saved["own"], collections.Counter(saved["picked"])
            return a.own_plays, a.shuffle_picks
        stamps = {}
        musicdb.counted(musicdb.db(), lib, stamps)
        auto, own, picked = musicdb.auto_starts(), {}, collections.Counter()
        for f, ts_list in stamps.items():
            picks = auto.get(f, [])
            for t in ts_list:
                if any(p - 120 <= musicdb.ts_epoch(t) <= p + 1800 for p in picks):
                    picked[f] += 1
                else:
                    own.setdefault(f, []).append(t)
        a.own_plays, a.shuffle_picks = own, picked
        # atomic, and a name of its own: two runs at once never read a half-written split or share a temp file
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False) as fh:
            json.dump({"key": key, "own": own, "picked": picked}, fh)
        os.replace(fh.name, path)
    return a.own_plays, a.shuffle_picks


def my_plays(cands, a, lib, plays):
    """Each library candidate's plays without the shuffle's own picks: candidate["mine"] (the rank's count; the
    Plays column keeps every play) and candidate["listened"] = Counter({listening year: my plays})."""
    own, picked = shuffle_split(a, lib)
    for f, c in cands.items():
        if c.get("file") == f:
            c["mine"] = max(0, plays.get(f, 0) - picked.get(f, 0))
            c["listened"] = collections.Counter(int(t[:4]) for t in own.get(f, ()))


def candidates(years, a, lib, plays, last):
    """Every song the rules may select, {key: candidate}, and each set's members {set: {key}}. Library songs are
    keyed by file, chart songs without a file "chart:<name key>", recommendations "rec:<mbid>"."""
    r = a.rules
    if getattr(a, "songs", None) is None:
        a.songs = library_songs()
    now = dt.datetime.now()
    cands = {}
    for f, s in a.songs.items():
        idle = (now - dt.datetime.fromisoformat(last[f])).days if last.get(f) else 3650
        c = cands[f] = dict(s, key=f, release=s["year"], chart_years=[], plays=plays.get(f, 0), idle_days=idle)
        c["order"] = song_order(c)
    members = {k: set() for k in r.sets}
    if "likes" in members:
        members["likes"] = {f for f, s in a.songs.items() if s["liked"]}
    if "playlists" in members:
        on, a.playlists_skipped = my_playlists()
        a.playlists_used = len({n for names in on.values() for n in names})
        for f, names in on.items():
            if f in cands:  # a stream or a file MPD no longer has is not a song to select
                cands[f]["reason"] = playlists_reason(names)
                members["playlists"].add(f)
    mine = r.years_of == "listened" or r.rank in ("plays", "rediscover")
    if mine:
        my_plays(cands, a, lib, plays)
    if r.rank == "billboard" or "billboard" in members or r.years_of == "chart":
        chart_candidates(cands, members, years, a, lib, plays)
        if mine:  # a chart row matched to a copy outside the library's songs
            my_plays({k: c for k, c in cands.items() if "mine" not in c}, a, lib, plays)
    if "recommended" in members:
        for c in recs_candidates(a, lib, plays):
            cands[c["key"]] = c
            members["recommended"].add(c["key"])
    # the named sets last: a smart list's members may be chart rows or recommendations of these candidates
    for k in r.sets:
        if ":" in k:
            members[k] = hits_sets.members(k, cands, a)
    return cands, members


def period_years(part, rules):
    """'1985-1992', '1987', '-1991' or '2000-' -> years on the rules' axis: finished year-end charts for the chart
    year, the year in progress included for listening and release years (an open start is year 1: undated songs
    stay out)."""
    if rules.years_of == "chart":
        return list(range_years(part))
    lo, hi = year_bounds(part, 1, dt.date.today().year)
    return list(range(lo, hi + 1))


def decade_axis_years(d, rules):
    ys = decade_years(d)
    return ys if rules.years_of == "chart" else list(range(ys[0] - ys[0] % 10, ys[0] - ys[0] % 10 + 10))


def make_label(a, period):
    """The result's name (status line, playlist name): an old source keeps its old label, any other combination
    of sets is "Hits <period> <formula>"."""
    r, legacy = a.rules, hits_rules.legacy_source(a.rules)
    head = {"likes": "Likes ", "library": "Library ", "playlists": "My playlists ", "mine": "My charts ",
            "recs": "Recommendations "}.get(legacy, "Hits ")
    label = (f"{head}{period}" + (f" {a.genre}" if a.genre else "") + (f" {a.artist}" if a.artist else "")
             + (f" top {a.top}%" if a.top_ranges else f" top{a.n}" if a.n else "")
             + (" per decade" if a.decade == "all" and not a.years else "") + (" owned" if a.owned else "")
             + (" by listens" if r.order == "listens" else ""))
    if legacy == "mine":
        return label + " · listening years, my plays (the shuffle's picks left out)"
    if legacy in ("likes", "library", "playlists"):
        return label + (" · by plays" if r.rank == "plays" else " · rediscover")
    if legacy == "recs":
        return label + " · LB Radio, similar to your most played artists"
    if legacy == "billboard":
        return label
    return label + f" · {a.formula}"


def load_rules(a):
    """--list REF / --rules FILE: the stored rules become the filter options (refused next to filter options:
    one source of rules). --list also opens the list, so its own exceptions apply."""
    from . import smartlists
    given = [opt for opt, value, default in FILTER_OPTIONS if getattr(a, value) != default]
    if given:
        raise ValueError(f"--list/--rules carry the rules: drop {', '.join(given)}")
    if a.list:
        lst = smartlists.find(a.list)
        if lst["blocked"]:
            raise ValueError(f"smart list {lst['name']!r}: {lst['blocked']}")
        rules, a.open_list = lst["rules"], lst["id"]
    else:
        data = json.loads(pathlib.Path(a.rules_file).expanduser().read_text())
        rules = data.get("rules", data) if isinstance(data, dict) else data
        problem = smartlists.problem(rules)
        if problem:
            raise ValueError(f"{a.rules_file}: {smartlists.NEWER} ({problem})")
    smartlists.to_options(rules, a)


# the filter options --list/--rules replace: (option, namespace attribute, parser default)
FILTER_OPTIONS = [("decade", "decade", None), ("--years", "years", None), ("--top", "top", None),
                  ("--set", "set", None), ("--rank", "rank", None), ("--years-of", "years_of", None),
                  ("--source", "source", None), ("--genre", "genre", ""), ("--artist", "artist", ""),
                  ("--owned", "owned", False)]


def show(a):
    """Run the rules, print the rows (and write --json, --playlist, --download); returns the rows. A rules error
    exits with its message; inside another run (a smart list used as a set, `a.nested`) it raises SetError."""
    try:
        return _show(a)
    except hits_sets.SetError as err:
        if getattr(a, "nested", False):
            raise
        hits_rules.fail(err)


def _show(a):
    try:
        if getattr(a, "list", None) or getattr(a, "rules_file", None):
            load_rules(a)
        a.rules = hits_rules.resolve(a.source, a.set, a.rank, a.years_of, a.sort)
        a.rules.sets = hits_sets.canonical_sets(a.rules.sets)
        a.top_ranges = hits_rules.top_for(a.rules, a.top)
    except (ValueError, LookupError, OSError) as err:
        raise hits_sets.SetError(str(err)) from None
    if getattr(a, "open_list", None) and not getattr(a, "list_stack", None):
        a.list_stack = [a.open_list]  # the open list may not use itself as a set
    r = a.rules
    r.list = getattr(a, "open_list", None)  # the open smart list: its exceptions (scope list:ID) apply
    if r.rank == "none" and a.top:
        a.n = 0  # "1-100" with no rank: every row
    lib = musicdb.library()
    plays, last, *_ = musicdb.counted(musicdb.db(), lib)
    if a.years:
        groups = [(a.years, sorted({y for part in a.years.split(",") if part.strip()
                                    for y in period_years(part, r)}))]
    elif a.decade:
        groups = [(d, decade_axis_years(d, r)) for d in (DECADES if a.decade == "all" else [a.decade])]
    elif r.rank == "billboard" and r.years_of == "chart":
        sys.exit("give a decade or --years")
    else:
        groups = [("all years", [])]
    period = a.years or a.decade or "all years"
    a.cohort_artists = {}
    a.set_names = hits_sets.labels(r.sets)
    a.formula = hits_rules.formula(r, period=a.years or a.decade, top=a.top_ranges, genre=a.genre,
                                   artist=a.artist, owned=a.owned, names=a.set_names)
    label = make_label(a, period)
    print(f"# {label}   ✓ = in library, plays = your play count")
    rows, a.candidates, a.cohort, a.mine_plays = [], 0, 0, 0
    a.pinned = a.excluded = 0
    genre_ok, artist_ok = genre_filter(a.genre), artist_filter(a.artist)
    exceptions = hits_exceptions.active()
    blocks = deleted.Blocks()
    for gi, (d, years) in enumerate(groups):
        cands, members = candidates(years, a, lib, plays, last)
        # hides are exclusions now: select keeps them, the exceptions below take them out after the Top % cut
        part, info = hits_rules.select(
            cands, members, r, wanted=years, top=a.top_ranges, owned=a.owned, show_hidden=True, n=a.n,
            artist_ok=(lambda c: artist_ok(c["artist"])) if a.artist else (lambda c: True),
            genre_ok=(lambda c: genre_ok(genres(c))) if a.genre else (lambda c: True))
        # pins once, in the last group (one list per decade would repeat them)
        part, xinfo = hits_rules.apply_exceptions(
            part, cands, r, hits_exceptions.by_candidate(cands, exceptions, hide_key),
            show_excluded=a.show_excluded, pins=gi == len(groups) - 1)
        a.pinned += xinfo["pinned"]
        a.excluded += xinfo["excluded"]
        a.candidates += info["candidates"]
        a.cohort += info["cohort"]
        if r.rank == "plays" and r.years_of == "listened":
            a.mine_plays += sum(hits_rules.score(cands[k], r, set(years)) for k in cands if cands[k].get("listened"))
        # the artist picker lists the artists of the whole selection before the Top % cut
        for k, (name, n) in count_artists(info["pool"]).items():
            a.cohort_artists.setdefault(k, [name, 0])[1] += n
        for s in part:  # a missing song deleted before is shown as deleted, never offered for download as missing
            e = None if s["file"] else blocks.chart_row(s.get("mbid"), s["artist"], s["title"])
            s["deleted"] = deleted.mark(e) if e else None
        if len(groups) > 1:
            print(f"\n## {d}: {sum(1 for s in part if s['file'])}/{len(part)} in library")
        print_rows(part, plays, a)
        rows += part
    have = [s for s in rows if s["file"]]
    a.summary = hits_rules.summary(a.formula, len(rows), a.candidates, a.pinned, a.excluded)
    print(f"\n{len(have)}/{len(rows)} in library · {a.summary}")
    if a.json:
        write_json(a, label, rows, plays)
    if a.playlist:
        PLAYLISTS.mkdir(parents=True, exist_ok=True)
        p = PLAYLISTS / (re.sub(r'[/\\:*?"<>|]', "", label) + ".m3u")
        p.write_text("".join(s["file"] + "\n" for s in have))  # ordered by decade, then rank
        print(f"playlist: {p.stem} ({len(have)} songs)")
    if a.download:
        missing = [s for s in rows if not s["file"]]
        for s in [s for s in missing if s["deleted"]]:
            print(f"\n--- {s['artist']} - {s['title']}: {deleted.reason(blocks.chart_row(s.get('mbid'), s['artist'], s['title']))}")
        for s in [s for s in missing if not s["deleted"]]:
            q = f"ytsearch1:{main_artist(s['artist'])} - {s['title']} official audio"
            print(f"\n==> {s['artist']} - {s['title']}")
            year = min(s["chart_years"], default=s.get("release") or 0)
            subprocess.run([sys.executable, "-m", "rormpc_tools.yt_mp3_mb", "--yes", "-d", f"Hits/{year // 10 * 10}s", q, "--", "--no-playlist"])
        if missing and a.playlist:
            print("re-run with --playlist after the MPD update to include the new files")
    return rows


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


def row_years(s):
    """The years a row lists (the details' "Year-end charts"): its chart years, else its release year."""
    return s.get("chart_years") or ([s["release"]] if s.get("release") else [])


def row_year(s, rules):
    """The Year column: the first chart year when Years of is the chart year, else the release year."""
    if rules.years_of == "chart":
        return min(s.get("chart_years") or [s.get("release") or 0])
    return s.get("release") or min(s.get("chart_years") or [0])


def rank_note(a):
    """The details line under a row's rank: what the rank means for these rules."""
    r = a.rules
    if r.rank == "billboard":
        note = "best year-end chart position within the chosen years" + (", by ListenBrainz listens" if r.order else "")
    elif r.rank == "plays" and r.years_of == "listened":
        note = (f"rank by my plays in these listening years ({a.mine_plays} plays"
                + (": thin data, a ranking of few plays" if a.mine_plays < MINE_THIN else "") + ")")
    elif r.rank == "plays":
        note = "rank by your plays among all library songs (the shuffle's own picks left out)"
    elif r.rank == "rediscover":
        note = "library songs often played (the shuffle's own picks left out), not lately"
    elif r.sets.get("recommended", 0) > 0:
        note = "not ranked; recommendations: more of your most played artists point to it; then they take turns"
    else:
        note = "not ranked (Rank by none), so no Top %"
    if r.sets.get("playlists", 0) > 0:
        note += "; " + playlists_note(a)
    return note


def write_json(a, label, rows, plays):
    """Versioned result file for rormpc's Hits pane, written atomically (the pane may read it any time). Version
    1 gained fields only: "rules", "formula", "summary", "counts", args.sets/years_of, rows' "ranked" and "sets";
    then (exceptions) counts.pinned/excluded, args.show_excluded, rows' "pinned", "excluded", "exceptions",
    "song_id" and "chart_key"; then rows' "deleted" ({id, deleted_at, file} of a missing song deleted before, else
    null: not to be downloaded again, deleted.py); then (smart lists) args.open_list and args.open_list_name; then (named sets)
    args.set_names {"tag:God": "Tag God", "list:ID": "Smart 80s party", ...}
    (rormpc's hits.rs parses a copy of this shape in its tests; tests/test_rormpc_contract.py checks this side)."""
    r = a.rules
    owned = sum(1 for s in rows if s["file"])
    out = {"version": 1, "generated_at": dt.datetime.now().isoformat(timespec="seconds"), "label": label,
           "artists": [{"name": name, "songs": n} for name, n in
                       sorted(a.cohort_artists.values(), key=lambda x: (-x[1], fold(x[0])))],
           "args": {"period": a.years or a.decade, "top": a.top, "genre": a.genre, "artist": a.artist,
                    "owned": a.owned, "rank": r.rank, "years_of": r.years_of,
                    "sets": [("+" if v > 0 else "-") + k for k, v in r.sets.items()],
                    "set_names": {k: v for k, v in a.set_names.items() if ":" in k},
                    "show_hidden": a.show_excluded, "show_excluded": a.show_excluded, "source": r.source,
                    "sort": a.sort, "open_list": r.list, "open_list_name": open_list_name(r.list)},
           "rules": hits_rules.as_dict(r) | {"period": a.years or a.decade, "top": a.top if a.top_ranges else None,
                                              "genre": a.genre, "artist": a.artist, "owned": a.owned},
           "formula": a.formula, "summary": a.summary,
           "counts": {"selected": len(rows), "owned": owned, "candidates": a.candidates, "cohort": a.cohort,
                      "pinned": a.pinned, "excluded": a.excluded},
           "rank_note": rank_note(a),
           "rows": [{"rank": s["rank"], "pct": s["pct"], "cohort": s["cohort"], "ranked": s["ranked"],
                     "artist": s["artist"], "title": s["title"], "year": row_year(s, r), "years": row_years(s),
                     "points": s.get("points", s.get("plays", 0)), "peak": s.get("peak", s.get("plays", 0)),
                     "listens": s.get("listens", 0), "sets": s.get("sets", []),
                     "genres": genres(s)[:5], "mbid": s.get("mbid"), "file": s["file"], "hidden": s.get("hidden", False),
                     "plays": plays.get(s["file"], 0) if s["file"] else 0, "reason": s.get("reason"),
                     "pinned": s.get("pinned", False), "excluded": s.get("excluded", False),
                     "exceptions": [{k: e.get(k) for k in ("id", "action", "scope", "applies", "via")}
                                    for e in s.get("exceptions", [])],
                     "song_id": hits_exceptions.id_of_file(s["file"]) if s["file"] else None,
                     "chart_key": hide_key(s["artist"], s["title"]), "deleted": s.get("deleted")} for s in rows]}
    path = pathlib.Path(a.json).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    tmp.replace(path)
    print(f"json: {path}")


def open_list_name(list_id):
    """The open smart list's name (None when none is open or it is gone)."""
    if not list_id:
        return None
    from . import smartlists
    lst = smartlists.fold().get(list_id)
    return lst["name"] if lst else None


def playlists_note(a):
    """The details line of set "playlists": how many playlists, and which were left out as generated."""
    used = getattr(a, "playlists_used", 0)
    head = f"the songs of your {used} playlist{'s' if used != 1 else ''}"
    left = sorted({next(p for p in GENERATED_PLAYLISTS if n.startswith(p)).strip()
                   for n in getattr(a, "playlists_skipped", [])})
    return head + (f"; generated ones left out: {', '.join(left)}" if left else "")


def print_rows(rows, plays, a):
    for s in rows:
        have = "✓" if s["file"] else "⌫" if s.get("deleted") else " "
        k = plays.get(s["file"], 0) if s["file"] else 0
        score = f"{s.get('listens', 0):>9,}" if a.rules.order == "listens" else f"{s.get('peak', s.get('plays', 0)):>4}"
        g = ", ".join(genres(s)[:3])
        rank = f"{s['rank']:4d}." if s["ranked"] else "   —."
        flag = "⊘" if s.get("excluded") else "✚" if s.get("pinned") else " "
        print(f"{rank}{flag}{have} {k or '':>4} {score}  {s['artist']} - {s['title']}  ({min(row_years(s), default='?')}; {g})"
              + (f"  [{s['reason']}]" if s.get("reason") else "")
              + (f"  [deleted {s['deleted']['deleted_at'][:10]}]" if s.get("deleted") else ""))


def prefetch(a):
    """Fill the cache for chart years. With --budget N it stops after N songs that needed MusicBrainz (and
    --max-seconds): the hourly `musicdb update` calls it so a new chart year or a cache gap fills in slowly in the
    background, then refreshes a little of the stale ListenBrainz popularity."""
    lo, _, hi = a.years.partition("-")
    years = range(max(int(lo), FIRST_YEAR), int(hi or lo) + 1)
    db, started, fetched = cache_db(), time.time(), 0
    budget = getattr(a, "budget", None)
    for y in years:
        es = entries([y]).values()
        for s in es:
            key = f"m{MATCH_VERSION}-{s['artist']}-{s['title']}-{y}"
            if budget is not None and not db.execute("SELECT 1 FROM mb WHERE name = ?", (key,)).fetchone():
                if fetched >= budget or time.time() - started > a.max_seconds:
                    print(f"budget used: {fetched} songs looked up", flush=True)
                    return
                fetched += 1
            genres(s | mb_song(s["title"], s["artist"], y))
        if budget is None:
            print(y, len(es), flush=True)
    if budget is not None:
        # a little stale or missing popularity per run (200 recordings, 8 short requests)
        stale = [m for (m,) in db.execute("SELECT mbid FROM lb_pop WHERE fetched < ? ORDER BY fetched LIMIT 200",
                                          (time.time() - LB_POP_TTL_D * 86400,))]
        missing = [json.loads(v).get("mbid") for (v,) in db.execute(
            "SELECT json FROM mb WHERE name LIKE ? AND json NOT LIKE '{}'", (f"m{MATCH_VERSION}-%",))]
        have = {m for (m,) in db.execute("SELECT mbid FROM lb_pop")}
        todo = stale + [m for m in missing if m and m not in have][:200 - len(stale)]
        if todo:
            lb_popularity(todo)
        print(f"{fetched} songs looked up, {len(todo)} popularity checked", flush=True)


@external.cli
def main():
    if sys.argv[1:] == ["--version"]:
        print(f"hits {settings.version()}")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "genres":
        from . import genres
        return genres.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        from . import fetch
        return fetch.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] in ("hide", "unhide", "hidden"):
        return hide_cmd(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "except":
        return hits_exceptions.except_cmd(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "exceptions":
        return hits_exceptions.list_cmd(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "lists":
        from . import smartlists
        return smartlists.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "sets":
        return hits_sets.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "prefetch":
        ap = argparse.ArgumentParser(prog="hits prefetch")
        ap.add_argument("cmd"); ap.add_argument("years", nargs="?", default=f"{FIRST_YEAR}-{dt.date.today().year - 1}")
        ap.add_argument("--budget", type=int, help="stop after this many songs that need MusicBrainz (background use)")
        ap.add_argument("--max-seconds", type=float, default=60)
        return prefetch(ap.parse_args())
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        return export_seed()
    if len(sys.argv) > 1 and sys.argv[1] == "compact":
        return compact_cache()
    show(parser().parse_args(set_argv(sys.argv[1:])))


def set_argv(argv):
    """"--set -likes" (or "-tag:Rock: live"): argparse would read it as an option, so it becomes "--set=-likes"."""
    out, rest = [], iter(argv)
    for x in rest:
        nxt = next(rest, None) if x == "--set" else None
        out += [f"--set={nxt}"] if nxt is not None else [x]
    return out


def parser(prog=None):
    """hits' options (the result, its rules and its outputs); `hits lists create|update` reads the same."""
    ap = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("decade", nargs="?", help='e.g. 1980s, 80s, 2010s, or "all" for top N of every decade')
    ap.add_argument("--years", help="year ranges instead of a decade, e.g. 1985-1992 or 1970-1979,1990-1999 (one pooled ranking)")
    ap.add_argument("--top", help='percent ranges of the ranking, e.g. "1-10" or "11-20,21-50" (instead of -n)')
    ap.add_argument("--json", metavar="PATH", help="also write the result as JSON (for rormpc's Hits pane)")
    ap.add_argument("--show-excluded", "--show-hidden", dest="show_excluded", action="store_true",
                    help="keep the excluded songs (`hits except exclude`, `hits hide`) in the result, marked ⊘")
    ap.add_argument("--set", action="append", metavar="±KIND",
                    help="a set chip, repeatable: +KIND includes, -KIND excludes; KIND: billboard (US year-end "
                         "charts), likes (rmpc like sticker), playlists (all your MPD playlists except the generated "
                         "ones: hits --playlist's, LB …, Folder …, Skipped, Not finished), recommended (songs of artists "
                         "similar to your most played ones, ListenBrainz Radio); named sets: tag:NAME, playlist:NAME, "
                         "live:ID, list:ID|NAME (hits sets lists them; one per --set, a name may hold commas). "
                         "Selection = (union of + sets, or the "
                         "whole library when none is +) - (union of - sets) ∩ period ∩ genres ∩ artists ∩ Top %% ∩ owned")
    ap.add_argument("--rank", choices=["billboard", "plays", "rediscover", "none", "chart", "listens"],
                    help="billboard: best year-end position; plays: your plays (the shuffle's own picks left out); "
                         "rediscover: often played, not lately; none: no ranking, no Top %%. Top %% is cut in the rank's own population (the chart songs or "
                         "the library songs of the period), before sets, genres and artists. Default: billboard when "
                         "+billboard, none for +recommended alone, else plays. (chart = billboard; listens = the "
                         "Billboard cohort by ListenBrainz listens)")
    ap.add_argument("--years-of", choices=["release", "chart", "listened"],
                    help="which years the period means; default follows --rank: billboard -> chart, plays -> "
                         "listened, else release")
    ap.add_argument("--source", choices=["billboard", "likes", "recs", "library", "mine", "playlists"],
                    help="the old shorthand: billboard = --set +billboard --rank billboard --years-of chart; "
                         "likes/library/playlists = --set +likes/(none)/+playlists --rank <--sort> --years-of release; "
                         "mine = --rank plays --years-of listened; recs = --set +recommended --rank none")
    ap.add_argument("--sort", choices=["plays", "rediscover"], default="plays",
                    help="the rank for --source likes, library and playlists")
    ap.add_argument("-n", type=int, default=100, help="how many without --top (10/100/1000; 0 = all)")
    ap.add_argument("-g", "--genre", default="", help='e.g. "rock -country" or "hip hop, r&b"')
    ap.add_argument("--artist", default="", help='e.g. "+Queen -Madonna" or "Toto, Queen": artists of the credit '
                    '(feat., &, ...), case and diacritics ignored; applied after the Top %% cut')
    ap.add_argument("--owned", action="store_true", help="top N among the songs you have, not the overall top N")
    ap.add_argument("--playlist", action="store_true", help="write an MPD playlist of the songs you have")
    ap.add_argument("--download", action="store_true", help="download missing songs with yt-mp3-mb")
    ap.add_argument("--list", metavar="ID|NAME", help="run a smart list's rules (hits lists); its exceptions apply")
    ap.add_argument("--rules", dest="rules_file", metavar="FILE",
                    help="run rules from a JSON file: a smart list's rules object (or an object with \"rules\")")
    ap.add_argument("--open-list", metavar="ID",
                    help="the smart list open in rormpc's Play: exceptions scoped to it (list:ID) apply")
    return ap


if __name__ == "__main__":
    main()
