"""Live playlists: a public YouTube playlist mirrored as an MPD playlist, checked for new tracks when you ask.

  liveplaylist add URL [--name NAME] [--dir DIR]   # subscribe and list it; every item waits for your review
  liveplaylist check [ID ...]                      # list again: new items wait (pending), gone ones go inactive
  liveplaylist accept ID (YTID ... | --all)        # accept items and download them (--no-download: queue only)
  liveplaylist reject ID YTID ...                  # never download these items (rejects are durable)
  liveplaylist download [ID ...]                   # one worker (a second one exits) empties the queue, resumable
  liveplaylist list [ID ...]                       # subscriptions and their items

Every command takes --json: one JSON object on stdout, messages on stderr. ID is the subscription id that `add`
prints (yt-<playlist id>), YTID a video id.

First version: public YouTube playlists only (no mixes), checked by hand, nothing on a timer. `check` lists the
playlist with `yt-dlp --flat-playlist -J` (no download). Each item has a decision (pending / accepted / rejected)
and, separately, a job state (queued / downloading / needs_match / ready / failed):

- An accepted item that is already in the library is referenced, not downloaded: the same YouTube video in the
  identity registry (songs.jsonl), or the recording MusicBrainz links to that video, when exactly one recording
  is linked and a library file carries its MBID. Never by title.
- Otherwise it is downloaded with `yt-mp3-mb --batch` into a staging dir (~/.cache/rormpc-tools/liveplaylist/
  staging). A confident MusicBrainz match moves into the subscription's dir in the music dir (ready); anything
  else waits as needs_match with the proposal, outside the library, until you accept it as it is (names only, no
  MBID) or reject it.
- The MPD playlist (<name>.m3u in MPD's playlist directory, written atomically) holds the accepted, ready, still
  listed items in the playlist's order; file names carry no position.
- Nothing deletes a file: a rejected or vanished item leaves the playlist, its file stays. A failed or partial
  listing changes nothing but the last-check error; an item that comes back is active again with its old decision.

State: one JSON per subscription in <data_dir>/liveplaylists/ (written atomically). Progress of the running
command: ~/.cache/rormpc-tools/liveplaylist/status.json (atomic), its log next to it; locks there too.
"""
import argparse, contextlib, datetime as dt, fcntl, json, os, pathlib, random, re, shutil, signal, subprocess, sys
import time, urllib.parse

from . import identity, mbtag, settings, yt_mp3_mb

SCHEMA = 1
MUSIC = settings.MUSIC_DIR
PLAYLISTS = settings.MPD_PLAYLISTS
DATA = settings.DATA_DIR / "liveplaylists"
CACHE = settings.XDG_CACHE / "rormpc-tools" / "liveplaylist"
PAUSE = (3, 8)  # seconds between downloads: external rate limit, YouTube blocks bursts
MAX_FAILURES = 3
DECISIONS = ("pending", "accepted", "rejected")
JOBS = ("queued", "downloading", "needs_match", "ready", "failed")
UNAVAILABLE = re.compile(r"^\[(deleted|private) video\]$", re.I)


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def say(*a):
    print(*a, file=sys.stderr, flush=True)


def write_atomic(path, text):
    """temp + rename in the same dir: a reader (rormpc) never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # not mkstemp: its 0600 would hide the m3u from MPD
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def locked():
    """One writer of the subscription files at a time (short: never held during a listing or a download)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / "state.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


# ------------------------------------------------------------------ subscriptions

def list_id(url):
    """The playlist id of a YouTube / YouTube Music URL, or a ValueError saying what is supported."""
    u = urllib.parse.urlparse(url.strip())
    host = (u.hostname or "").lower()
    if not (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")):
        raise ValueError("only public YouTube playlists for now (a youtube.com URL with list=...)")
    lid = (urllib.parse.parse_qs(u.query).get("list") or [""])[0]
    if not re.fullmatch(r"[\w-]{10,}", lid):
        raise ValueError("not a playlist URL: it has no list=... parameter")
    if lid.startswith("RD"):
        raise ValueError("a YouTube mix is generated for each viewer, not a playlist; save it as a playlist first")
    return lid


def sub_path(sid):
    if not re.fullmatch(r"yt-[\w-]+", sid):
        raise ValueError(f"not a subscription id: {sid}")
    return DATA / f"{sid}.json"


def load(sid):
    p = sub_path(sid)
    if not p.exists():
        raise ValueError(f"no subscription {sid} (liveplaylist list shows them)")
    return json.loads(p.read_text())


def save(sub):
    sub["updated_at"] = now()
    write_atomic(sub_path(sub["id"]), json.dumps(sub, ensure_ascii=False, indent=1) + "\n")


def all_ids():
    return sorted(p.stem for p in DATA.glob("yt-*.json")) if DATA.is_dir() else []


def ordered(sub):
    """Items in playlist order: active ones by position, then inactive ones by when they were last seen."""
    return sorted(sub["items"].values(), key=lambda it: (not it["active"], it["position"] if it["active"] else 0,
                                                         it.get("last_seen") or ""))


def counts(sub):
    c = {k: 0 for k in DECISIONS + JOBS + ("inactive",)}
    for it in sub["items"].values():
        c[it["decision"]] += 1
        if it.get("job"):
            c[it["job"]] += 1
        c["inactive"] += not it["active"]
    return c


def m3u_path(name):
    return PLAYLISTS / f"{name}.m3u"


def free_name(title, sid):
    """An MPD playlist name no other playlist uses: the title, else "title (2)", ..."""
    base = re.sub(r'[/\\\x00-\x1f]', " ", title).strip() or sid
    taken = {load(s)["playlist"] for s in all_ids() if s != sid}
    name, n = base, 1
    while name in taken or m3u_path(name).exists():
        n += 1
        name = f"{base} ({n})"
    return name


def write_m3u(sub):
    """Accepted, ready, still listed items in playlist order; paths relative to the music dir."""
    lines = [it["path"] for it in ordered(sub)
             if it["active"] and it["decision"] == "accepted" and it.get("job") == "ready" and it.get("path")]
    write_atomic(m3u_path(sub["playlist"]), "".join(f"{p}\n" for p in lines))
    return len(lines)


# ------------------------------------------------------------------ listing

def listing(url):
    """yt-dlp's flat listing: ({"title", "entries": [...]}) or RuntimeError."""
    return yt_mp3_mb.flat(url)


def merge(sub, info):
    """Apply one listing to the subscription. Returns {"new", "back", "gone", "partial"}. A partial listing (fewer
    entries than the playlist count, or holes) never makes an item inactive."""
    entries = info.get("entries")
    if entries is None:
        raise RuntimeError("yt-dlp returned no playlist entries")
    stamp = now()
    seen, new, back = set(), [], []
    partial = any(e is None for e in entries)
    total = info.get("playlist_count")
    if isinstance(total, int) and total > len(entries):
        partial = True
    if not entries and any(it["active"] for it in sub["items"].values()):
        partial = True  # a playlist emptied at once is more likely a broken extraction than a decision
    position = 0
    for e in entries:
        if not e or not e.get("id"):
            continue
        yid, title = e["id"], e.get("title") or ""
        it = sub["items"].get(yid)
        if UNAVAILABLE.match(title):
            # still in the playlist upstream, but not downloadable: keep what we have, add nothing
            if it:
                seen.add(yid)
                it["last_seen"] = stamp
            continue
        if it is None:
            it = sub["items"][yid] = {"ytid": yid, "title": title, "channel": e.get("channel") or e.get("uploader") or "",
                                      "duration": e.get("duration"), "decision": "pending", "job": None, "path": None,
                                      "first_seen": stamp, "active": True}
            new.append(yid)
        elif not it["active"]:
            it["active"] = True
            back.append(yid)
        it.update(position=position, last_seen=stamp)
        if title:
            it["title"] = title
        seen.add(yid)
        position += 1
    gone = []
    if not partial:
        for yid, it in sub["items"].items():
            if yid not in seen and it["active"]:
                it["active"] = False
                gone.append(yid)
    if info.get("title"):
        sub["source_title"] = info["title"]
    return {"new": new, "back": back, "gone": gone, "partial": partial}


def check_one(sid):
    """List the playlist (outside the lock), then merge it under the lock."""
    url = load(sid)["url"]
    try:
        info = listing(url)
        error = None
    except RuntimeError as e:
        info, error = None, str(e)
    with locked():
        sub = load(sid)
        result = {"id": sid, "ok": False, "new": [], "back": [], "gone": [], "partial": False, "error": error}
        if info is not None:
            try:
                result.update(merge(sub, info), ok=True)
            except RuntimeError as e:
                result["error"] = str(e)
        sub["last_check"] = {"at": now(), "ok": result["ok"], "error": result["error"], "partial": result["partial"],
                             "new": len(result["new"])}
        save(sub)
        if result["ok"]:
            write_m3u(sub)
    return result


def cmd_add(a):
    lid = list_id(a.url)
    sid = f"yt-{lid}"
    url = f"https://www.youtube.com/playlist?list={lid}"
    if sub_path(sid).exists():
        say(f"{sid}: already subscribed, checking it")
        return {"id": sid, "added": False, "check": check_one(sid)}
    info = listing(url)  # a playlist that cannot be listed is not subscribed
    title = info.get("title") or lid
    with locked():
        if sub_path(sid).exists():
            return {"id": sid, "added": False, "check": check_one(sid)}
        rel = a.dir or f"LivePlaylists/{yt_mp3_mb.safe(title, 80)}--{lid}"
        sub = {"schema": SCHEMA, "id": sid, "kind": "youtube", "url": url, "list_id": lid, "title": title,
               "playlist": a.name or free_name(title, sid), "dir": rel, "added_at": now(), "items": {}}
        if a.name and m3u_path(a.name).exists():
            raise ValueError(f"an MPD playlist named {a.name!r} exists already: choose another --name")
        result = merge(sub, info)
        sub["last_check"] = {"at": now(), "ok": True, "error": None, "partial": result["partial"],
                             "new": len(result["new"])}
        save(sub)
        write_m3u(sub)
    say(f"{sid}: {title!r}, {len(result['new'])} items to review; MPD playlist {sub['playlist']!r}")
    return {"id": sid, "added": True, "check": {"id": sid, "ok": True, **result, "error": None}}


def cmd_check(a):
    results = []
    for sid in a.ids or all_ids():
        r = check_one(sid)
        say(f"{sid}: " + (f"{len(r['new'])} new, {len(r['back'])} back, {len(r['gone'])} gone"
                          + (" (partial listing: nothing marked gone)" if r["partial"] else "")
                          if r["ok"] else f"check failed, nothing changed: {r['error']}"))
        results.append(r)
    return {"checks": results}


# ------------------------------------------------------------------ decisions

def cmd_accept(a):
    if not a.all and not a.ytids:
        raise ValueError("give video ids or --all")
    changed = {"queued": [], "ready": [], "unchanged": []}
    with locked():
        sub = load(a.id)
        for yid in (a.ytids or [it["ytid"] for it in ordered(sub) if it["decision"] == "pending" and it["active"]]):
            it = sub["items"].get(yid)
            if it is None:
                raise ValueError(f"{a.id} has no item {yid}")
            it["decision"] = "accepted"
            it["decided_at"] = now()
            if it.get("job") == "needs_match":
                # accepted as it is: names only, no MBID
                it.update(job="ready", path=promote(sub, it), error=None, review=None)
                changed["ready"].append(yid)
            elif it.get("job") in (None, "failed"):
                it.update(job="queued", error=None)
                changed["queued"].append(yid)
            else:
                changed["unchanged"].append(yid)
        save(sub)
        write_m3u(sub)
    if changed["ready"]:
        mpd_update(sub["dir"])
    say(f"{a.id}: {len(changed['queued'])} queued, {len(changed['ready'])} ready")
    result = {"id": a.id, **changed}
    if changed["queued"] and not a.no_download:
        result["download"] = download()  # the whole queue: one worker serves every subscription
    return result


def cmd_reject(a):
    with locked():
        sub = load(a.id)
        for yid in a.ytids:
            it = sub["items"].get(yid)
            if it is None:
                raise ValueError(f"{a.id} has no item {yid}")
            it["decision"] = "rejected"
            it["decided_at"] = now()
            if it.get("job") == "queued":
                it["job"] = None
        save(sub)
        write_m3u(sub)
    say(f"{a.id}: {len(a.ytids)} rejected")
    return {"id": a.id, "rejected": a.ytids}


def cmd_list(a):
    subs = []
    for sid in a.ids or all_ids():
        sub = load(sid)
        subs.append({k: v for k, v in sub.items() if k != "items"} | {"counts": counts(sub), "items": ordered(sub)})
    if not a.json:
        for s in subs:
            c = s["counts"]
            print(f"{s['id']}  {s['title']}  -> {s['playlist']}.m3u  pending {c['pending']}, ready {c['ready']}, "
                  f"queued {c['queued']}, needs match {c['needs_match']}, failed {c['failed']}"
                  + (f"  last check failed: {s['last_check']['error']}" if not s.get("last_check", {}).get("ok", True) else ""))
    return {"subscriptions": subs, "status": read_status()}


# ------------------------------------------------------------------ downloads

def status_path():
    return CACHE / "status.json"


def read_status():
    try:
        return json.loads(status_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_status(**fields):
    write_atomic(status_path(), json.dumps({"pid": os.getpid(), "updated_at": now(), **fields}, ensure_ascii=False))


def log(sid, **fields):
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / "log.jsonl", "a") as f:
        f.write(json.dumps({"at": now(), "id": sid, **fields}, ensure_ascii=False) + "\n")


def staging(sub):
    return CACHE / "staging" / sub["id"]


def promote(sub, it):
    """Move a staged file into the subscription's dir in the library; returns its path relative to the music dir."""
    src = pathlib.Path(it["path"])
    if not src.is_absolute():
        return it["path"]  # already in the library
    dest = MUSIC / sub["dir"] / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        shutil.move(src, dest)
    return str(dest.relative_to(MUSIC))


def in_library(yid):
    """(path relative to the music dir, how) of a library file that is this video's recording, else None. Only a
    confirmed match: the same YouTube id, or the single recording MusicBrainz links to the video."""
    live = [r for r in identity.load()["rows"].values() if r.get("state") == "live" and r.get("path")]
    same = sorted(r["path"] for r in live if r.get("ytid") == yid)
    if same:
        return same[0], "same YouTube video"
    recordings = {rel["recording"] for rel in mbtag.mb_url(yid) if rel.get("recording")}
    if len(recordings) == 1:
        mbid = recordings.pop()
        owned = sorted(r["path"] for r in live if r.get("mbid") == mbid)
        if owned:
            return owned[0], f"MusicBrainz recording {mbid}"
    return None


def file_with(directory, yid):
    if directory.is_dir():
        for f in sorted(directory.iterdir()):
            if identity.ytid_from_name(f.name) == yid:
                return f
    return None


def fetch_item(sub, it):
    """Bring one accepted item into the library; returns the fields to store on it."""
    yid = it["ytid"]
    found = in_library(yid)
    if found:
        return {"job": "ready", "path": found[0], "source": "library", "match": found[1], "error": None}
    target = MUSIC / sub["dir"]
    if (f := file_with(target, yid)):  # moved in before an interruption
        return {"job": "ready", "path": str(f.relative_to(MUSIC)), "source": "download", "error": None}
    stage = staging(sub)
    if (f := file_with(stage, yid)):
        return {"job": "needs_match", "path": str(f), "source": "download", "error": None,
                "review": {"reason": "downloaded before an interruption; its match was not recorded"}}
    report = yt_mp3_mb.batch([f"https://www.youtube.com/watch?v={yid}"], str(stage), ["--no-playlist"],
                             known=({stage}, {yid}))
    entry = next((f for f in report["files"] if f["ytid"] == yid), None)
    if entry is None:
        err = report["error"] or next((f["error"] for f in report["failed"] if f["ytid"] == yid), None)
        return {"job": "failed", "error": err or "yt-dlp produced no file"}
    review = next((r["proposal"] for r in report["needs_review"] if r["ytid"] == yid), None)
    if review is not None:
        return {"job": "needs_match", "path": entry["path"], "source": "download", "error": None,
                "review": {"reason": "no confident MusicBrainz match", **review}}
    it = {**it, "path": entry["path"]}
    return {"job": "ready", "path": promote(sub, it), "source": "download", "mbid": entry["mbid"], "error": None}


def update_item(sid, yid, **fields):
    with locked():
        sub = load(sid)
        # an item rejected while it downloaded keeps its file but stays out of the playlist (write_m3u)
        sub["items"][yid].update(fields, changed_at=now())
        save(sub)
        write_m3u(sub)
        return sub


def mpd_update(rel_dir):
    """Let MPD see the new files before rormpc loads the playlist (MPD_HOST/MPD_PORT apply)."""
    if shutil.which("mpc"):
        subprocess.run(["mpc", "-q", "--wait", "update", rel_dir])


class Cancelled(BaseException):
    """SIGTERM: not an Exception, so no `except Exception` on the way (yt-mp3-mb's per-file guard) swallows it."""


def _cancel(*_):
    raise Cancelled()


def download(ids=None):
    """Download every queued, accepted item of these subscriptions (None: all), one at a time, until the queue is
    empty. A second worker exits."""
    CACHE.mkdir(parents=True, exist_ok=True)
    worker = open(CACHE / "worker.lock", "w")
    try:
        fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError("a liveplaylist download is already running")
    signal.signal(signal.SIGTERM, _cancel)  # rormpc cancels with SIGTERM: yt-dlp is killed, the item requeued
    with locked():  # a worker that died mid-item left it downloading: queue it again
        for sid in ids or all_ids():
            sub = load(sid)
            for it in sub["items"].values():
                if it.get("job") == "downloading":
                    it["job"] = "queued"
            save(sub)
    done, failed, errors, failures, arrived, current, fetched = 0, 0, [], 0, set(), None, False
    state = "done"
    try:
        # the queue is read again before each item: items accepted while this runs are downloaded too
        while todo := queued(ids):
            sid, yid, title = todo[0]
            if fetched:  # only after a YouTube request: a library reference asks YouTube nothing
                time.sleep(random.uniform(*PAUSE))
            current = (sid, yid)
            write_status(running=True, state="running", subscription=sid, current={"ytid": yid, "title": title},
                         done=done, failed=failed, total=done + failed + len(todo), errors=errors[-5:])
            say(f"==> {title} ({yid})")
            sub = update_item(sid, yid, job="downloading")
            try:
                fields = fetch_item(sub, sub["items"][yid])
            except Exception as e:
                fields = {"job": "failed", "error": f"{type(e).__name__}: {e}"}
            update_item(sid, yid, **fields)
            current = None
            fetched = fields.get("source") != "library"
            log(sid, ytid=yid, **{k: v for k, v in fields.items() if k != "review"})
            say(f"    {fields['job']}" + (f": {fields['error']}" if fields.get("error") else ""))
            if fields["job"] == "failed":
                failed += 1
                failures += 1
                errors.append(f"{title}: {fields['error']}")
                if "429" in fields["error"]:
                    errors.append("YouTube answered 429 (too many requests): stopped; try again in an hour")
                    state = "stopped"
                    break
                if failures >= MAX_FAILURES:
                    errors.append(f"{MAX_FAILURES} failures in a row: stopped")
                    state = "stopped"
                    break
            else:
                failures = 0
                done += 1
                if fields["job"] == "ready" and fields.get("source") == "download":
                    arrived.add(sid)
    except (Cancelled, KeyboardInterrupt):
        state = "cancelled"
        if current:
            update_item(*current, job="queued")
    for sid in sorted(arrived):
        mpd_update(load(sid)["dir"])
    total = done + failed + len(queued(ids))
    summary = {"state": state, "done": done, "failed": failed, "total": total, "errors": errors}
    write_status(running=False, subscription=None, current=None, **summary)
    say(f"liveplaylist: {done} done, {failed} failed of {total}" + (f" ({state})" if state != "done" else ""))
    return summary


def queued(ids):
    """Accepted items waiting for the worker, in playlist order (ids: these subscriptions, else all)."""
    return [(sid, it["ytid"], it["title"]) for sid in ids or all_ids() for it in ordered(load(sid))
            if it["decision"] == "accepted" and it.get("job") == "queued"]


def cmd_download(a):
    return download(a.ids or None)


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="liveplaylist", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {settings.version()}")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("add", help="subscribe to a playlist URL")
    p.add_argument("url"); p.add_argument("--name", help="MPD playlist name (default: the playlist title)")
    p.add_argument("--dir", help="where its downloads go, relative to the music dir")
    p.set_defaults(fn=cmd_add)
    p = sp.add_parser("check", help="list the playlists again"); p.add_argument("ids", nargs="*"); p.set_defaults(fn=cmd_check)
    p = sp.add_parser("accept", help="accept items (and download them)")
    p.add_argument("id"); p.add_argument("ytids", nargs="*"); p.add_argument("--all", action="store_true",
                                                                               help="every pending item")
    p.add_argument("--no-download", action="store_true", help="only queue them")
    p.set_defaults(fn=cmd_accept)
    p = sp.add_parser("reject", help="never download these items")
    p.add_argument("id"); p.add_argument("ytids", nargs="+"); p.set_defaults(fn=cmd_reject)
    p = sp.add_parser("download", help="download the queued items"); p.add_argument("ids", nargs="*")
    p.set_defaults(fn=cmd_download)
    p = sp.add_parser("list", help="subscriptions and items"); p.add_argument("ids", nargs="*"); p.set_defaults(fn=cmd_list)
    for p in sp.choices.values():
        p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    a = ap.parse_args(argv)
    try:
        result = a.fn(a)
    except (ValueError, RuntimeError) as e:
        if a.json:
            print(json.dumps({"error": str(e)}, ensure_ascii=False))
        say(f"liveplaylist: {e}")
        sys.exit(1)
    if a.json:
        print(json.dumps(result, ensure_ascii=False))
    worker = result.get("download", result)  # accept runs the download too
    failed = worker.get("state") in ("stopped", "cancelled") or any(not c["ok"] for c in result.get("checks", []))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
