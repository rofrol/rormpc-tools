"""Personal play history for the MPD library -> MPD stickers that rmpc can show and sort by.

  musicdb import-mpdlog                 # MPD log "player: played" lines (skips < 30 s / half the song are dropped)
  musicdb import-lb                     # ListenBrainz listens (listenbrainz-mpd already applies a play threshold)
  musicdb import-takeout PATH           # Google Takeout zip/dir/watch-history.json (YouTube + YouTube Music)
  musicdb import-spotify PATH           # Spotify export zip/dir (Streaming_History_Audio_*.json, YourLibrary.json)
  musicdb import-favorites FILE         # text file, one "Artist - Title" per line (e.g. music.txt)
  musicdb import-local                  # listens ro-listenbrainz-mpd logged locally (counted once with their LB copy)
  musicdb import-skips                  # songs ro-listenbrainz-mpd saw left for another song before their end
  musicdb sync                          # match events to library files and write stickers and the "Skipped" playlist
  musicdb update                        # deletion retries + import-skips/local + import-lb + sync + export (hourly);
                                        # a failed import-lb is reported after the rest has run
  musicdb export                        # dump events/favorites as JSONL into the data repo and commit
  musicdb lb-import-spotify [--dry-run]  # send the imported Spotify plays to ListenBrainz (once)
  musicdb lb-playlists [--download]     # ListenBrainz recommendation playlists -> MPD playlists "LB <kind>"
  musicdb delete [--permanent] [--listenbrainz] [FILE...]  # rormpc Ctrl-x menu: Trash or remove; --listenbrainz
                                        # also deletes the history (LB listens: irreversible, YouTube playlists)
  musicdb delete --preview [--youtube] [FILE...]  # JSON: plays, LB listens, YouTube playlists; changes nothing
  musicdb undo                          # rmpc key: restore the most recently trashed song (repeatable)
  musicdb restore ID [--yes]            # any deletion back: file (Trash or downloaded again), stickers, history,
                                        # ListenBrainz listens, YouTube playlists; dry run without --yes
  musicdb keep|unkeep [FILE...]         # not a deletion candidate: drop it from the "Not finished" playlist
  musicdb tag add|remove|list|of ...   # hand-made lists (God, melancholic, ...); musicdb tag --help
  musicdb genre add|exclude|reset GENRE --current   # correct a song's MusicBrainz genres
  musicdb chart [--bucket month] [--open]  # HTML page: how my most played songs rose and fell
  musicdb identity sync|show FILE       # one id per library file (songs.jsonl + tags), follows renames
  musicdb versions --help               # which file a played track is, when several files share its name
  musicdb dedupe [--apply]              # one file per identical audio stream: state merged, copies quarantined
  musicdb doctor [--json] [-v]          # silent data errors: duplicate listens, songs in several files, paths
                                        # that no longer exist, plays credited to no file (read-only)
  musicdb lyrics --help             # lyrics from LRCLIB into lyrics_dir (rmpc's Lyrics pane)
  musicdb deletions [--json [--all]] [--retry]  # the deletion journal (--all adds finished permanent deletions);
                                                # --retry runs failed remote steps (update does it)
  musicdb deletions allow|block ID      # a deleted song may be downloaded again / is blocked again (deleted.py)
  musicdb top [-n 30]                   # most played library songs
  musicdb skips [-n 30]                 # songs skipped most since their last play
  musicdb missing [-n 50]               # played / liked songs that are not in the library (yt-mp3-mb candidates)

Stickers: playCount (plain integer), plays (space-padded to 5 chars, because rmpc sorts sticker text
lexicographically), lastPlayed (YYYY-MM-DD). Likes use rmpc's own "like" sticker (key r in rmpc: 2 like,
1 neutral, 0 dislike); skips (space-padded like plays) counts skips since the song's last play, and songs
with SKIPPED_MIN or more are listed in the MPD playlist "Skipped" to review (Ctrl-x deletes); sync sends changes to ListenBrainz as love / clear / hate, and liked songs from
favorites (Spotify library, music.txt) become like=2 when the song has no like yet.
MPD plays come from the MPD log before LB_CUTOFF and from ListenBrainz after it, so nothing is counted twice.
Source of truth: JSONL in data_dir (settings; committed when it is a git repository, keep it private);
db_file is a cache rebuilt from it when missing.
Needs sticker_file in mpd.conf.
"""
import argparse, collections, contextlib, datetime as dt, fcntl, json, os, pathlib, re, shutil, sqlite3, statistics, subprocess, sys, time, urllib.request, zipfile, zoneinfo

from . import external, identity, mbtag, settings

DB = settings.DB_FILE
DATA = settings.DATA_DIR
PUSH_EVERY_S = 20 * 3600
PLAYLISTS = settings.MPD_PLAYLISTS
MPD_LOG = settings.MPD_LOG
LB_CUTOFF = settings.LB_SINCE  # MPD log plays before it, ListenBrainz listens after it
SKIPS_LOG = settings.SKIPS_LOG
LISTENS_LOG = settings.LISTENS_LOG
# import-lb re-reads this much before the newest listen it has: the scrobbler's offline cache submits late
LB_OVERLAP_S = 7 * 86400
SKIPPED_MIN = 2  # skips since the last play that put a song into the "Skipped" playlist
YT_DEDUP_S = 300  # repeated Takeout entries of the same video within 5 min = one play
# zone of the stored (naive) timestamps; None: the system's. See settings.HISTORY_TZ.
TZ = zoneinfo.ZoneInfo(settings.HISTORY_TZ) if settings.HISTORY_TZ else None


def local_ts(when):
    """Stored timestamp ("2026-09-26T02:16:18", the history's zone) of a Unix time or an aware datetime."""
    d = dt.datetime.fromtimestamp(when, TZ) if isinstance(when, (int, float)) else when.astimezone(TZ)
    return d.replace(tzinfo=None).isoformat(timespec="seconds")


def aliases():
    """{old path: current path} from aliases.jsonl in data_dir: files merged into another copy (`musicdb dedupe`)
    or moved. History and logs keep the path they were written with; readers map it through canon()."""
    out = {r["old"]: r["new"] for r in jsonl(DATA / "aliases.jsonl")}
    # the identity registry knows every path a file had (renames by anything, not only by these tools)
    rows = identity.load()["rows"]
    for r in rows.values():
        now = r.get("path") if r.get("state") == "live" else (rows.get(r.get("into"), {}).get("path") if r.get("state") == "merged" else None)
        for p in r.get("paths", []):
            if now and p != now:
                out.setdefault(p, now)
    for old in out:  # follow chains (a -> b -> c)
        seen = {old}
        while out[old] in out and out[old] not in seen:
            seen.add(out[old]); out[old] = out[out[old]]
    return out


def canon(f, al=None):
    """The current path of a file that may since have been merged or moved."""
    return (aliases() if al is None else al).get(f, f)


def epoch_of(ts):
    """Unix time of a stored timestamp (the inverse of local_ts)."""
    d = dt.datetime.fromisoformat(ts)
    return (d.replace(tzinfo=TZ) if TZ else d).timestamp()

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  source TEXT, ts TEXT, ytid TEXT, mbid TEXT, spotify_uri TEXT, artist TEXT, title TEXT, ms_played INTEGER, extra TEXT,
  UNIQUE(source, ts, ytid, mbid, spotify_uri, artist, title));
CREATE TABLE IF NOT EXISTS favorites (
  source TEXT, artist TEXT, title TEXT, spotify_uri TEXT, ytid TEXT, UNIQUE(source, artist, title));
-- events of deleted songs: removed from `events`, kept here so re-imports cannot bring them back
CREATE TABLE IF NOT EXISTS tombstones (
  source TEXT, ts TEXT, ytid TEXT, mbid TEXT, spotify_uri TEXT, artist TEXT, title TEXT, ms_played INTEGER, extra TEXT,
  deleted_at TEXT, UNIQUE(source, ts, ytid, mbid, spotify_uri, artist, title));
-- songs left for another song before their end (ro-listenbrainz-mpd skips.jsonl); never sent anywhere
CREATE TABLE IF NOT EXISTS skips (ts TEXT, file TEXT, mbid TEXT, position_s REAL, duration_s REAL, run_s REAL, UNIQUE(ts, file));
-- what ListenBrainz was last told per recording, so likes are sent only when they change
CREATE TABLE IF NOT EXISTS lb_feedback (mbid TEXT PRIMARY KEY, score INTEGER, ts TEXT);
"""


EVENT_COLS = ["source", "ts", "ytid", "mbid", "spotify_uri", "artist", "title", "ms_played", "extra"]
FAV_COLS = ["source", "artist", "title", "spotify_uri", "ytid"]
TOMB_COLS = EVENT_COLS + ["deleted_at"]
SKIP_COLS = ["ts", "file", "mbid", "position_s", "duration_s", "run_s"]
TABLES = (("events", EVENT_COLS), ("favorites", FAV_COLS), ("tombstones", TOMB_COLS), ("skips", SKIP_COLS))
KEY = "source, ts, ytid, mbid, spotify_uri, artist, title"


def db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    fresh = not DB.exists()
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    if fresh:  # rebuild the cache from the data repo
        for table, cols in TABLES:
            f = DATA / f"{table}.jsonl"
            if f.exists():
                rows = [tuple(json.loads(l).get(k, "") for k in cols) for l in f.read_text().splitlines() if l]
                c.executemany(f"INSERT OR IGNORE INTO {table} VALUES ({','.join('?' * len(cols))})", rows)
        c.commit()
    return c


def export(_a):
    """Write sorted JSONL (diffable, one event per line), commit if it changed, push at most every PUSH_EVERY_S."""
    if not (DATA / ".git").exists():
        print(f"no data repo at {DATA}, skipping export"); return
    c = db()
    for table, cols in TABLES:
        rows = c.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY {','.join(cols)}").fetchall()
        (DATA / f"{table}.jsonl").write_text("".join(
            json.dumps({k: v for k, v in zip(cols, r) if v not in ("", None)}, ensure_ascii=False) + "\n" for r in rows))
    git = lambda *a: subprocess.run(["git", "-C", str(DATA), *a], capture_output=True, text=True)
    # the hand-written logs (tag lists, manual genres, hidden hits) are committed with the hourly export
    logs = [f for f in ("collections.jsonl", "genres.jsonl", "hits-hidden.jsonl", "not-finished-keep.jsonl", "likes.jsonl", "aliases.jsonl", "versions.jsonl", "songs.jsonl", "exceptions.jsonl", "smartlists.jsonl")
            if (DATA / f).exists()]
    git("add", "events.jsonl", "favorites.jsonl", "tombstones.jsonl", "skips.jsonl", "deletions", *logs)
    if git("diff", "--cached", "--quiet").returncode:
        n = git("diff", "--cached", "--numstat").stdout.split()[:1]
        git("commit", "-q", "-m", f"musicdb: +{n[0] if n else '?'} lines")
        print("committed")
    stamp = DATA / ".git" / "musicdb-last-push"
    ahead = git("rev-list", "--count", "@{u}..HEAD").stdout.strip()
    if ahead not in ("", "0") and (not stamp.exists() or time.time() - stamp.stat().st_mtime > PUSH_EVERY_S):
        if git("push", "-q").returncode == 0:
            stamp.touch()
            print("pushed")


def mpd():
    from mpd import MPDClient
    c = MPDClient()
    c.timeout = 60
    host = os.environ.get("MPD_HOST", "localhost")
    c.connect(host if host.startswith("/") else host, int(os.environ.get("MPD_PORT", 6600)))
    return c


def add_events(rows):
    c = db()
    before = c.total_changes
    # '' instead of NULL: SQLite never treats NULLs as equal, so UNIQUE would not dedupe re-imports
    # a re-import may fill `extra` (e.g. the ListenBrainz msid) of an event stored before it was recorded
    dead = set(c.execute(f"SELECT {KEY} FROM tombstones"))
    dead_lb = {(ts, a, t) for ts, a, t in c.execute("SELECT ts, artist, title FROM tombstones WHERE source = 'lb'")}
    rows = [r for r in rows if tuple("" if v is None else v for v in r[:7]) not in dead
            and not (r[0] == "lb" and (r[1], r[5] or "", r[6] or "") in dead_lb)]
    c.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(source, ts, ytid, mbid, spotify_uri, artist, title) DO UPDATE SET extra = excluded.extra "
                  "WHERE events.extra = '' AND excluded.extra != ''",
                  [tuple("" if v is None else v for v in r) for r in rows])
    merged = merge_lb_copies(c)
    c.commit()
    print(f"{c.total_changes - before - merged} new events ({len(rows)} read)" + (f", {merged} duplicate listens merged" if merged else ""))


def merge_lb_copies(c):
    """One ListenBrainz listen is one event. Its identity is (second, artist, title): the MBID is not, because
    ListenBrainz maps a listen to a recording later, so a re-read in the import overlap brings the same listen
    back with an MBID (and msid) the stored copy lacks, and the UNIQUE key saw two events. Keeps the copy with
    the most information, fills its missing fields from the others, deletes the rest. Returns rows changed."""
    n = 0
    groups = c.execute("SELECT ts, artist, title FROM events WHERE source = 'lb' "
                       "GROUP BY ts, artist, title HAVING count(*) > 1").fetchall()
    for ts, artist, title in groups:
        rows = c.execute("SELECT rowid, mbid, extra, ms_played FROM events WHERE source = 'lb' AND ts = ? AND artist = ? "
                         "AND title = ? ORDER BY (extra != '') DESC, (mbid != '') DESC, rowid", (ts, artist, title)).fetchall()
        keep, rest = rows[0], rows[1:]
        mbid = keep[1] or next((r[1] for r in rest if r[1]), "")
        extra = keep[2] or next((r[2] for r in rest if r[2]), "")
        ms = keep[3] if keep[3] not in ("", None) else next((r[3] for r in rest if r[3] not in ("", None)), "")
        c.executemany("DELETE FROM events WHERE rowid = ?", [(r[0],) for r in rest])
        c.execute("UPDATE events SET mbid = ?, extra = ?, ms_played = ? WHERE rowid = ?", (mbid, extra, ms, keep[0]))
        n += len(rest) + 1
    return n


def one(x):
    return x[0] if isinstance(x, list) else x


# ---------------------------------------------------------------- importers

def import_mpdlog(_a):
    if not LB_CUTOFF:
        sys.exit(f"set lb_since in {settings.CONFIG_FILE} (when your scrobbler started): MPD log plays after it are "
                 "ListenBrainz listens too and would count twice")
    durs = {}
    for s in mpd().listallinfo():
        if "file" in s and (y := identity.ytid(s["file"])):
            durs[y] = float(one(s.get("duration", 0)) or 0)
    lines = MPD_LOG.read_text(errors="replace").splitlines()
    # (second, file) of decode failures: MPD logs "played" for files it could not open, too
    failed = {(l[:19], re.search(r'Failed to decode "([^"]+)"', l).group(1).rsplit("/", 1)[-1]) for l in lines if "Failed to decode" in l}
    rows, prev = [], None
    for l in lines:
        m = re.match(r'(\S+) player: played "(.+)"$', l)
        if not m:
            continue
        ts, f = dt.datetime.fromisoformat(m.group(1)), m.group(2)
        gap = (ts - prev).total_seconds() if prev else 1e9
        prev = ts
        if m.group(1) >= LB_CUTOFF or (m.group(1), f.rsplit("/", 1)[-1]) in failed:
            continue
        y = identity.ytid(f)
        dur = durs.get(y, 0) if y else 0
        # the line is logged when the song ends; the previous one marks (roughly) when it started
        if gap < 30 or (dur and gap < min(dur / 2, 240)):
            continue
        rows.append(("mpd", m.group(1), y, None, None, None, f, None, None))
    add_events(rows)


def import_lb(_a):
    """Listens since LB_CUTOFF (pages are newest first, so stop at the first older one).
    Any failed or malformed page raises: a silent partial import would look like success."""
    cutoff = epoch_of(LB_CUTOFF) if LB_CUTOFF else 0
    newest = db().execute("SELECT max(ts) FROM events WHERE source = 'lb'").fetchone()[0]
    if newest:  # only what is new since the last import
        cutoff = max(cutoff, epoch_of(newest) - LB_OVERLAP_S)
    user = mbtag.lb_user()
    rows, max_ts = [], None
    while True:
        # small pages: with years of imported history LB times out on count=1000
        u = mbtag.lb_api(f"/1/user/{user}/listens?count=100") + (f"&max_ts={max_ts}" if max_ts else "")
        page = mbtag.http(u, host_interval=0.5, strict=True)
        ls = (page or {}).get("payload", {}).get("listens")
        if not isinstance(ls, list):
            raise RuntimeError(f"unexpected ListenBrainz response for {u}")
        for l in ls:
            if l["listened_at"] < cutoff:
                break
            tm = l["track_metadata"]
            ai = tm.get("additional_info") or {}
            if ai.get("submission_client") == "musicdb-spotify-import":
                continue  # already in the DB as source "spotify"
            mbid = ai.get("recording_mbid") or (tm.get("mbid_mapping") or {}).get("recording_mbid")
            ts = local_ts(l["listened_at"])
            # recording_msid identifies the listen on LB: needed to map or delete it later
            rows.append(("lb", ts, None, mbid, None, tm.get("artist_name"), tm.get("track_name"), ai.get("duration_ms"),
                         json.dumps({"msid": l.get("recording_msid")})))
        oldest = min((l["listened_at"] for l in ls), default=None)
        if oldest is None or oldest < cutoff or (max_ts is not None and oldest >= max_ts):
            break  # end of history, reached the cutoff, or no progress (max_ts is exclusive)
        max_ts = oldest
    add_events(rows)


def import_local(_a):
    """Listens logged by ro-listenbrainz-mpd when they count: plays without reading ListenBrainz back. The same
    listens come back from ListenBrainz with the same timestamp; counted() counts them once."""
    rows = []
    for r in jsonl(LISTENS_LOG):
        rows.append(("local", local_ts(r["ts"]), identity.ytid(r["file"]),
                     r.get("mbid"), None, None, None, None, json.dumps({"file": r["file"]})))
    add_events(rows)


PREV_MATCH_S = 2  # a skip this close to a logged Previous move of the same song is that move, not a skip


def prev_moves():
    """{file: [Unix times]} of songs mpd-player's shuffle left with Previous (its prev.jsonl): leaving a song that
    way is neutral, so the scrobbler's skip of it is not one. A move whose transition failed is left out."""
    p = pathlib.Path(os.environ.get("XDG_STATE_HOME") or settings.HOME / ".local/state") / "rormpc/prev.jsonl"
    rows = jsonl(p)
    failed = {r["cmd"] for r in rows if r.get("result") == "failed"}
    out = collections.defaultdict(list)
    for r in rows:
        if "t" in r and r.get("from") and r["cmd"] not in failed:
            out[r["from"]].append(float(r["t"]))
    return out


def import_skips(_a):
    moves = prev_moves()
    log = jsonl(SKIPS_LOG)
    kept = [r for r in log if not any(abs(float(r["ts"]) - t) <= PREV_MATCH_S for t in moves.get(r["file"], []))]
    rows = [(local_ts(r["ts"]), r["file"], r.get("mbid") or "", r.get("position_s"),
             r.get("duration_s"), r.get("run_s")) for r in kept]
    c = db()
    before = c.total_changes
    c.executemany("INSERT OR IGNORE INTO skips VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    print(f"{c.total_changes - before} new skips ({len(log)} read, {len(log) - len(kept)} were Previous)")


def skipped(c, last):
    """{file: skips since the file's last play}, only songs skipped at least once since then."""
    n, al = collections.Counter(), aliases()
    for ts, f in c.execute("SELECT ts, file FROM skips"):
        f = al.get(f, f)
        if ts > last.get(f, ""):
            n[f] += 1
    return n


def write_skipped_playlist(skips):
    files = [f for f, k in skips.most_common() if k >= SKIPPED_MIN]
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    tmp = PLAYLISTS / ".Skipped.m3u.tmp"
    tmp.write_text("".join(f + "\n" for f in files))
    tmp.replace(PLAYLISTS / "Skipped.m3u")  # atomic: MPD never reads a half-written playlist
    return len(files)


NF_DAYS, NF_MIN_S, NF_VISITS, NF_DAYS_SEEN, NF_RATIO = 180, 90, 4, 3, 0.2
NF_KEEP = DATA / "not-finished-keep.jsonl"


def not_finished(c, durations):
    """{file: reason} of deletion candidates: songs I rarely play to the end (TODO, consulted 2026-10-03).
    A visit is a counted listen (90 % without a seek, the scrobbler's local log) or a skip; in the last NF_DAYS days
    a song needs NF_VISITS visits on NF_DAYS_SEEN days, at most NF_RATIO of them finished, and most skips before half
    of the song without a seek. Songs shorter than NF_MIN_S (intros, skits) and never-played songs are no data.
    Likes and "keep" decisions are applied by the caller."""
    since = (dt.datetime.now() - dt.timedelta(days=NF_DAYS)).isoformat()
    done, skips, al = collections.defaultdict(list), collections.defaultdict(list), aliases()
    for ts, extra in c.execute("SELECT ts, extra FROM events WHERE source = 'local' AND ts >= ?", (since,)):
        f = canon(json.loads(extra or "{}").get("file"), al)
        if f:
            done[f].append(ts)
    for ts, f, pos, dur, run in c.execute("SELECT ts, file, position_s, duration_s, run_s FROM skips WHERE ts >= ?", (since,)):
        skips[canon(f, al)].append((ts, pos or 0, dur or durations.get(f, 0), run or 0))
    out = {}
    for f in set(done) | set(skips):
        dur = durations.get(f) or max((d for _, _, d, _ in skips[f]), default=0)
        visits = len(done[f]) + len(skips[f])
        days = {t[:10] for t in done[f]} | {t[:10] for t, *_ in skips[f]}
        if dur < NF_MIN_S or visits < NF_VISITS or len(days) < NF_DAYS_SEEN or len(done[f]) / visits > NF_RATIO:
            continue
        seeked = sum(1 for _, pos, _, run in skips[f] if pos - run > 5)  # jumped ahead: a favourite part, not dislike
        early = sum(1 for _, pos, d, run in skips[f] if pos - run <= 5 and pos < 0.5 * (d or dur))
        if early * 2 < len(skips[f]):
            continue
        out[f] = f"{len(done[f])}/{visits} finished · {early} early exits" + (f" · {seeked} seek-heavy" if seeked else "")
    return out


def kept():
    """Files I marked "keep" (not a deletion candidate) with `musicdb keep`; "unkeep" revokes."""
    state, al = {}, aliases()
    for e in jsonl(NF_KEEP):
        state[canon(e["file"], al)] = e["action"] == "keep"
    return {f for f, k in state.items() if k}


def keep_cmd(a):
    files = a.files or [subprocess.run(["mpc", "-f", "%file%", "current"], capture_output=True, text=True).stdout.strip()]
    if not all(files):
        sys.exit("nothing is playing")
    now = dt.datetime.now().isoformat(timespec="seconds")
    with open(NF_KEEP, "a") as fh:
        for f in files:
            fh.write(json.dumps({"ts": now, "action": a.cmd, "file": f}, ensure_ascii=False) + "\n")
    print(f"{a.cmd}: {', '.join(files)} (the Not finished playlist follows on the next sync)")


def write_playlist(name, files):
    PLAYLISTS.mkdir(parents=True, exist_ok=True)
    tmp = PLAYLISTS / f".{name}.m3u.tmp"
    tmp.write_text("".join(f + "\n" for f in files))
    tmp.replace(PLAYLISTS / f"{name}.m3u")  # atomic: MPD never reads a half-written playlist


def open_export(path, pattern):
    """Yield (name, bytes) of files matching pattern inside a zip or a directory tree."""
    p = pathlib.Path(path).expanduser()
    if p.is_file() and p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if re.search(pattern, n):
                    yield n, z.read(n)
    elif p.is_file():
        yield p.name, p.read_bytes()
    else:
        for f in sorted(p.rglob("*")):
            if f.is_file() and re.search(pattern, str(f)):
                yield str(f), f.read_bytes()
        for z in sorted(p.rglob("*.zip")):
            yield from open_export(z, pattern)


def import_takeout(a):
    rows, last = [], {}
    items = []
    for _, data in open_export(a.path, r"watch-history\.json$"):
        items += json.loads(data)
    items.sort(key=lambda x: x.get("time", ""))
    for it in items:
        m = re.search(r"[?&]v=([\w-]{11})", it.get("titleUrl", ""))
        if not m:
            continue
        ytid, ts = m.group(1), dt.datetime.fromisoformat(local_ts(dt.datetime.fromisoformat(it["time"].replace("Z", "+00:00"))))
        if ytid in last and (ts - last[ytid]).total_seconds() < YT_DEDUP_S:
            continue
        last[ytid] = ts
        title = re.sub(r"^Watched |^Obejrzano: ", "", it.get("title", ""))
        channel = (it.get("subtitles") or [{}])[0].get("name", "")
        rows.append(("yt-music" if it.get("header") == "YouTube Music" else "yt", ts.isoformat(timespec="seconds"),
                     ytid, None, None, channel, title, None, None))
    add_events(rows)


def import_spotify(a):
    rows, favs = [], []
    for _, data in open_export(a.path, r"(Streaming_History_Audio|StreamingHistory_music|StreamingHistory)\S*\.json$"):
        for e in json.loads(data):
            if "ts" in e:  # extended history
                if not e.get("master_metadata_track_name") or (e.get("ms_played") or 0) < 30000:
                    continue
                ts = dt.datetime.fromisoformat(local_ts(dt.datetime.fromisoformat(e["ts"].replace("Z", "+00:00"))))
                rows.append(("spotify", ts.isoformat(timespec="seconds"), None, None, e.get("spotify_track_uri"),
                             e.get("master_metadata_album_artist_name"), e["master_metadata_track_name"], e["ms_played"], None))
            elif e.get("msPlayed", 0) >= 30000:  # basic "StreamingHistory_music" export
                rows.append(("spotify", e["endTime"].replace(" ", "T") + ":00", None, None, None, e["artistName"],
                             e["trackName"], e["msPlayed"], None))
    for _, data in open_export(a.path, r"YourLibrary\.json$"):
        for t in json.loads(data).get("tracks", []):
            favs.append(("spotify", t.get("artist"), t.get("track"), t.get("uri"), None))
    add_events(rows)
    if favs:
        c = db()
        c.executemany("INSERT OR IGNORE INTO favorites VALUES (?,?,?,?,?)", [tuple(v or "" for v in r) for r in favs])
        c.commit()
        print(f"{len(favs)} liked tracks")


def import_favorites(a):
    favs = []
    for line in pathlib.Path(a.file).expanduser().read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        y = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", line)
        parts = re.split(r"\s+[-–—]\s+", re.sub(r"https?://\S+", "", line).strip(), maxsplit=1)
        artist, title = (parts + [""])[:2] if len(parts) == 2 else ("", parts[0])
        favs.append((pathlib.Path(a.file).name, artist, title, None, y.group(1) if y else None))
    c = db()
    c.executemany("INSERT OR IGNORE INTO favorites VALUES (?,?,?,?,?)", [tuple(v or "" for v in r) for r in favs])
    c.commit()
    print(f"{len(favs)} favorites")


# ---------------------------------------------------------------- matching

def library():
    by_yt, by_mbid, by_name = {}, {}, collections.defaultdict(set)
    for s in mpd().listallinfo():
        f = s.get("file")
        if not f:
            continue
        if y := identity.ytid(f):
            by_yt[y] = f
        if s.get("musicbrainz_trackid"):
            by_mbid[one(s["musicbrainz_trackid"])] = f
        a, t = one(s.get("artist", "")), one(s.get("title", ""))
        if a and t:
            by_name[name_key(a, t)].add(f)
    return by_yt, by_mbid, by_name


def name_key(artist, title):
    title = re.sub(r"\s*[\(\[].*?[\)\]]", "", title or "")  # "(radio edit)", "[Remastered]" -> same song
    # main artist before norm(): norm() drops "feat" and "&", so splitting after it never finds the main artist
    return f"{mbtag.norm(mbtag.main_artist(artist))}|{mbtag.norm(title)}"


def match(lib, ytid=None, mbid=None, artist=None, title=None, any_copy=False):
    """Library file for an event or chart entry. A name shared by several files stays unmatched (a play must
    not be credited to the wrong copy) unless any_copy: "do I have this song" is answered by any of them."""
    by_yt, by_mbid, by_name = lib
    if ytid and ytid in by_yt:
        return by_yt[ytid], "ytid"
    if mbid and mbid in by_mbid:
        return by_mbid[mbid], "mbid"
    if artist and title:
        hits = by_name.get(name_key(artist, title), set())
        if len(hits) == 1 or (hits and any_copy):
            return sorted(hits)[0], "name"
    return None, None


def library_files(lib):
    return set(lib[0].values()) | set(lib[1].values()) | {f for fs in lib[2].values() for f in fs}


ALIASES_CACHE = {}  # aliases() for event_file, refreshed by prepare()
RESOLVE = [None]  # versions.resolver() for event_file, refreshed by prepare()


def prepare(lib, files):
    """Load the path aliases and the version decisions event_file reads; call before a pass over events."""
    from . import versions
    ALIASES_CACHE.clear(); ALIASES_CACHE.update(aliases())
    RESOLVE[0] = versions.resolver(lib, files)


def event_file(lib, files, src, ytid, mbid, artist, title, extra, uri=None):
    """Library file of a play: the scrobbler's local log names it; then the YouTube id, the MBID, a decision
    for its track (`musicdb versions`), and last the name when exactly one file has it."""
    if src == "local":
        f = json.loads(extra or "{}").get("file")
        if (f := ALIASES_CACHE.get(f, f)) in files:
            return f
    f, _ = match(lib, ytid, mbid)
    if f:
        return f
    if artist and title and RESOLVE[0]:
        from . import versions
        d = RESOLVE[0](src, uri, mbid, artist, title)
        if d == versions.NOT_OWNED:
            return None
        if d:
            return d
    return match(lib, None, None, artist, title)[0]


def counted(c, lib, stamps=None):
    """{file: (count, last_ts)}, {file: favorite}, unmatched play keys, unmatched favorites. With `stamps` (a dict),
    it also gets each file's play timestamps."""
    plays, last, unmatched = collections.Counter(), {}, collections.Counter()
    files = library_files(lib)
    prepare(lib, files)
    local = {ts for (ts,) in c.execute("SELECT ts FROM events WHERE source = 'local'")}
    for src, ts, ytid, mbid, uri, artist, title, extra in c.execute(
            "SELECT source, ts, ytid, mbid, spotify_uri, artist, title, extra FROM events"):
        if src == "lb" and ts in local:
            continue  # the scrobbler's own listen, already counted from its local log
        f = event_file(lib, files, src, ytid, mbid, artist, title, extra, uri)
        if src == "yt" and not f:
            continue  # plain YouTube watches that are not in the library are mostly not music
        if f:
            plays[f] += 1
            last[f] = max(last.get(f, ""), ts)
            if stamps is not None:
                stamps.setdefault(f, []).append(ts)
        else:
            unmatched[(artist or "", title or "", ytid or "")] += 1
    favs, fav_missing = set(), []
    for src, artist, title, uri, ytid in c.execute("SELECT * FROM favorites"):
        f, _ = match(lib, ytid, None, artist, title)
        if f:
            favs.add(f)
        else:
            fav_missing.append((src, artist, title, ytid))
    return plays, last, favs, unmatched, fav_missing


LIKE_TO_LB = {"2": 1, "1": 0, "0": -1}  # rmpc like sticker -> LB feedback score (love / clear / hate)

# ---------------------------------------------------------------- data for mpd-player's weighted shuffle

CADENCE_MIN_D, CADENCE_MAX_D, CADENCE_DEFAULT_D = 2, 60, 14
SKIP_WINDOW_D = 180


def ts_epoch(ts):
    """Unix time of a stored timestamp (naive, the history's zone)."""
    d = dt.datetime.fromisoformat(ts[:19])
    return (d.replace(tzinfo=TZ) if TZ else d).timestamp()


def auto_log():
    """mpd-player's log of the songs its shuffle picked itself."""
    return pathlib.Path(os.environ.get("XDG_STATE_HOME") or settings.HOME / ".local/state") / "rormpc/auto.jsonl"


def auto_starts():
    """{file: [start times]} of the songs mpd-player's shuffle picked itself (its auto.jsonl): their plays are
    exposure, not preference, so they don't raise a song's weight."""
    p = auto_log()
    out, al = collections.defaultdict(list), aliases()
    for r in jsonl(p) if p.exists() else []:
        out[al.get(r["file"], r["file"])].append(float(r["start"]))
    return out


def shuffle_data(files, stamps, likes, durations, c):
    """Per file: plays (preference: the shuffle's own picks left out), heard (all), last (Unix time of the last
    play), cadence (median days between its preference plays, when it has 2+ gaps), liked/disliked, early/late
    (Unix times of skips in the last SKIP_WINDOW_D days; early = under min(30 s, 20% of the song))."""
    auto, al = auto_starts(), aliases()
    now = time.time()
    early, late = collections.defaultdict(list), collections.defaultdict(list)
    for ts, f, dur, run in c.execute("SELECT ts, file, duration_s, run_s FROM skips"):
        t = ts_epoch(ts)
        if now - t > SKIP_WINDOW_D * 86400:
            continue
        f = al.get(f, f)
        (early if (run or 0) < min(30, 0.2 * (dur or 150)) else late)[f].append(round(t))
    out, medians = {}, []
    for f in files:
        times = sorted({round(ts_epoch(t)) for t in stamps.get(f, [])})
        dur = durations.get(f) or 300
        own = [t for t in times if any(a - 120 <= t <= a + dur + 900 for a in auto.get(f, []))]
        pref = [t for t in times if t not in own]
        gaps = [(b - a) / 86400 for a, b in zip(pref, pref[1:]) if b - a > 600]
        cadence = None
        if len(gaps) >= 2:
            cadence = min(CADENCE_MAX_D, max(CADENCE_MIN_D, statistics.median(gaps)))
            medians.append(cadence)
        like = likes.get(f)
        out[f] = {"plays": len(pref), "heard": len(times), "last": times[-1] if times else None,
                  "cadence": cadence and round(cadence, 2), "liked": like == "2", "disliked": like == "0",
                  "early": early.get(f, []), "late": late.get(f, [])}
    return out, round(statistics.median(medians), 2) if medians else CADENCE_DEFAULT_D


def write_weights(data, global_cadence):
    """$XDG_STATE_HOME/rormpc/weights.json for mpd-player (musicdb is its only writer), atomically."""
    d = pathlib.Path(os.environ.get("XDG_STATE_HOME") or settings.HOME / ".local/state") / "rormpc"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".weights.json.tmp"
    tmp.write_text(json.dumps({"version": 2, "generated": time.time(), "global_cadence": global_cadence,
                               "files": data}))
    os.replace(tmp, d / "weights.json")


def sync(_a):
    c, lib, m = db(), library(), mpd()
    stamps = {}
    plays, last, favs, _, _ = counted(c, lib, stamps)
    like_of = {}
    files = library_files(lib)
    skips = skipped(c, last)
    durations = {s["file"]: float(one(s.get("duration", 0)) or 0) for s in m.listallinfo() if s.get("file")}
    unfinished, keep, candidates = not_finished(c, durations), kept(), []
    mbid_of = {f: mbid for mbid, f in lib[1].items()}
    n, likes = 0, collections.defaultdict(list)
    for f in files:
        k = plays.get(f, 0)
        want = {"playCount": str(k), "plays": f"{k:5d}" if k else "", "lastPlayed": last.get(f, "")[:10],
                "skips": f"{skips[f]:5d}" if skips[f] else "", "notFinished": ""}
        try:
            have = m.sticker_list("song", f)
        except Exception:  # MPD errors when a song has no stickers yet
            have = {}
        if "favorite" in have:  # old musicdb sticker, replaced by rmpc's own like sticker
            m.sticker_delete("song", f, "favorite")
        like = have.get("like")
        if like is None and (f in favs or have.get("favorite")):  # liked elsewhere: fills unrated songs only
            like = "2"
            m.sticker_set("song", f, "like", like)
            n += 1
        if like in LIKE_TO_LB and f in mbid_of:
            likes[mbid_of[f]].append(LIKE_TO_LB[like])
        like_of[f] = like
        if f in unfinished and like != "2" and f not in keep:
            want["notFinished"] = unfinished[f]
            candidates.append(f)
        for key, val in want.items():
            if have.get(key, "") == val:
                continue
            if val:
                m.sticker_set("song", f, key, val)
            elif key in have:
                m.sticker_delete("song", f, key)
            n += 1
    write_likes(m, files)
    write_weights(*shuffle_data(files, stamps, like_of, durations, c))
    skipped_n = write_skipped_playlist(collections.Counter({f: k for f, k in skips.items() if f in files}))
    write_playlist("Not finished", candidates)
    print(f"{len(files)} songs, {sum(1 for f in files if plays.get(f))} with plays, "
          f"{sum(1 for v in likes.values() if max(v) == 1)} liked, {n} sticker updates, "
          f"{skipped_n} in playlist Skipped, {len(candidates)} in Not finished")
    # the network last: a ListenBrainz failure must not cost the local results; unsent likes go next time
    if LB_PAUSED[0]:
        return
    sent = push_feedback(c, {mbid: max(scores) for mbid, scores in likes.items()})  # duplicates: a like wins
    print(f"{sent} LB feedback sent")


def write_likes(m, files):
    """Snapshot of rmpc's like stickers into the data repo. MPD keys stickers by path, so a move or rename of
    the library (or a lost sticker DB) would drop them silently; this keeps them with the song's identity
    (YouTube id, MBID) to put them back."""
    from . import tags
    rows = []
    for f in sorted(files):
        try:
            like = m.sticker_list("song", f).get("like")
        except Exception:
            continue
        if like is not None:
            rows.append(tags.song_ref(m, f) | {"like": like})
    if DATA.exists():
        write_jsonl(DATA / "likes.jsonl", rows)


def push_feedback(c, scores):
    """Send changed likes to ListenBrainz. A song with no like sticker sends nothing (it never clears LB)."""
    told = dict(c.execute("SELECT mbid, score FROM lb_feedback"))
    todo = {mbid: s for mbid, s in scores.items() if told.get(mbid, 0 if s == 0 else None) != s}
    token = mbtag.lb_token() if todo else None
    for mbid, score in todo.items():
        req = urllib.request.Request(mbtag.lb_api("/1/feedback/recording-feedback"), method="POST",
                                     data=json.dumps({"recording_mbid": mbid, "score": score}).encode(),
                                     headers={"Authorization": "Token " + token, "Content-Type": "application/json",
                                              "User-Agent": mbtag.UA})
        urllib.request.urlopen(req, timeout=30).read()
        c.execute("INSERT OR REPLACE INTO lb_feedback VALUES (?,?,?)", (mbid, score, dt.datetime.now().isoformat(timespec="seconds")))
        c.commit()
    return len(todo)


LB_BACKOFF = settings.XDG_CACHE / "rormpc-tools" / "lb-backoff.json"
LB_BACKOFF_MAX_S = 12 * 3600
LB_PAUSED = [False]  # set by update() while ListenBrainz is in backoff: sync then keeps likes for later


def lb_backoff():
    try:
        return json.loads(LB_BACKOFF.read_text())
    except (OSError, ValueError):
        return {"failures": 0, "next": 0}


def lb_backoff_record(ok):
    """After a run that reached ListenBrainz: reset, or wait 1, 2, 4 ... 12 hours before trying again (the hourly
    job would otherwise spend its retries and timeouts on a server that is down)."""
    st = {"failures": 0, "next": 0} if ok else lb_backoff()
    if not ok:
        st["failures"] += 1
        st["next"] = time.time() + min(3600 * 2 ** (st["failures"] - 1), LB_BACKOFF_MAX_S) - 60  # -60: the next tick
    LB_BACKOFF.parent.mkdir(parents=True, exist_ok=True)
    tmp = LB_BACKOFF.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(LB_BACKOFF)


def update(a):
    """Every step runs even when ListenBrainz is down; failed network steps are reported at the end. After a
    ListenBrainz failure its steps pause with a growing backoff; the local steps run every time."""
    failed, lb_failed = [], []

    def network(step, *args, lb=False):
        try:
            step(*args)
        except (Exception, SystemExit) as e:  # ListenBrainz down, slow or a bad token: the local log still counts
            failed.append(f"{step.__name__}: {e}")
            (lb_failed if lb else []).append(step.__name__)
            print(f"{step.__name__} failed: {e}", file=sys.stderr)

    st = lb_backoff()
    LB_PAUSED[0] = time.time() < st["next"]
    if LB_PAUSED[0]:
        print(f"ListenBrainz: paused after {st['failures']} failed runs, next try after "
              f"{dt.datetime.fromtimestamp(st['next']).strftime('%H:%M')}")
    deletions(argparse.Namespace(retry=True, json=False))
    if not LB_PAUSED[0]:
        from . import restore
        network(restore.retry, lb=True)  # restores whose ListenBrainz or YouTube step waits or failed
    rep = identity.sync()  # new downloads get an id, renamed files keep theirs
    print(identity.summary(rep))
    if rep["tagged"]:
        identity.subprocess_update()
    import_skips(a)
    import_local(a)
    if not LB_PAUSED[0]:
        network(import_lb, a, lb=True)
    network(sync, a, lb=True)  # its last step sends likes to ListenBrainz: a timeout there must not skip the export
    if not LB_PAUSED[0]:
        network(lb_playlists, argparse.Namespace(user=None, n=0, download=False, all=False), lb=True)
        lb_backoff_record(not lb_failed)
    network(youtube_index_daily)
    network(hits_background)
    network(versions_fingerprints)
    network(smart_lists)
    export(a)
    from . import doctor
    print(doctor.write_summary())
    if failed:
        sys.exit("failed: " + "; ".join(failed))


def hits_background():
    """Hits' cache, a little per hourly run: chart songs not looked up yet (a new year-end chart, a gap), at most 30
    MusicBrainz lookups in 60 s, and some stale ListenBrainz popularity. A new install already has the seed."""
    from . import hits
    hits.prefetch(argparse.Namespace(years=f"{hits.FIRST_YEAR}-{dt.date.today().year - 1}", budget=30, max_seconds=60))


def smart_lists():
    """Each smart list as the MPD playlist "Smart NAME" (hits lists export): phones play a fresh snapshot."""
    from . import smartlists
    errors = smartlists.export()
    if errors:
        raise RuntimeError("; ".join(errors))


def versions_fingerprints():
    """Audio fingerprints of new files in Versions groups, so the Versions pane finds copies without waiting."""
    from . import audiomatch, versions
    if audiomatch.available():
        versions.fingerprint()


PERIODIC = {"daily-jams", "weekly-jams", "weekly-exploration"}


def lb_import_spotify(a):
    """Send the Spotify plays (>= 30 s, already in the DB) to ListenBrainz as an import, 500 per request.
    Tagged submission_client "musicdb-spotify-import" so import-lb never reads them back as MPD plays."""
    rows = db().execute("SELECT ts, artist, title, ms_played, spotify_uri FROM events WHERE source = 'spotify' ORDER BY ts").fetchall()
    listens = []
    for ts, artist, title, ms, uri in rows:
        ai = {"music_service": "spotify.com", "submission_client": "musicdb-spotify-import", "ms_played": ms}
        if uri and uri.startswith("spotify:track:"):
            ai["spotify_id"] = "https://open.spotify.com/track/" + uri.rsplit(":", 1)[-1]
        listens.append({"listened_at": int(epoch_of(ts)),
                        "track_metadata": {"artist_name": artist, "track_name": title, "additional_info": ai}})
    print(f"{len(listens)} Spotify plays, {listens[0]['listened_at'] if listens else '-'} .. {listens[-1]['listened_at'] if listens else '-'}")
    if a.dry_run:
        print(json.dumps(listens[:2], ensure_ascii=False, indent=1)); return
    token = mbtag.lb_token()
    for i in range(0, len(listens), 500):
        req = urllib.request.Request(mbtag.lb_api("/1/submit-listens"), method="POST",
                                     data=json.dumps({"listen_type": "import", "payload": listens[i:i + 500]}).encode(),
                                     headers={"Authorization": "Token " + token, "Content-Type": "application/json",
                                              "User-Agent": mbtag.UA})
        urllib.request.urlopen(req, timeout=120).read()
        print(f"sent {min(i + 500, len(listens))}/{len(listens)}", flush=True)
        time.sleep(1)  # designed pacing: LB rate-limits submissions per user


def lb_playlists(a):
    """Newest ListenBrainz recommendation playlist of each kind (Daily/Weekly Jams, Weekly Exploration, ...)
    -> MPD playlist "LB <kind>" of the tracks in the library, plus the missing ones (optionally downloaded)."""
    user = a.user or mbtag.lb_user()
    r = mbtag.http(mbtag.lb_api(f"/1/user/{user}/playlists/createdfor?count=50"), host_interval=0.5, strict=True)
    newest = {}
    for p in r.get("playlists", []):
        pl = p["playlist"]
        kind = (pl.get("extension", {}).get("https://musicbrainz.org/doc/jspf#playlist", {})
                .get("additional_metadata", {}).get("algorithm_metadata", {}).get("source_patch"))
        if not kind or (kind not in PERIODIC and not a.all):  # yearly recaps would flood rmpc's playlist pane
            continue
        if kind not in newest or pl["date"] > newest[kind]["date"]:
            newest[kind] = pl
    if not newest:
        print(f"ListenBrainz has no recommendation playlists for {user} yet"); return
    lib = library()
    from . import deleted
    blocks = deleted.Blocks()
    for kind, pl in sorted(newest.items()):
        full = mbtag.http(mbtag.lb_api(f"/1/playlist/{pl['identifier'].rsplit('/', 1)[-1]}"),
                          host_interval=0.5, strict=True)["playlist"]
        have, missing = [], []
        for t in full.get("track", []):
            mbid = next((i.rsplit("/", 1)[-1] for i in t.get("identifier", []) if "/recording/" in i), None)
            f, _ = match(lib, None, mbid, t.get("creator"), t.get("title"))
            gone = None if f else blocks.chart_row(mbid, t.get("creator"), t.get("title"))
            (have if f else missing).append(f or (f"{t.get('creator')} - {t.get('title')}", gone))
        name = "LB " + kind.replace("-", " ").title()  # stable name: replaced each time, not one per week
        PLAYLISTS.mkdir(parents=True, exist_ok=True)
        (PLAYLISTS / f"{name}.m3u").write_text("".join(f + "\n" for f in have))
        print(f"{name}: {len(have)}/{len(have) + len(missing)} in library ({pl['title']})")
        for m, gone in missing[: a.n]:
            print(f"   missing: {m}" + (f" ({deleted.reason(gone)})" if gone else ""))
            if a.download and not gone:
                subprocess.run([sys.executable, "-m", "rormpc_tools.yt_mp3_mb", "--yes", "-d", "LB",
                                f"ytsearch1:{m} official audio", "--", "--no-playlist"])


MUSIC = settings.MUSIC_DIR
PENDING = DATA / "deletions" / "pending.jsonl"  # trashed songs (Ctrl-y restores the newest) and failed remote steps
DONE = DATA / "deletions" / "done.jsonl"
LOCK = DB.parent / "deletions.lock"


def jsonl(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l] if path.exists() else []


def write_jsonl(path, rows):
    """Atomic: a reader never sees a half-written journal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    tmp.replace(path)


@contextlib.contextmanager
def journal_lock():
    """rormpc runs delete and undo in background threads and `update` retries hourly: one writer at a time."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def describe(rel, rows, lib, m):
    """What deleting rel touches: its plays (all sources), recording MBID and video id."""
    files = library_files(lib)
    prepare(lib, files)
    info = (m.find("file", rel) or [{}])[0]
    return {"file": rel, "artist": one(info.get("artist", "")), "title": one(info.get("title", pathlib.Path(rel).stem)),
            "ytid": identity.ytid(rel), "mbid": next((x for x, f in lib[1].items() if f == rel), None),
            "events": [dict(zip(EVENT_COLS, r)) for r in rows
                       if event_file(lib, files, r[0], r[2], r[3], r[5], r[6], r[8], r[4]) == rel]}


def plays_of(events):
    """Plays among a song's events: a ListenBrainz listen with the timestamp of a local one is the same play."""
    local = {e["ts"] for e in events if e["source"] == "local"}
    return sum(1 for e in events if not (e["source"] == "lb" and e["ts"] in local))


def lb_listens(r):
    """ListenBrainz listens of a song that can be deleted (the API needs their msid)."""
    return [e for e in r["events"] if e["source"] == "lb" and json.loads(e["extra"] or "{}").get("msid")]


def youtube_index_daily():
    """Refresh yt-playlist's cached index once a day, so the delete menu knows the playlists when the login has
    expired. Quietly does nothing without chosen playlists or a valid login."""
    idx = settings.XDG_CACHE / "rormpc-tools" / "youtube-index.json"
    if not (DATA / "youtube-playlists.json").exists() or (idx.exists() and time.time() - idx.stat().st_mtime < 20 * 3600):
        return
    subprocess.run([sys.executable, "-m", "rormpc_tools.yt_playlist", "index"], capture_output=True, stdin=subprocess.DEVNULL)


def youtube_playlists(ytid):
    """My chosen YouTube playlists that contain the video, looked up live: {"playlists": [{id, title, item}]}
    or {"error": ...}. A failed lookup is never "in no playlist"."""
    if not ytid:
        return None
    if not (DATA / "youtube-playlists.json").exists():
        return {"error": "no playlists chosen (yt-playlist use ID...)"}
    p = subprocess.run([sys.executable, "-m", "rormpc_tools.yt_playlist", "find", "--json", ytid],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode:
        return {"error": ((p.stderr or p.stdout).strip().splitlines() or ["yt-playlist failed"])[-1]}
    return json.loads(p.stdout.strip().splitlines()[-1])


PREVIEW_VERSION = 1  # of `delete --preview`'s JSON, read by rormpc's delete menu


def delete(a):
    """Ctrl-x in rmpc (rormpc's delete menu). Songs come from the arguments, else rmpc's $SELECTED_SONGS, else
    $FILE (the playing song). Default: move to the Trash and keep the history (Ctrl-y undoes). --permanent
    removes the file instead (no undo). --listenbrainz also deletes the song's history: its ListenBrainz listens
    (irreversible, unless another library file has the same recording), the video in your chosen YouTube playlists
    and the local plays (kept as tombstones that re-imports skip). Each song is journaled in pending.jsonl before
    anything is touched; a failed remote step stays there and `musicdb update` retries it.
    --preview prints what a deletion would touch as JSON and changes nothing; --youtube adds the live YouTube lookup."""
    files = a.files or [f for f in os.environ.get("SELECTED_SONGS", os.environ.get("FILE", "")).splitlines() if f]
    if not files:
        sys.exit("no song given")
    c, lib, m = db(), library(), mpd()
    rows = c.execute(f"SELECT {', '.join(EVENT_COLS)} FROM events").fetchall()
    if a.preview:
        out = []  # PREVIEW_VERSION: bump it when a field changes meaning or goes away; rormpc checks it
        for rel in files:
            d = describe(rel, rows, lib, m)
            x = {k: d[k] for k in ("file", "artist", "title", "ytid")} | {
                "exists": (MUSIC / rel).exists(), "plays": plays_of(d["events"]), "lb_listens": len(lb_listens(d)),
                "shared": other_copies(d["mbid"], rel)}
            if a.youtube:
                x["youtube"] = youtube_playlists(d["ytid"])
            out.append(x)
        print(json.dumps({"version": PREVIEW_VERSION, "songs": out}, ensure_ascii=False))
        return
    recs, names = [], []
    with journal_lock():
        pending = jsonl(PENDING)
        for rel in files:
            src = MUSIC / rel
            if not src.exists():
                print(f"not found: {src}"); continue
            d = describe(rel, rows, lib, m)
            if (m.currentsong() or {}).get("file") == rel:
                m.next()  # don't leave MPD playing a file that is gone
            try:
                stickers = m.sticker_list("song", rel)  # restored by `undo` in case MPD drops them
            except Exception:
                stickers = {}
            rid = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{src.name}"
            rec = {"id": rid, "file": rel, "mode": "permanent" if a.permanent else "trash",
                   "trashed_to": None if a.permanent else str(pathlib.Path.home() / ".Trash" / rid),
                   "history": "delete" if a.listenbrainz else "keep", "ytid": d["ytid"], "mbid": d["mbid"],
                   "artist": d["artist"], "title": d["title"],
                   "queued_at": dt.datetime.now().isoformat(timespec="seconds"), "stickers": stickers,
                   "events": d["events"]}
            pending.append(rec)
            write_jsonl(PENDING, pending)  # journal first: a crash below leaves a record of what was meant
            if a.permanent:
                src.unlink()
            else:
                src.rename(rec["trashed_to"])
            recs.append(rec)
            names.append(f"{d['artist']} - {d['title']}".strip(" -"))
            print(f"{'deleted' if a.permanent else 'trashed'} {rel}: {len(d['events'])} plays, "
                  f"{len(lb_listens(d))} ListenBrainz listens")
    if not recs:
        return
    subprocess.run(["mpc", "-q", "update"])
    for r in recs:
        if r["history"] == "delete":
            finish(r)
    errors = settle(recs)
    if a.listenbrainz:
        export(argparse.Namespace())
    # the last line is what rormpc shows in its status bar (stdout on success, stderr on failure)
    summary = f"{'Deleted' if a.permanent else 'Trashed'}: {', '.join(names)}"
    if errors:
        notify(summary[:120], f"FAILED, retried hourly: {'; '.join(errors)[:150]}")
        sys.exit(f"{summary}; failed, retried hourly: {'; '.join(errors)}")
    print(summary + ("; history deleted" if a.listenbrainz else "") + ("" if a.permanent else " (Ctrl-y undoes)"))


def finish(r):
    """The history part of a deletion (mutates r): ListenBrainz listens, YouTube playlists, local plays.
    Each step's outcome goes to r["ops"]; steps already done are skipped, so a retry continues."""
    ops, errors = r.setdefault("ops", {}), []
    if ops.get("listenbrainz") != "done" and not ops.get("listenbrainz", "").startswith("kept"):
        if other_copies(r.get("mbid"), r["file"]):
            ops["listenbrainz"] = "kept: another file has the same recording"
        else:
            gone = set(r.setdefault("lb_deleted", []))
            try:
                token = mbtag.lb_token()
                for e in lb_listens(r):
                    msid = json.loads(e["extra"])["msid"]
                    if f"{e['ts']} {msid}" in gone:
                        continue
                    req = urllib.request.Request(mbtag.lb_api("/1/delete-listen"), method="POST",
                                                 data=json.dumps({"listened_at": int(epoch_of(e["ts"])),
                                                                  "recording_msid": msid}).encode(),
                                                 headers={"Authorization": "Token " + token, "Content-Type": "application/json",
                                                          "User-Agent": mbtag.UA})
                    urllib.request.urlopen(req, timeout=30).read()  # network deadline for one API call
                    r["lb_deleted"].append(f"{e['ts']} {msid}")
                ops["listenbrainz"] = "done"
            except Exception as err:  # keep what was deleted; a retry continues from there
                ops["listenbrainz"] = f"failed: {err}"
                errors.append(f"ListenBrainz: {err}")
    if r.get("ytid") and ops.get("youtube") != "done":
        yt = youtube_remove(r["ytid"])
        if isinstance(yt, str):
            ops["youtube"] = yt
            if not yt.startswith("skipped"):
                errors.append(f"YouTube: {yt}")
        else:
            ops["youtube"] = "done"
            r["youtube_removed"] = yt
    if ops.get("local") != "done":
        c = db()
        for e in r["events"]:  # local history: keep as tombstones, re-imports skip them
            vals = [e[k] for k in EVENT_COLS]
            c.execute(f"INSERT OR IGNORE INTO tombstones VALUES ({','.join('?' * len(TOMB_COLS))})",
                      vals + [dt.datetime.now().isoformat(timespec="seconds")])
            c.execute(f"DELETE FROM events WHERE ({KEY}) = ({','.join('?' * 7)})", vals[:7])
        c.commit()
        ops["local"] = "done"
    r["error"] = "; ".join(errors) or None
    return errors


def settle(recs):
    """Write finished records back: failures stay pending (retried), permanent deletions go to done.jsonl,
    trashed songs stay pending so Ctrl-y can restore them. Returns the errors."""
    errors = [r["error"] for r in recs if r.get("error")]
    with journal_lock():
        pending, done = jsonl(PENDING), jsonl(DONE)
        by_id = {r["id"]: r for r in recs}
        keep = []
        for p in pending:
            r = by_id.get(p["id"], p)
            if p["id"] in by_id and r["mode"] == "permanent" and not r.get("error"):
                done.append(r | {"finished_at": dt.datetime.now().isoformat(timespec="seconds")})
            else:
                keep.append(r)
        write_jsonl(PENDING, keep)
        write_jsonl(DONE, done)
    return errors


def notify(text, subtitle="", title="musicdb"):
    """Best-effort desktop notification, for failures (rormpc shows successes in its status bar) and for news of a
    job on a timer (liveplaylist's daily check): terminal-notifier, else osascript on macOS; notify-send on Linux;
    nothing when none is installed. It never raises: a missing notifier must not fail work that is already done."""
    try:
        if sys.platform == "darwin":
            if shutil.which("terminal-notifier"):
                cmd = ["terminal-notifier", "-title", title, "-subtitle", subtitle, "-message", text]
            else:
                q = lambda x: x.replace("\\", "\\\\").replace('"', '\\"')
                cmd = ["osascript", "-e", f'display notification "{q(text)}" with title "{q(title)}" subtitle "{q(subtitle)}"']
        elif shutil.which("notify-send"):
            cmd = ["notify-send", title, f"{text}\n{subtitle}".strip()]
        else:
            return
        subprocess.run(cmd, capture_output=True, timeout=10)  # deadline: a hung notifier must not block the job
    except Exception:
        pass


def undo(a):
    """Restore the most recently trashed song (repeat to go further back), or the one with --id, with its
    stickers. Deleted listens and playlist entries stay deleted. Its journal record goes, and with it the block that
    kept downloaders from fetching the song again (deleted.py)."""
    with journal_lock():
        pending = jsonl(PENDING)
        trashed = [r for r in pending if r.get("mode", "trash") == "trash" and r.get("trashed_to")
                   and pathlib.Path(r["trashed_to"]).exists()]
        if getattr(a, "id", None):
            trashed = [r for r in trashed if r["id"] == a.id]
            if not trashed:
                sys.exit(f"Not in the Trash any more: {a.id}")
        if not trashed:
            sys.exit("Nothing to undo")
        r = max(trashed, key=lambda r: r["queued_at"])
        dst = MUSIC / r["file"]
        if dst.exists():
            sys.exit(f"Not restored: {r['file']} exists again")
        pathlib.Path(r["trashed_to"]).rename(dst)
        write_jsonl(PENDING, [x for x in pending if x is not r])
    subprocess.run(["mpc", "-q", "update", "--wait"])
    m = mpd()
    for k, v in (r.get("stickers") or {}).items():
        m.sticker_set("song", r["file"], k, v)
    kept = r.get("history") != "delete"
    print(f"Restored: {r['file'].rsplit('/', 1)[-1]}" + ("" if kept else " (its deleted listens stay deleted)"))


def other_copies(mbid, rel):
    """Library files other than rel with the same recording MBID: their listens must stay on ListenBrainz."""
    return [s["file"] for s in mpd().find("musicbrainz_trackid", mbid) if s.get("file") != rel] if mbid else []


def youtube_remove(ytid):
    """Remove the video from your chosen YouTube playlists (yt-playlist, official API): the list of playlist ids,
    or an error string (recorded and retried; an expired login needs `yt-playlist auth` in a terminal)."""
    if not (DATA / "youtube-playlists.json").exists():
        return "skipped (no playlists chosen: yt-playlist use ID...)"
    p = subprocess.run([sys.executable, "-m", "rormpc_tools.yt_playlist", "remove", ytid],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode:
        return f"failed: {((p.stderr or p.stdout).strip().splitlines() or ['?'])[-1]} https://youtu.be/{ytid}"
    return json.loads(p.stdout.strip().splitlines()[-1])["removed"]


def deletions(a):
    """The deletion journal: trashed songs (Ctrl-y restores the newest) and remote steps that failed.
    --retry runs the failed steps again (`update` does it hourly)."""
    if a.retry:
        failed = [r for r in jsonl(PENDING) if r.get("error")]
        if not failed:
            return
        for r in failed:
            finish(r)
        settle(failed)
        for r in failed:
            print(f"{r['file']}: " + ", ".join(f"{k} {v}" for k, v in r["ops"].items()))
        # notify once per new error, not every hour while e.g. the YouTube login stays expired
        fresh = [r for r in failed if r.get("error") and r.get("notified_error") != r["error"]]
        if fresh:
            notify(f"Deletion cleanup failed for {len(fresh)} songs", "; ".join(r["error"] for r in fresh)[:150])
            with journal_lock():
                pending = jsonl(PENDING)
                errs = {r["id"]: r["error"] for r in fresh}
                for p in pending:
                    if p["id"] in errs:
                        p["notified_error"] = errs[p["id"]]
                write_jsonl(PENDING, pending)
        export(argparse.Namespace())
        return
    rows = [{"id": r["id"], "file": r["file"], "mode": r.get("mode", "trash"), "history": r.get("history", "keep"),
             "queued_at": r["queued_at"], "ops": r.get("ops", {}), "error": r.get("error")} for r in jsonl(PENDING)]
    if a.action:
        if not a.id:
            sys.exit(f"musicdb deletions {a.action} ID")
        from . import deleted
        deleted.log(a.action, a.id)
        print(f"{a.id}: " + ("may be downloaded again" if a.action == "allow" else "never downloaded again"))
        return
    if a.json and getattr(a, "all", False):
        from . import deleted, restore
        ok, restored = deleted.allowed(), restore.records()
        print(json.dumps([journal_row(r) | {"download": block_row(deleted.entry(r), r["id"] in ok),
                                            "restore": restore.state_of(r["id"], restored)}
                          for r in jsonl(PENDING) + jsonl(DONE)], ensure_ascii=False)); return
    if a.json:
        print(json.dumps(rows, ensure_ascii=False)); return
    for x in rows:
        print(f"{x['queued_at']}  {x['mode']:9} history {x['history']:6} {x['file']}"
              + (f"\n   {', '.join(f'{k} {v}' for k, v in x['ops'].items())}" if x["ops"] else "")
              + (f"\n   ERROR {x['error']}" if x["error"] else ""))


def name_title(rel):
    """The video title in a downloaded file name (NNN--Channel--Title--ytid--YYYYMMDD.mp3), else the stem."""
    parts = pathlib.Path(rel).stem.split("--")
    if identity.ytid_from_name(rel) and len(parts) >= 4:
        return "--".join(parts[2:-2] or parts[1:-2]).replace("_", " ")
    return pathlib.Path(rel).stem


def block_row(e, allowed):
    """How a deletion gates the downloaders, for the Deleted pane: "blocked" or "allowed", and what it matches."""
    return {"state": "allowed" if allowed else "blocked", "ytid": e["ytid"], "mbid": e["mbid"],
            "chart_key": e["chart_key"]}


def journal_row(r):
    """One deleted song for rormpc's Deleted pane. Older records have no artist/title: take them from a play, else
    the file name. `restorable`: the file is still in the Trash (Ctrl-y / `undo --id`)."""
    first = (r.get("events") or [{}])[0]
    trashed = r.get("trashed_to")
    return {"id": r["id"], "file": r["file"], "artist": r.get("artist") or first.get("artist") or "",
            "title": r.get("title") or first.get("title") or name_title(r["file"]),
            "mode": r.get("mode", "trash"), "history": r.get("history", "keep"), "deleted_at": r["queued_at"],
            "finished_at": r.get("finished_at"), "restorable": bool(trashed and pathlib.Path(trashed).exists()),
            "plays": plays_of(r.get("events") or []), "lb_listens": len(lb_listens(r)),
            "lb_deleted": len(r.get("lb_deleted") or []), "youtube_removed": r.get("youtube_removed") or [],
            "ytid": r.get("ytid"), "ops": r.get("ops", {}), "error": r.get("error")}


def top(a):
    plays, last, favs, _, _ = counted(db(), library())
    for f, k in plays.most_common(a.n):
        print(f"{k:5d}  {last[f][:10]}  {'♥' if f in favs else ' '}  {f}")


def skips_cmd(a):
    c, lib = db(), library()
    plays, last, _, _, _ = counted(c, lib)
    for f, k in skipped(c, last).most_common(a.n):
        print(f"{k:3d} skips  {plays.get(f, 0):4d} plays  {last.get(f, '')[:10] or '-':10}  {f}")


def missing(a):
    _, _, _, unmatched, fav_missing = counted(db(), library())
    print("# played, not in library")
    for (artist, title, ytid), k in unmatched.most_common(a.n):
        print(f"{k:5d}  {artist} - {title}" + (f"  https://youtu.be/{ytid}" if ytid else ""))
    print(f"\n# favorites not in library ({len(fav_missing)})")
    for src, artist, title, ytid in fav_missing[: a.n]:
        print(f"  [{src}] {artist} - {title}" + (f"  https://youtu.be/{ytid}" if ytid else ""))


@external.cli
def main():
    if sys.argv[1:] == ["--version"]:
        print(f"musicdb {settings.version()}")
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("tag", "genre"):
        from . import tags
        return (tags.main_tag if sys.argv[1] == "tag" else tags.main_genre)(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "chart":
        from . import chart
        return chart.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        from . import restore
        return restore.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "identity":
        return identity.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "versions":
        from . import versions
        return versions.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "dedupe":
        from . import dedupe
        return dedupe.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "doctor":
        from . import doctor
        return doctor.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "lyrics":
        from . import lyrics
        return lyrics.main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("import-mpdlog").set_defaults(fn=import_mpdlog)
    sp.add_parser("import-lb").set_defaults(fn=import_lb)
    p = sp.add_parser("import-takeout"); p.add_argument("path"); p.set_defaults(fn=import_takeout)
    p = sp.add_parser("import-spotify"); p.add_argument("path"); p.set_defaults(fn=import_spotify)
    p = sp.add_parser("import-favorites"); p.add_argument("file"); p.set_defaults(fn=import_favorites)
    sp.add_parser("import-local").set_defaults(fn=import_local)
    sp.add_parser("import-skips").set_defaults(fn=import_skips)
    sp.add_parser("sync").set_defaults(fn=sync)
    sp.add_parser("update").set_defaults(fn=update)
    sp.add_parser("export").set_defaults(fn=export)
    p = sp.add_parser("lb-import-spotify"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(fn=lb_import_spotify)
    p = sp.add_parser("lb-playlists"); p.add_argument("--user"); p.add_argument("-n", type=int, default=10, help="missing tracks to list")
    p.add_argument("--download", action="store_true", help="download the listed missing tracks with yt-mp3-mb")
    p.add_argument("--all", action="store_true", help="also the yearly recap playlists"); p.set_defaults(fn=lb_playlists)
    p = sp.add_parser("delete"); p.add_argument("files", nargs="*", help="paths relative to the music dir")
    p.add_argument("--permanent", action="store_true", help="remove the file instead of moving it to the Trash")
    p.add_argument("--listenbrainz", action="store_true", help="also delete its history: ListenBrainz, YouTube playlists, local plays")
    p.add_argument("--preview", action="store_true", help="print what would be touched (JSON), change nothing")
    p.add_argument("--youtube", action="store_true", help="with --preview: look the video up in your YouTube playlists")
    p.set_defaults(fn=delete)
    p = sp.add_parser("undo"); p.add_argument("--id", help="a journal id (musicdb deletions --json)")
    p.set_defaults(fn=undo)
    for verb in ("keep", "unkeep"):
        p = sp.add_parser(verb, help="(un)mark songs as not deletion candidates (Not finished)")
        p.add_argument("files", nargs="*"); p.set_defaults(fn=keep_cmd, cmd=verb)
    p = sp.add_parser("deletions"); p.add_argument("--retry", action="store_true")
    p.add_argument("action", nargs="?", choices=("allow", "block"),
                   help="allow: the deleted song may be downloaded again; block: never again (the default)")
    p.add_argument("id", nargs="?", help="a journal id (musicdb deletions --json --all)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="with --json: also finished permanent deletions")
    p.set_defaults(fn=deletions)
    p = sp.add_parser("top"); p.add_argument("-n", type=int, default=30); p.set_defaults(fn=top)
    p = sp.add_parser("skips"); p.add_argument("-n", type=int, default=30); p.set_defaults(fn=skips_cmd)
    p = sp.add_parser("missing"); p.add_argument("-n", type=int, default=50); p.set_defaults(fn=missing)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
