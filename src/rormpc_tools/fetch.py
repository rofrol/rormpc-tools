"""Import queue for the chart songs you don't have (rormpc's Hits pane "Fetch missing…").

  hits fetch add --from ~/.cache/rormpc/hits/current.json [--rank 12 ...] [--first 10]   # queue missing rows
  hits fetch run                # one worker (a second one exits): search, download, verify, promote
  hits fetch status [--json]    # every item and its state
  hits fetch accept KEY ...     # move reviewed files into the library as they are
  hits fetch reject KEY ...     # delete reviewed files, never fetch that song again
  hits fetch retry KEY ...      # failed or rejected items back to the queue
  hits fetch cancel             # the worker stops after the current song; queued items stay queued
  hits fetch clear              # forget songs already in the library (rejected ones stay, so they never return)

Per song: YouTube search (5 results) -> candidates filtered by duration (within 5 s of the chart recording on
MusicBrainz) and version words (live, remix, cover, sped up, ...) unless the chart title has them -> download the
best one into a staging dir outside the music dir -> identify it like yt-mp3-mb (MusicBrainz URL relation,
AcoustID, ...). Only an exact match of the chart's recording MBID goes into <music>/Hits/<decade>s; anything else
waits in review with the reason ("other recording of the same song", "different song", "no match"). Tags are
never changed to force agreement. Between songs the worker sleeps 8-20 s; it stops after 3 failures in a row or
when YouTube answers 429.

State: $XDG_STATE_HOME/rormpc-tools/fetch/queue.json (written atomically; rormpc reads it), staged files next to it.
"""
import argparse, contextlib, datetime as dt, fcntl, json, os, pathlib, random, re, shutil, subprocess, sys, time

from . import mbtag, settings, yt_mp3_mb

STATE_DIR = pathlib.Path(os.environ.get("XDG_STATE_HOME", settings.HOME / ".local/state")) / "rormpc-tools/fetch"
QUEUE = STATE_DIR / "queue.json"
STAGING = STATE_DIR / "staging"
LOCK = STATE_DIR / "queue.lock"
WORKER_LOCK = STATE_DIR / "worker.lock"
CANCEL = STATE_DIR / "cancel"
MUSIC = settings.MUSIC_DIR
DURATION_SLACK = 5  # seconds between the chart recording and a YouTube candidate
MAX_FAILURES = 3
VERSION = re.compile(r"\b(live|remix|cover|sped[ -]?up|slowed|reverb|karaoke|nightcore|instrumental|8d|"
                     r"extended|acoustic|lyrics? video|mashup|loop|1 hour|hour version)\b", re.I)
STATES = ("queued", "searching", "downloading", "verifying", "ok", "review", "failed", "rejected")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


@contextlib.contextmanager
def locked():
    """One writer of queue.json at a time (the worker and `add`/`accept` from rormpc)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def load():
    return json.loads(QUEUE.read_text()) if QUEUE.exists() else {"version": 1, "items": []}


def save(q):
    """Atomic: rormpc may read the file at any moment."""
    q["updated_at"] = now()
    tmp = QUEUE.with_name(QUEUE.name + ".tmp")
    tmp.write_text(json.dumps(q, ensure_ascii=False, indent=1))
    tmp.replace(QUEUE)


def update(key, **fields):
    with locked():
        q = load()
        for it in q["items"]:
            if it["key"] == key:
                it.update(fields, changed_at=now())
        save(q)


def song_key(row):
    """Queue identity: the chart recording MBID, else the normalised artist|title (what `hits hide` uses)."""
    from .hits import hide_key
    return row.get("mbid") or hide_key(row["artist"], row["title"])


def add(a):
    rows = json.loads(pathlib.Path(a.source).expanduser().read_text())["rows"]
    missing = [r for r in rows if not r.get("file") and not r.get("hidden")]
    if a.rank:
        missing = [r for r in missing if r["rank"] in set(a.rank)]
    if a.first:
        missing = missing[: a.first]
    added = 0
    with locked():
        q = load()
        known = {it["key"] for it in q["items"]}
        for r in missing:
            key = song_key(r)
            if key in known:
                continue  # queued, done or rejected before: never twice
            q["items"].append({"key": key, "artist": r["artist"], "title": r["title"], "year": r["year"],
                               "mbid": r.get("mbid"), "state": "queued", "added_at": now(), "changed_at": now()})
            known.add(key)
            added += 1
        save(q)
    print(f"queued {added} of {len(missing)} missing songs" + (f" ({len(missing) - added} already known)" if len(missing) > added else ""))


def chart_length(mbid):
    """Seconds of the chart recording on MusicBrainz, or None."""
    rec = mbtag.mb_recording(mbid) if mbid else None
    return round(rec["length"] / 1000) if rec and rec.get("length") else None


def candidates(item):
    """YouTube search results that could be the chart recording, best first: [(score, entry, reason)]."""
    from .hits import main_artist
    query = f"ytsearch5:{main_artist(item['artist'])} - {item['title']} official audio"
    p = subprocess.run(["yt-dlp", "--ignore-config", "--flat-playlist", "-J", query], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(((p.stderr or "").strip().splitlines() or ["yt-dlp search failed"])[-1])
    entries = json.loads(p.stdout).get("entries") or []
    length = chart_length(item.get("mbid"))
    chart_versions = set(m.lower() for m in VERSION.findall(item["title"]))
    artist = mbtag.norm(main_artist(item["artist"]))
    out, nearest = [], []
    for e in entries:
        title, channel = e.get("title") or "", e.get("channel") or e.get("uploader") or ""
        words = set(m.lower() for m in VERSION.findall(title)) - chart_versions
        if words:
            continue  # a live/remix/cover/... upload, unless the chart entry is that version
        dur = e.get("duration")
        if length and dur and abs(dur - length) > DURATION_SLACK:
            nearest.append(f"{round(dur)} s {channel}")
            continue
        if not length and dur and not 90 <= dur <= 900:
            continue
        score = 0.0
        if artist and artist in mbtag.norm(channel):
            score += 2  # the artist's own channel or "<artist> - Topic"
        if channel.endswith(" - Topic") or "vevo" in channel.lower():
            score += 1
        if mbtag.norm(item["title"]) in mbtag.norm(title):
            score += 1
        if length and dur:
            score += 1 - abs(dur - length) / (DURATION_SLACK + 1)
        out.append((score, e, f"{channel} · {title} · {dur and round(dur)} s"))
    return sorted(out, key=lambda x: -x[0]), length, nearest


def verify(item, row):
    """'ok' only for the chart's own recording; otherwise review with a reason."""
    if item.get("mbid") and row["mbid"] == item["mbid"]:
        return "ok", "same recording as the chart entry"
    if not row["mbid"]:
        return "review", "no MusicBrainz match"
    if mbtag.sim(row["title"], item["title"]) >= 0.85 and mbtag.sim(row["artist"], item["artist"]) >= 0.6:
        return "review", f"other recording of the same song ({row['mbid']})"
    return "review", f"different song: {row['artist']} - {row['title']}"


def target_dir(item):
    return MUSIC / "Hits" / f"{item['year'] // 10 * 10}s"


def fetch_one(item):
    """Search, download into staging, identify; returns the fields to store on the item."""
    key = item["key"]
    update(key, state="searching", error=None)
    found, length, nearest = candidates(item)
    if not found:
        return {"state": "failed", "error": f"no YouTube upload within {DURATION_SLACK} s of the chart recording"
                + (f" ({length} s)" if length else "") + " without live/remix/cover words"
                + (f"; other lengths: {', '.join(nearest[:3])}" if nearest else "")}
    _, entry, why = found[0]
    url = f"https://www.youtube.com/watch?v={entry['id']}"
    update(key, state="downloading", candidate=why, url=url)
    infos = yt_mp3_mb.download([url], ["--no-playlist"])
    if not infos:
        return {"state": "failed", "error": "yt-dlp produced no file"}
    info = infos[0]
    update(key, state="verifying")
    path = pathlib.Path(info["filepath"])
    d = mbtag.collect(path, info["id"], info.get("channel") or info.get("uploader") or "", info.get("title") or "",
                      info.get("description"), float(info.get("duration") or 0), info.get("artist"), info.get("track"))
    row = mbtag.resolve(d)
    mbtag.write_tags(path, row, album=d["provided"]["album"] if d["provided"] else None)
    try:
        cov = mbtag.cover(info["id"], row["mbid"])
        if cov:
            mbtag.embed_cover(path, cov, row["title"])
    except Exception as e:  # a missing cover must not lose the download
        print(f"  cover failed: {e}", file=sys.stderr)
    name = f"{yt_mp3_mb.safe(row['artist'], 60)}--{yt_mp3_mb.safe(row['title'])}--{info['id']}--{info.get('upload_date') or ''}.mp3"
    state, reason = verify(item, row)
    STAGING.mkdir(parents=True, exist_ok=True)
    dest = (target_dir(item) if state == "ok" else STAGING) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(path, dest)
    return {"state": state, "reason": reason, "file": str(dest), "found_mbid": row["mbid"],
            "found": f"{row['artist']} - {row['title']}", "method": row["method"]}


def run(_a):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    worker = open(WORKER_LOCK, "w")
    try:
        fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("a fetch worker is already running")
    CANCEL.unlink(missing_ok=True)
    with locked():  # a worker that died mid-song left its item in a busy state: queue it again
        q = load()
        for it in q["items"]:
            if it["state"] in ("searching", "downloading", "verifying"):
                it["state"] = "queued"
        save(q)
    failures, failed, done, arrived, first = 0, 0, 0, 0, True
    while True:
        if CANCEL.exists():
            CANCEL.unlink(missing_ok=True)
            print("cancelled")
            break
        item = next((it for it in load()["items"] if it["state"] == "queued"), None)
        if not item:
            break
        if not first:
            time.sleep(random.uniform(8, 20))  # external rate limit: YouTube blocks bursts of searches/downloads
        first = False
        print(f"==> {item['artist']} - {item['title']}", flush=True)
        try:
            fields = fetch_one(item)
        except (Exception, SystemExit) as e:  # yt_mp3_mb.download exits when yt-dlp fails
            fields = {"state": "failed", "error": str(e)}
        update(item["key"], **fields)
        print(f"    {fields['state']}: {fields.get('reason') or fields.get('error') or ''}", flush=True)
        if fields["state"] == "failed":
            failures += 1
            failed += 1
            if "429" in (fields.get("error") or ""):
                print("YouTube answered 429 (too many requests): stopping; try again in an hour", file=sys.stderr)
                break
            if failures >= MAX_FAILURES:
                print(f"{MAX_FAILURES} failures in a row: stopping", file=sys.stderr)
                break
        else:
            failures = 0
            done += 1
            arrived += fields["state"] == "ok"
    if arrived:
        subprocess.run(["mpc", "-q", "update"])
    # the last line is what rormpc shows; it reruns hits unless "0 new"
    review = sum(1 for it in load()["items"] if it["state"] == "review")
    print(f"fetch: {arrived} new in the library, {done - arrived} to review, {failed} failed"
          + (f" · {review} waiting for review" if review else ""))


def status(a):
    q = load()
    if a.json:
        print(json.dumps(q, ensure_ascii=False))
        return
    for it in q["items"]:
        extra = it.get("reason") or it.get("error") or ""
        print(f"{it['state']:11} {it['artist']} - {it['title']} ({it['year']})" + (f"\n            {extra}" if extra else ""))


def decide(a, verb):
    moved = []
    with locked():
        q = load()
        for it in q["items"]:
            if it["key"] not in a.keys:
                continue
            if verb == "retry" and it["state"] in ("failed", "rejected"):
                it.update(state="queued", error=None, changed_at=now())
            elif verb in ("accept", "reject") and it["state"] == "review":
                f = pathlib.Path(it["file"])
                if verb == "accept":
                    dest = target_dir(it) / f.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if f.exists():
                        shutil.move(f, dest)
                        moved.append(dest)
                    it.update(state="ok", file=str(dest), reason=f"accepted: {it.get('reason', '')}", changed_at=now())
                else:
                    f.unlink(missing_ok=True)
                    it.update(state="rejected", file=None, changed_at=now())
        save(q)
    if moved:
        subprocess.run(["mpc", "-q", "update"])
    print(f"{verb}: {len(a.keys)}")


def clear(_a):
    with locked():
        q = load()
        q["items"] = [it for it in q["items"] if it["state"] not in ("ok",)]
        save(q)


def main(argv):
    ap = argparse.ArgumentParser(prog="hits fetch", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("add"); p.add_argument("--from", dest="source", required=True, help="a hits --json result")
    p.add_argument("--rank", type=int, nargs="*", help="only these ranks"); p.add_argument("--first", type=int)
    p.set_defaults(fn=add)
    sp.add_parser("run").set_defaults(fn=run)
    p = sp.add_parser("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=status)
    for verb in ("accept", "reject", "retry"):
        p = sp.add_parser(verb); p.add_argument("keys", nargs="+"); p.set_defaults(fn=lambda a, v=verb: decide(a, v))
    sp.add_parser("cancel").set_defaults(fn=lambda _a: (STATE_DIR.mkdir(parents=True, exist_ok=True), CANCEL.touch()))
    sp.add_parser("clear").set_defaults(fn=clear)
    a = ap.parse_args(argv)
    a.fn(a)
